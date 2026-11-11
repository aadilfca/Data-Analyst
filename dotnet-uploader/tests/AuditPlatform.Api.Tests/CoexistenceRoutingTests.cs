using System.ComponentModel.DataAnnotations;
using System.Net;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Coexistence;
using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class CoexistenceRoutingTests
{
    private const string Path = "/api/p06-stub/operation";
    private const string Session = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    private static CoexistenceRoute Route(string method = "POST", CoexistenceOwner owner = CoexistenceOwner.Legacy,
        CoexistenceAccess access = CoexistenceAccess.Client) => new("test_operation", method, Path, owner, access);
    private static CoexistenceOptions Settings() => new()
    {
        Enabled = true, LegacyBaseAddress = "http://127.0.0.1:5019/", TimeoutSeconds = 2, ConnectTimeoutSeconds = 1
    };

    [Fact]
    public async Task Disabled_bridge_and_empty_production_table_preserve_health_and_404()
    {
        await using var app = new WebApplicationFactory<Program>();
        using var client = app.CreateClient();
        Assert.False(app.Services.GetRequiredService<IOptions<CoexistenceOptions>>().Value.Enabled);
        Assert.Null(app.Services.GetRequiredService<CoexistenceRouteTable>().Find("POST", Path));
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/healthz", Token)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, (await client.GetAsync("/health/ready", Token)).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await client.PostAsync(Path, null, Token)).StatusCode);
    }

    [Theory]
    [InlineData(CoexistenceOwner.Legacy, "legacy")]
    [InlineData(CoexistenceOwner.DotNet, "dotnet")]
    public async Task Owner_selects_one_backend_before_dispatch(CoexistenceOwner owner, string expected)
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("legacy")));
        await using var app = App(Route(owner: owner), handler, CoexistenceAccessDecision.Allowed);
        using var client = app.CreateClient();
        using var response = await client.PostAsync(Path, new StringContent("{}"), Token);
        Assert.Equal(expected, await response.Content.ReadAsStringAsync(Token));
        Assert.Equal(owner == CoexistenceOwner.Legacy ? 1 : 0, handler.Calls);
    }

    [Theory]
    [InlineData(CoexistenceAccessDecision.Unavailable, 503)]
    [InlineData(CoexistenceAccessDecision.Unauthenticated, 401)]
    [InlineData(CoexistenceAccessDecision.Forbidden, 403)]
    public async Task Denied_decisions_reach_neither_backend(CoexistenceAccessDecision decision, int status)
    {
        var handler = new Stub((_, _) => throw new InvalidOperationException());
        await using var app = App(Route(), handler, decision);
        using var client = app.CreateClient();
        using var response = await client.PostAsync(Path, null, Token);
        Assert.Equal(status, (int)response.StatusCode);
        Assert.Equal(0, handler.Calls);
        Assert.Equal("not_sent", Json(await response.Content.ReadAsStringAsync(Token)).GetProperty("outcome").GetString());
    }

    [Fact]
    public async Task Default_gate_blocks_even_public_registration_and_forged_identity_headers()
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("must not run")));
        await using var app = App(Route(access: CoexistenceAccess.Public), handler, null);
        using var client = app.CreateClient();
        client.DefaultRequestHeaders.Add("X-User-Id", "1");
        client.DefaultRequestHeaders.Add("X-User-Role", "admin");
        client.DefaultRequestHeaders.Add("Cookie", "session_token=" + Session);
        using var response = await client.PostAsync(Path, null, Token);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, response.StatusCode);
        Assert.Equal(0, handler.Calls);
    }

    [Fact]
    public async Task Changing_owner_is_explicit_and_a_legacy_failure_never_calls_native_route()
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("legacy rejected", HttpStatusCode.Conflict)));
        await using var legacy = App(Route(), handler, CoexistenceAccessDecision.Allowed);
        using var legacyClient = legacy.CreateClient();
        using var failed = await legacyClient.PostAsync(Path, null, Token);
        Assert.Equal(HttpStatusCode.Conflict, failed.StatusCode);
        Assert.Equal("legacy rejected", await failed.Content.ReadAsStringAsync(Token));
        await using var native = App(Route(owner: CoexistenceOwner.DotNet), handler, CoexistenceAccessDecision.Allowed);
        using var nativeClient = native.CreateClient();
        Assert.Equal("dotnet", await (await nativeClient.PostAsync(Path, null, Token)).Content.ReadAsStringAsync(Token));
        Assert.Equal(1, handler.Calls);
    }

    [Fact]
    public async Task Unknown_route_and_unowned_method_never_forward()
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("unexpected")));
        await using var app = App(Route(), handler, CoexistenceAccessDecision.Allowed);
        using var client = app.CreateClient();
        using var unknown = await client.GetAsync("/api/unknown", Token);
        Assert.Equal(HttpStatusCode.NotFound, unknown.StatusCode);
        using var method = await client.GetAsync(Path, Token);
        Assert.Equal(HttpStatusCode.MethodNotAllowed, method.StatusCode);
        Assert.Contains("POST", method.Content.Headers.Allow);
        Assert.Equal(0, handler.Calls);
    }

    [Theory]
    [InlineData("http://example.invalid:5000/")]
    [InlineData("http://localhost:5000/")]
    [InlineData("http://127.0.0.1:80/")]
    [InlineData("http://127.0.0.1:5000/private")]
    [InlineData("http://127.0.0.1:5000/?password=PRIVATE_SENTINEL")]
    [InlineData("http://user:PRIVATE_SENTINEL@127.0.0.1:5000/")]
    [InlineData("http://127.0.0.1:5000/#fragment")]
    [InlineData("http://2130706433:5000/")]
    public void Unsafe_targets_fail_sanitized_validation(string value)
    {
        var options = Settings(); options.LegacyBaseAddress = value;
        var failures = new List<ValidationResult>();
        Assert.False(Validator.TryValidateObject(options, new(options), failures, true));
        Assert.DoesNotContain("PRIVATE_SENTINEL", string.Join(" ", failures));
    }

    [Theory]
    [InlineData("TimeoutSeconds", 0)]
    [InlineData("TimeoutSeconds", 121)]
    [InlineData("ConnectTimeoutSeconds", 11)]
    [InlineData("MaximumConcurrentRequests", 0)]
    [InlineData("MaximumConcurrentRequests", 17)]
    [InlineData("MaximumRequestBytes", 0)]
    [InlineData("MaximumResponseBytes", 67108865)]
    public void Invalid_limits_are_rejected(string property, int value)
    {
        var options = Settings(); typeof(CoexistenceOptions).GetProperty(property)!.SetValue(options, value);
        Assert.False(Validator.TryValidateObject(options, new(options), new List<ValidationResult>(), true));
    }

    [Theory]
    [InlineData("/healthz")]
    [InlineData("/health/ready")]
    [InlineData("/HEALTHZ/")]
    [InlineData("//example.invalid/path")]
    [InlineData("/api/../other")]
    [InlineData("/api/%2fother")]
    [InlineData("/api/{id}")]
    [InlineData("/api/path?target=other")]
    public void Reserved_or_ambiguous_route_paths_are_rejected(string path) =>
        Assert.Throws<ArgumentException>(() => new CoexistenceRouteTable([Route() with { Path = path }]));

    [Fact]
    public void Duplicate_ids_or_method_paths_fail_before_activation()
    {
        Assert.Throws<ArgumentException>(() => new CoexistenceRouteTable([Route(), Route() with { Id = "other" }]));
        Assert.Throws<ArgumentException>(() => new CoexistenceRouteTable([Route(), Route() with { Method = "GET" }]));
        Assert.Throws<ArgumentException>(() => new CoexistenceRouteTable([Route() with { Method = "OPTIONS" }]));
    }

    [Fact]
    public async Task Invalid_target_fails_real_host_start_without_leaking_value()
    {
        await using var app = new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
            builder.ConfigureServices(services => services.PostConfigure<CoexistenceOptions>(options =>
            {
                options.Enabled = true; options.LegacyBaseAddress = "http://PRIVATE_SENTINEL.invalid:5000/";
            })));
        var error = Assert.ThrowsAny<Exception>(() => app.CreateClient());
        Assert.DoesNotContain("PRIVATE_SENTINEL", error.ToString());
    }

    [Theory]
    [InlineData("/api/%70%30%36-stub/operation", "")]
    [InlineData("/api/p06-stub/operation", "/prefix")]
    public async Task Alternate_raw_encoding_or_pathbase_is_rejected(string raw, string pathBase)
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("unexpected")));
        using var forwarder = Forwarder(handler);
        var context = Context(); context.Request.PathBase = pathBase;
        context.Features.Get<IHttpRequestFeature>()!.RawTarget = raw;
        var nativeCalls = 0;
        var middleware = new CoexistenceMiddleware(_ => { nativeCalls++; return Task.CompletedTask; });
        await middleware.InvokeAsync(context, Options.Create(Settings()), new([Route()]),
            new TestGate(CoexistenceAccessDecision.Allowed), forwarder);
        Assert.Equal(400, context.Response.StatusCode);
        Assert.Equal(0, handler.Calls); Assert.Equal(0, nativeCalls);
    }

    [Theory]
    [InlineData("/API/P06-STUB/OPERATION")]
    [InlineData("/api/p06-stub/operation/")]
    public async Task Native_case_or_trailing_slash_matching_cannot_bypass_the_gate(string path)
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("unexpected")));
        await using var app = App(Route(owner: CoexistenceOwner.DotNet), handler, CoexistenceAccessDecision.Forbidden);
        using var client = app.CreateClient();
        using var response = await client.PostAsync(path, null, Token);
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal(0, handler.Calls);
    }

    [Fact]
    public async Task Body_query_download_and_session_headers_are_preserved_without_untrusted_headers()
    {
        var logger = new CaptureLogger();
        var handler = new Stub(async (request, token) =>
        {
            Assert.Equal("http://127.0.0.1:5019" + Path + "?q=a%2Bb&client_id=5", request.RequestUri!.AbsoluteUri);
            Assert.Equal("{\"value\":\"PRIVATE_SENTINEL\"}", await request.Content!.ReadAsStringAsync(token));
            Assert.Equal("session_token=" + Session, Assert.Single(request.Headers.GetValues("Cookie")));
            Assert.False(request.Headers.Contains("Authorization"));
            Assert.False(request.Headers.Contains("X-User-Id"));
            Assert.False(request.Headers.Contains("X-Forwarded-Host"));
            Assert.False(request.Headers.Contains("Connection"));
            Assert.False(request.Headers.Contains("Range"));
            Assert.Equal("test-csrf", Assert.Single(request.Headers.GetValues("X-CSRF-Token")));
            var response = Reply("workbook bytes");
            response.Content.Headers.ContentType = new("application/octet-stream");
            response.Content.Headers.ContentDisposition = new("attachment") { FileName = "review.xlsx" };
            response.Headers.TryAddWithoutValidation("Set-Cookie", "session_token=" + Session + "; Path=/; HttpOnly; SameSite=Lax");
            response.Headers.TryAddWithoutValidation("Set-Cookie", "other=PRIVATE_SENTINEL; Path=/");
            response.Headers.TryAddWithoutValidation("Connection", "ETag");
            response.Headers.TryAddWithoutValidation("ETag", "\"PRIVATE_SENTINEL\"");
            response.Headers.TryAddWithoutValidation("X-Internal-Detail", "PRIVATE_SENTINEL");
            return response;
        });
        using var forwarder = Forwarder(handler, logger: logger);
        var context = Context("{\"value\":\"PRIVATE_SENTINEL\"}");
        context.Request.QueryString = new("?q=a%2Bb&client_id=5");
        context.Request.Headers.Cookie = "other=PRIVATE_SENTINEL; session_token=" + Session;
        context.Request.Headers.Authorization = "Bearer PRIVATE_SENTINEL";
        context.Request.Headers["X-User-Id"] = "1";
        context.Request.Headers["X-Forwarded-Host"] = "example.invalid";
        context.Request.Headers["X-CSRF-Token"] = "test-csrf";
        context.Request.Headers.Connection = "Range"; context.Request.Headers.Range = "bytes=0-10";
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal("workbook bytes", Body(context));
        Assert.Equal("no-store", context.Response.Headers.CacheControl.ToString());
        Assert.Contains("review.xlsx", context.Response.Headers.ContentDisposition.ToString());
        Assert.Single(context.Response.Headers.SetCookie);
        Assert.False(context.Response.Headers.ContainsKey("ETag"));
        Assert.False(context.Response.Headers.ContainsKey("X-Internal-Detail"));
        Assert.Empty(logger.Messages);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task Oversized_known_or_chunked_request_is_rejected_before_dispatch(bool known)
    {
        var settings = Settings(); settings.MaximumRequestBytes = 3;
        var handler = new Stub((_, _) => Task.FromResult(Reply("unexpected")));
        using var forwarder = Forwarder(handler, settings);
        var context = Context("1234"); if (!known) context.Request.ContentLength = null;
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal(413, context.Response.StatusCode); Assert.Equal(0, handler.Calls);
        Assert.Equal("not_sent", Json(Body(context)).GetProperty("outcome").GetString());
    }

    [Fact]
    public async Task Incomplete_input_invalid_cookie_and_self_loop_never_dispatch()
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("unexpected")));
        using var forwarder = Forwarder(handler);
        var shortBody = Context("x"); shortBody.Request.ContentLength = 2;
        await forwarder.ForwardAsync(shortBody, Route()); Assert.Equal(400, shortBody.Response.StatusCode);
        var cookie = Context(); cookie.Request.Headers.Cookie = "session_token=PRIVATE_SENTINEL";
        await forwarder.ForwardAsync(cookie, Route()); Assert.Equal(400, cookie.Response.StatusCode);
        var loop = Context(); loop.Connection.LocalPort = 5019;
        await forwarder.ForwardAsync(loop, Route()); Assert.Equal(503, loop.Response.StatusCode);
        Assert.Equal(0, handler.Calls);
    }

    [Theory]
    [InlineData("Content-Encoding", "gzip")]
    [InlineData("Upgrade", "websocket")]
    public async Task Encoded_or_upgrade_requests_are_explicitly_rejected(string name, string value)
    {
        var handler = new Stub((_, _) => Task.FromResult(Reply("unexpected")));
        using var forwarder = Forwarder(handler); var context = Context("opaque input");
        context.Request.Headers[name] = value;
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal(415, context.Response.StatusCode); Assert.Equal(0, handler.Calls);
    }

    [Theory]
    [InlineData("/static/login.html?next=home", "/static/login.html?next=home")]
    [InlineData("http://127.0.0.1:5019/static/login.html", "/static/login.html")]
    [InlineData("https://example.invalid/private", null)]
    [InlineData("//example.invalid/private", null)]
    [InlineData("relative/path", null)]
    public async Task Redirects_stay_same_origin_and_are_not_followed(string value, string? expected)
    {
        var handler = new Stub((_, _) =>
        {
            var response = Reply("", HttpStatusCode.Found); response.Headers.Location = new(value, UriKind.RelativeOrAbsolute);
            return Task.FromResult(response);
        });
        using var forwarder = Forwarder(handler); var context = Context();
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal(expected is null ? 502 : 302, context.Response.StatusCode);
        Assert.Equal(expected ?? "", context.Response.Headers.Location.ToString());
        Assert.Equal(1, handler.Calls);
        if (expected is null) Assert.DoesNotContain("example.invalid", Body(context));
    }

    [Theory]
    [InlineData("session_token=; Max-Age=0; Path=/", true)]
    [InlineData("session_token=PRIVATE_SENTINEL; HttpOnly; Path=/", false)]
    [InlineData("session_token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; Domain=example.invalid; HttpOnly; Path=/", false)]
    public async Task Logout_deletion_is_preserved_but_unsafe_session_cookies_fail(string value, bool accepted)
    {
        var handler = new Stub((_, _) =>
        {
            var response = Reply("{}"); response.Headers.TryAddWithoutValidation("Set-Cookie", value);
            return Task.FromResult(response);
        });
        using var forwarder = Forwarder(handler); var context = Context();
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal(accepted ? 200 : 502, context.Response.StatusCode);
        Assert.Equal(accepted ? 1 : 0, context.Response.Headers.SetCookie.Count);
        Assert.DoesNotContain("PRIVATE_SENTINEL", Body(context));
    }

    [Fact]
    public async Task Lost_write_response_is_unknown_and_never_replayed_or_logged_raw()
    {
        var effects = 0; var logger = new CaptureLogger();
        var handler = new Stub((_, _) =>
        {
            effects++; throw new HttpRequestException("PRIVATE_SENTINEL token=PRIVATE_SENTINEL");
        });
        using var forwarder = Forwarder(handler, logger: logger);
        var context = Context("PRIVATE_SENTINEL"); context.Request.QueryString = new("?secret=PRIVATE_SENTINEL");
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal(502, context.Response.StatusCode);
        Assert.Equal("unknown", Json(Body(context)).GetProperty("outcome").GetString());
        Assert.Equal(1, effects); Assert.Equal(1, handler.Calls);
        Assert.DoesNotContain("PRIVATE_SENTINEL", Body(context) + string.Join(" ", logger.Messages));
    }

    [Fact]
    public async Task Total_deadline_stops_waiting_without_write_retry()
    {
        var settings = Settings(); settings.TimeoutSeconds = 1;
        var handler = new Stub(async (_, token) => { await Task.Delay(Timeout.InfiniteTimeSpan, token); return Reply("unreachable"); });
        using var forwarder = Forwarder(handler, settings); var context = Context();
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal(504, context.Response.StatusCode);
        Assert.Equal("unknown", Json(Body(context)).GetProperty("outcome").GetString());
        Assert.Equal(1, handler.Calls);
    }

    [Fact]
    public async Task Client_cancellation_aborts_without_fabricated_error()
    {
        using var cancel = CancellationTokenSource.CreateLinkedTokenSource(Token);
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var handler = new Stub(async (_, token) =>
        {
            entered.SetResult(); await Task.Delay(Timeout.InfiniteTimeSpan, token); return Reply("unreachable");
        });
        using var forwarder = Forwarder(handler);
        var context = Context(); var lifetime = new Lifetime { RequestAborted = cancel.Token };
        context.Features.Set<IHttpRequestLifetimeFeature>(lifetime);
        var pending = forwarder.ForwardAsync(context, Route());
        await entered.Task.WaitAsync(TimeSpan.FromSeconds(5), Token); cancel.Cancel(); await pending;
        Assert.True(lifetime.Aborted); Assert.Equal("", Body(context)); Assert.Equal(1, handler.Calls);
    }

    [Fact]
    public async Task Concurrency_limit_rejects_before_buffer_or_dispatch_and_recovers()
    {
        var settings = Settings(); settings.MaximumConcurrentRequests = 1;
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var handler = new Stub(async (_, token) =>
        {
            entered.TrySetResult(); await release.Task.WaitAsync(token); return Reply("done");
        });
        using var forwarder = Forwarder(handler, settings);
        var first = forwarder.ForwardAsync(Context(), Route());
        await entered.Task.WaitAsync(TimeSpan.FromSeconds(5), Token);
        var busy = Context(); await forwarder.ForwardAsync(busy, Route());
        Assert.Equal(503, busy.Response.StatusCode); Assert.Equal(1, handler.Calls);
        release.SetResult(); await first;
        var recovered = Context(); await forwarder.ForwardAsync(recovered, Route());
        Assert.Equal(200, recovered.Response.StatusCode); Assert.Equal(2, handler.Calls);
    }

    [Fact]
    public async Task Response_size_and_incomplete_stream_never_append_a_second_reply()
    {
        var settings = Settings(); settings.MaximumResponseBytes = 3;
        using (var large = Forwarder(new Stub((_, _) => Task.FromResult(Reply("1234"))), settings))
        {
            var context = Context(); await large.ForwardAsync(context, Route());
            Assert.Equal(502, context.Response.StatusCode); Assert.Contains("response_too_large", Body(context));
        }
        var handler = new Stub((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StreamContent(new InterruptedStream())
        }));
        using var partial = Forwarder(handler);
        var started = Context(); var lifetime = new Lifetime { RequestAborted = Token };
        started.Features.Set<IHttpRequestLifetimeFeature>(lifetime);
        started.Features.Set<IHttpResponseFeature>(new StartedResponse());
        started.Response.Body = new MemoryStream();
        await partial.ForwardAsync(started, Route());
        Assert.True(lifetime.Aborted); Assert.Equal("partial", Body(started)); Assert.Equal(1, handler.Calls);
    }

    [Fact]
    public async Task Unknown_length_response_cannot_exceed_the_transfer_limit()
    {
        var settings = Settings(); settings.MaximumResponseBytes = 3;
        var handler = new Stub((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        { Content = new StreamContent(new ChunkStream("1234")) }));
        using var forwarder = Forwarder(handler, settings); var context = Context();
        await forwarder.ForwardAsync(context, Route());
        Assert.Equal(502, context.Response.StatusCode);
        Assert.Contains("response_too_large", Body(context)); Assert.Equal(1, handler.Calls);
    }

    [Fact]
    public async Task Mismatched_response_length_aborts_a_started_transfer()
    {
        var handler = new Stub((_, _) =>
        {
            var response = Reply("abc"); response.Content.Headers.ContentLength = 20;
            return Task.FromResult(response);
        });
        using var forwarder = Forwarder(handler); var context = Context();
        var lifetime = new Lifetime { RequestAborted = Token };
        context.Features.Set<IHttpRequestLifetimeFeature>(lifetime);
        context.Features.Set<IHttpResponseFeature>(new StartedResponse());
        context.Response.Body = new MemoryStream();
        await forwarder.ForwardAsync(context, Route());
        Assert.True(lifetime.Aborted); Assert.Equal("abc", Body(context)); Assert.Equal(1, handler.Calls);
    }

    [Theory]
    [InlineData("HEAD", 200)]
    [InlineData("GET", 204)]
    [InlineData("GET", 304)]
    public async Task Bodyless_responses_remain_bodyless(string method, int status)
    {
        using var forwarder = Forwarder(new Stub((_, _) => Task.FromResult(Reply("unused", (HttpStatusCode)status))));
        var context = Context(); await forwarder.ForwardAsync(context, Route(method));
        Assert.Equal(status, context.Response.StatusCode); Assert.Equal("", Body(context));
    }

    [Fact]
    public async Task Real_loopback_transport_does_not_share_cookies_or_follow_redirects()
    {
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions { EnvironmentName = "Testing" });
        builder.Logging.ClearProviders(); builder.WebHost.UseUrls("http://127.0.0.1:0");
        await using var upstream = builder.Build();
        var observed = new List<string>();
        upstream.Run(async context =>
        {
            observed.Add(context.Request.Headers.Cookie.ToString());
            if (observed.Count == 1)
                context.Response.Headers.Append("Set-Cookie", "session_token=" + Session + "; Path=/; HttpOnly; SameSite=Lax");
            if (observed.Count == 3)
            {
                context.Response.StatusCode = 302; context.Response.Headers.Location = "/another-route";
            }
            await context.Response.WriteAsync("upstream", context.RequestAborted);
        });
        await upstream.StartAsync(Token);
        try
        {
            var address = upstream.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            var settings = Settings(); settings.LegacyBaseAddress = address.TrimEnd('/') + "/";
            using var forwarder = new LegacyForwarder(Options.Create(settings), new CaptureLogger());
            var first = Context(); first.Request.Headers.Cookie = "session_token=" + Session;
            await forwarder.ForwardAsync(first, Route());
            var second = Context(); await forwarder.ForwardAsync(second, Route());
            var third = Context(); await forwarder.ForwardAsync(third, Route());
            Assert.Equal(3, observed.Count);
            Assert.Equal("session_token=" + Session, observed[0]); Assert.Equal("", observed[1]);
            Assert.Equal(302, third.Response.StatusCode); Assert.Equal("/another-route", third.Response.Headers.Location.ToString());
            Assert.Equal("upstream", Body(second));
        }
        finally { await upstream.StopAsync(Token); }
    }

    private static WebApplicationFactory<Program> App(CoexistenceRoute route, Stub handler, CoexistenceAccessDecision? decision) =>
        new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseEnvironment("Testing");
            builder.ConfigureServices(services =>
            {
                services.AddSingleton(route);
                services.PostConfigure<CoexistenceOptions>(options =>
                { options.Enabled = true; options.LegacyBaseAddress = Settings().LegacyBaseAddress; });
                if (decision is not null) services.AddSingleton<ICoexistenceAccessGate>(new TestGate(decision.Value));
                services.AddSingleton(provider => new LegacyForwarder(provider.GetRequiredService<IOptions<CoexistenceOptions>>(),
                    provider.GetRequiredService<ILogger<LegacyForwarder>>(), handler));
                services.AddControllers().AddApplicationPart(typeof(P06StubController).Assembly);
            });
        });

    private static LegacyForwarder Forwarder(Stub handler, CoexistenceOptions? options = null, CaptureLogger? logger = null) =>
        new(Options.Create(options ?? Settings()), logger ?? new(), handler);
    private static DefaultHttpContext Context(string body = "")
    {
        var context = new DefaultHttpContext();
        context.Request.Method = "POST"; context.Request.Path = Path; context.Request.ContentType = "application/json";
        context.Request.Body = new MemoryStream(Encoding.UTF8.GetBytes(body));
        context.Request.ContentLength = context.Request.Body.Length;
        context.Response.Body = new MemoryStream(); context.RequestAborted = Token;
        return context;
    }
    private static string Body(HttpContext context) => Encoding.UTF8.GetString(((MemoryStream)context.Response.Body).ToArray());
    private static JsonElement Json(string value) => JsonDocument.Parse(value).RootElement.Clone();
    private static HttpResponseMessage Reply(string value, HttpStatusCode status = HttpStatusCode.OK) =>
        new(status) { Content = new StringContent(value) };
    private sealed class Stub(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> send) : HttpMessageHandler
    {
        private int calls;
        public int Calls => Volatile.Read(ref calls);
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        { Interlocked.Increment(ref calls); return send(request, token); }
    }
    private sealed class TestGate(CoexistenceAccessDecision decision) : ICoexistenceAccessGate
    {
        public ValueTask<CoexistenceAccessDecision> AuthorizeAsync(HttpContext context, CoexistenceRoute route, CancellationToken token) =>
            ValueTask.FromResult(decision);
    }
    private sealed class CaptureLogger : ILogger<LegacyForwarder>
    {
        public List<string> Messages { get; } = [];
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel level) => true;
        public void Log<TState>(LogLevel level, EventId id, TState state, Exception? error, Func<TState, Exception?, string> formatter) =>
            Messages.Add(formatter(state, error));
    }
    private sealed class Lifetime : IHttpRequestLifetimeFeature
    {
        public CancellationToken RequestAborted { get; set; }
        public bool Aborted { get; private set; }
        public void Abort() => Aborted = true;
    }
    private sealed class StartedResponse : IHttpResponseFeature
    {
        public int StatusCode { get; set; } = 200;
        public string? ReasonPhrase { get; set; }
        public IHeaderDictionary Headers { get; set; } = new HeaderDictionary();
        public Stream Body { get; set; } = new MemoryStream();
        public bool HasStarted => true;
        public void OnStarting(Func<object, Task> callback, object state) { }
        public void OnCompleted(Func<object, Task> callback, object state) { }
    }
    private class ChunkStream(string value) : Stream
    {
        private readonly byte[] bytes = Encoding.UTF8.GetBytes(value);
        private int position;
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            var count = Math.Min(buffer.Length, bytes.Length - position);
            bytes.AsMemory(position, count).CopyTo(buffer); position += count;
            return ValueTask.FromResult(count);
        }
        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override void Flush() => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    }
    private sealed class InterruptedStream() : ChunkStream("partial")
    {
        private bool returned;
        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            if (returned) throw new IOException("PRIVATE_SENTINEL");
            returned = true; return base.ReadAsync(buffer, cancellationToken);
        }
    }
}

// Rehearsal controller is discovered only when a test explicitly adds this assembly.
[ApiController]
public sealed class P06StubController : ControllerBase
{
    [HttpPost("/api/p06-stub/operation")]
    public IActionResult Operation() => Content("dotnet");
}
