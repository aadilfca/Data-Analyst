using System.Net;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using Microsoft.Net.Http.Headers;

namespace AuditPlatform.Api.Coexistence;

public sealed class LegacyForwarder : IDisposable
{
    private readonly CoexistenceOptions settings;
    private readonly HttpClient client;
    private readonly SemaphoreSlim slots;
    private readonly ILogger<LegacyForwarder> logger;

    public LegacyForwarder(IOptions<CoexistenceOptions> options, ILogger<LegacyForwarder> logger)
        : this(options, logger, CreateHandler(options.Value)) { }

    // The handler seam lets tests rehearse transport faults without production
    // fault routes, identity headers, or connections to the real Python server.
    public LegacyForwarder(IOptions<CoexistenceOptions> options, ILogger<LegacyForwarder> logger,
        HttpMessageHandler handler)
    {
        settings = options.Value;
        this.logger = logger;
        slots = new(settings.MaximumConcurrentRequests);
        client = new(handler) { Timeout = Timeout.InfiniteTimeSpan };
    }

    public static SocketsHttpHandler CreateHandler(CoexistenceOptions options) => new()
    {
        AllowAutoRedirect = false, UseCookies = false, UseProxy = false,
        AutomaticDecompression = DecompressionMethods.None,
        ConnectTimeout = TimeSpan.FromSeconds(options.ConnectTimeoutSeconds),
        PooledConnectionLifetime = TimeSpan.FromMinutes(2),
        MaxConnectionsPerServer = options.MaximumConcurrentRequests
    };

    public async Task ForwardAsync(HttpContext context, CoexistenceRoute route)
    {
        if (!settings.Enabled || !settings.TryGetTarget(out var origin)
            || context.Connection.LocalPort == origin!.Port)
        {
            await RejectAsync(context, 503, "target_not_ready", "not_sent");
            return;
        }
        if (!await slots.WaitAsync(0, context.RequestAborted))
        {
            await RejectAsync(context, 503, "bridge_busy", "not_sent");
            return;
        }
        var dispatched = false;
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(context.RequestAborted);
        deadline.CancelAfter(TimeSpan.FromSeconds(settings.TimeoutSeconds));
        try
        {
            if (context.Request.Headers.ContentEncoding.Count != 0 || context.Request.Headers.Upgrade.Count != 0)
                throw new BridgeFailure(415, "unsupported_request_encoding_or_upgrade");
            if (context.Request.ContentLength > settings.MaximumRequestBytes)
                throw new BridgeFailure(413, "request_too_large");
            // A bounded request buffer prevents oversized/incomplete input from
            // reaching a legacy mutation before its full body has been checked.
            using var input = new MemoryStream();
            await CopyBoundedAsync(context.Request.Body, input, settings.MaximumRequestBytes, deadline.Token,
                () => new BridgeFailure(413, "request_too_large"));
            if (context.Request.ContentLength is long expected && input.Length != expected)
                throw new BridgeFailure(400, "incomplete_request");
            input.Position = 0;
            using var request = new HttpRequestMessage(new HttpMethod(route.Method),
                new Uri(origin, route.Path + context.Request.QueryString.Value));
            if (request.RequestUri!.Fragment.Length != 0)
                throw new BridgeFailure(400, "ambiguous_query");
            request.Content = new StreamContent(input);
            request.Content.Headers.ContentLength = input.Length;
            var requestHopHeaders = HopHeaders(context.Request.Headers.Connection.ToString());
            foreach (var name in RequestHeaders)
                if (!requestHopHeaders.Contains(name) && context.Request.Headers.TryGetValue(name, out var values))
                    request.Headers.TryAddWithoutValidation(name, values.ToArray());
            if (!requestHopHeaders.Contains("Content-Type") && context.Request.ContentType is string contentType)
                request.Content.Headers.ContentType = System.Net.Http.Headers.MediaTypeHeaderValue.Parse(contentType);
            if (!requestHopHeaders.Contains("Cookie") && context.Request.Cookies.TryGetValue("session_token", out var cookie))
            {
                if (!ValidToken(cookie)) throw new BridgeFailure(400, "invalid_session_cookie");
                request.Headers.TryAddWithoutValidation("Cookie", "session_token=" + cookie);
            }
            dispatched = true;
            // Exactly one application dispatch: no retries, redirects, fallback
            // backend, or persistent shared CookieContainer.
            using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
            if ((int)response.StatusCode < 200 || response.StatusCode == HttpStatusCode.SwitchingProtocols)
                throw new BridgeFailure(502, "unsupported_response");
            if (response.Content.Headers.ContentLength > settings.MaximumResponseBytes)
                throw new BridgeFailure(502, "response_too_large");
            var responseHopHeaders = HopHeaders(string.Join(",", response.Headers.Connection));
            var location = responseHopHeaders.Contains("Location") ? null : LocalRedirect(response.Headers.Location, origin);
            var cookies = responseHopHeaders.Contains("Set-Cookie") ? [] : SessionCookies(response);
            context.Response.StatusCode = (int)response.StatusCode;
            foreach (var name in ResponseHeaders)
            {
                if (responseHopHeaders.Contains(name)) continue;
                if (response.Headers.TryGetValues(name, out var values)
                    || response.Content.Headers.TryGetValues(name, out values))
                    context.Response.Headers[name] = values.ToArray();
            }
            if (location is not null) context.Response.Headers.Location = location;
            foreach (var value in cookies) context.Response.Headers.Append("Set-Cookie", value);
            if (route.Access != CoexistenceAccess.Public || cookies.Count != 0)
                context.Response.Headers.CacheControl = "no-store";
            if (response.Content.Headers.ContentLength is long length) context.Response.ContentLength = length;
            if (route.Method == "HEAD" || response.StatusCode is HttpStatusCode.NoContent or HttpStatusCode.NotModified)
                return;
            await using var output = await response.Content.ReadAsStreamAsync(deadline.Token);
            var written = await CopyBoundedAsync(output, context.Response.Body, settings.MaximumResponseBytes, deadline.Token,
                () => new BridgeFailure(502, "response_too_large"));
            if (response.Content.Headers.ContentLength is long declared && written != declared)
                throw new BridgeFailure(502, "incomplete_response");
        }
        catch (OperationCanceledException) when (context.RequestAborted.IsCancellationRequested)
        {
            context.Abort();
        }
        catch (Exception error) when (error is BridgeFailure or HttpRequestException or IOException
            or OperationCanceledException or FormatException)
        {
            var (status, reason) = error switch
            {
                BridgeFailure failure => (failure.Status, failure.Reason),
                OperationCanceledException => (504, "bridge_timeout"),
                FormatException => (400, "invalid_request_header"),
                _ => (502, "transport_unavailable")
            };
            var outcome = dispatched && route.Method is not "GET" and not "HEAD" ? "unknown" : "not_completed";
            if (!dispatched) outcome = "not_sent";
            // Never log request paths/queries, cookies, body, exception text or upstream URLs.
            logger.LogWarning("Bridge failure {RouteId} {Method} {Reason} {Outcome}", route.Id, route.Method, reason, outcome);
            if (context.Response.HasStarted) context.Abort();
            else
            {
                context.Response.Clear();
                await RejectAsync(context, status, reason, outcome);
            }
        }
        finally { slots.Release(); }
    }

    public static Task RejectAsync(HttpContext context, int status, string reason, string outcome)
    {
        context.Response.StatusCode = status;
        context.Response.Headers.CacheControl = "no-store";
        return context.Response.WriteAsJsonAsync(new
        {
            success = false,
            error = new
            {
                error_code = "BRIDGE_UNAVAILABLE", error_id = (long?)null,
                user_message = "The migration route could not complete this request.",
                suggested_action = outcome == "unknown"
                    ? "Check whether the operation completed before submitting it again."
                    : "Check access or service availability before trying again.",
                technical_details = (object?)null
            },
            reason, outcome
        }, context.RequestAborted);
    }

    private static async Task<int> CopyBoundedAsync(Stream source, Stream destination, int limit,
        CancellationToken token, Func<BridgeFailure> failure)
    {
        var buffer = new byte[64 * 1024];
        var total = 0;
        while (true)
        {
            var count = await source.ReadAsync(buffer.AsMemory(0, Math.Min(buffer.Length, limit - total + 1)), token);
            if (count == 0) return total;
            if (count > limit - total) throw failure();
            await destination.WriteAsync(buffer.AsMemory(0, count), token);
            total += count;
        }
    }

    private static bool ValidToken(string value) => value.Length == 64 && value.All(char.IsAsciiHexDigit);

    private static List<string> SessionCookies(HttpResponseMessage response)
    {
        var result = new List<string>();
        if (!response.Headers.TryGetValues("Set-Cookie", out var values)) return result;
        foreach (var value in values)
        {
            if (!SetCookieHeaderValue.TryParse(value, out var cookie))
                throw new BridgeFailure(502, "invalid_response_cookie");
            if (cookie.Name != "session_token") continue;
            var token = HeaderUtilities.RemoveQuotes(cookie.Value).Value ?? "";
            if ((token.Length != 0 && !ValidToken(token)) || cookie.Domain.HasValue
                || (cookie.Path.HasValue && cookie.Path != "/") || (token.Length != 0 && !cookie.HttpOnly)
                || (token.Length == 0 && cookie.MaxAge != TimeSpan.Zero
                    && !(cookie.Expires is DateTimeOffset expires && expires < DateTimeOffset.UtcNow)))
                throw new BridgeFailure(502, "invalid_response_cookie");
            result.Add(value);
        }
        return result;
    }

    private static string? LocalRedirect(Uri? location, Uri origin)
    {
        if (location is null) return null;
        var text = location.OriginalString;
        if (text.Any(c => char.IsControl(c) || c == '\\') || text.StartsWith("//", StringComparison.Ordinal)
            || (!location.IsAbsoluteUri && !text.StartsWith('/')))
            throw new BridgeFailure(502, "unsafe_redirect");
        var resolved = new Uri(origin, location);
        if (resolved.Scheme != origin.Scheme || resolved.Host != origin.Host || resolved.Port != origin.Port
            || resolved.UserInfo.Length != 0)
            throw new BridgeFailure(502, "unsafe_redirect");
        return resolved.PathAndQuery + resolved.Fragment;
    }

    private static readonly string[] RequestHeaders =
        ["Accept", "Accept-Language", "If-None-Match", "If-Modified-Since", "Range", "X-CSRF-Token"];
    private static readonly string[] ResponseHeaders =
        ["Content-Type", "Content-Disposition", "Content-Encoding", "ETag", "Last-Modified", "Cache-Control", "Vary", "Content-Range", "Accept-Ranges"];

    private static HashSet<string> HopHeaders(string value) =>
        value.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);

    private sealed class BridgeFailure(int status, string reason) : Exception("Bridge transfer rejected.")
    {
        public int Status { get; } = status;
        public string Reason { get; } = reason;
    }

    public void Dispose() { client.Dispose(); slots.Dispose(); }
}
