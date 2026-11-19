using System.ComponentModel.DataAnnotations;
using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Middleware;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class DiagnosticErrorTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>, IAsyncLifetime
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    private static JsonElement Json(string value) => JsonDocument.Parse(value).RootElement.Clone();
    private static DiagnosticError Record() => new(Guid.NewGuid(), Guid.NewGuid().ToString("N"), LogActor.Anonymous,
        null, "Tests", "/test/{id}", "POST", 500, "System", "Unhandled test exception.", "Safe.TestMethod\nInvalidOperationException",
        Json("{\"row_count\":3,\"password\":\"PRIVATE_SENTINEL\",\"nested\":{\"token\":\"PRIVATE_SENTINEL\"}}"));
    private static LogSanitizer Sanitizer(LogPersistenceOptions? options = null) => new(Options.Create(options ?? new()));
    private static DiagnosticErrorLogger Logger(PostgresDataSourceFactory pool, CaptureLogger output, int seconds = 2) =>
        new(new PostgresTransactions(pool, Options.Create(new PostgresOptions { CommandTimeoutSeconds = 3 })), pool,
            Sanitizer(), Options.Create(new LogPersistenceOptions { DiagnosticTimeoutSeconds = seconds }), output);

    public async ValueTask InitializeAsync() => await fixture.ApplyAuditSchemaAsync(Token);
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

    [Fact]
    public async Task Diagnostic_persists_real_id_sanitized_metadata_and_registry_reference()
    {
        await using var pool = fixture.CreatePool();
        var output = new CaptureLogger();
        var record = Record();
        var result = await Logger(pool, output).LogAsync(record, Token);
        Assert.NotNull(result.ErrorId);
        Assert.Equal("persisted", result.Persistence);
        Assert.Empty(output.Messages);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var command = new NpgsqlCommand("""
            SELECT e.input_data::text,e.actor_kind,e.actor_user_id,e.user_id,e.stack_trace,e.file_name,e.code_context,
                e.error_code_id=c.id,e.event_id FROM public.error_log e
            JOIN public.error_codes c ON c.code=e.error_code WHERE e.id=$1
            """, connection);
        command.Parameters.AddWithValue(result.ErrorId.Value);
        await using var reader = await command.ExecuteReaderAsync(Token);
        Assert.True(await reader.ReadAsync(Token));
        var metadata = reader.GetString(0);
        Assert.Equal(3, Json(metadata).GetProperty("row_count").GetInt32());
        Assert.True(Json(metadata).GetProperty("_omitted").GetBoolean());
        Assert.DoesNotContain("PRIVATE_SENTINEL", metadata);
        Assert.Equal("anonymous", reader.GetString(1));
        Assert.True(reader.IsDBNull(2));
        Assert.Equal("Anonymous", reader.GetString(3));
        Assert.EndsWith("InvalidOperationException", reader.GetString(4));
        Assert.True(reader.IsDBNull(5));
        Assert.True(reader.IsDBNull(6));
        Assert.True(reader.GetBoolean(7));
        Assert.Equal(record.EventId, reader.GetGuid(8));
    }

    [Theory]
    [InlineData("error_codes")]
    [InlineData("error_log")]
    public async Task Missing_registry_or_store_returns_null_id_and_fixed_metadata_fallback(string table)
    {
        // Names are fixed InlineData, never public input.
        await ControlAsync($"ALTER TABLE public.{table} RENAME TO p05b_hidden", Token);
        try
        {
            await using var pool = fixture.CreatePool();
            var output = new CaptureLogger();
            var result = await Logger(pool, output).LogAsync(Record() with
            {
                SafeMessage = "password=PRIVATE_SENTINEL", SafeTrace = "token=PRIVATE_SENTINEL"
            }, Token);
            Assert.Null(result.ErrorId);
            Assert.Equal("unavailable", result.Persistence);
            Assert.Equal(DiagnosticErrorLogger.GenericMessage, result.UserMessage);
            Assert.Single(output.Messages);
            Assert.DoesNotContain("PRIVATE_SENTINEL", output.Messages[0]);
            Assert.DoesNotContain("password", output.Messages[0], StringComparison.OrdinalIgnoreCase);
        }
        finally { await ControlAsync($"ALTER TABLE public.p05b_hidden RENAME TO {table}", Token); }
    }

    [Fact]
    public async Task Diagnostic_is_independent_of_a_rolled_back_business_transaction()
    {
        var source = await fixture.CreateSourceRowAsync(5, Token);
        await using var pool = fixture.CreatePool();
        var transactions = new PostgresTransactions(pool, Options.Create(new PostgresOptions()));
        await Assert.ThrowsAsync<InvalidOperationException>(() => transactions.ExecuteAsync<int>(async (context, token) =>
        {
            await using var change = context.CreateCommand("UPDATE public.client_data SET row_data='{\"failed\":true}'::jsonb WHERE id=$1");
            change.Parameters.AddWithValue(source);
            await change.ExecuteNonQueryAsync(token);
            throw new InvalidOperationException("Original business failure");
        }, Token));
        var result = await Logger(pool, new()).LogAsync(Record(), Token);
        Assert.NotNull(result.ErrorId);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var verify = new NpgsqlCommand("SELECT row_data ? 'failed' FROM public.client_data WHERE id=$1", connection);
        verify.Parameters.AddWithValue(source);
        Assert.False((bool)(await verify.ExecuteScalarAsync(Token))!);
    }

    [Fact]
    public async Task Exhausted_pool_respects_diagnostic_budget_and_recovers()
    {
        await using var pool = fixture.CreatePool(maximumPoolSize: 1);
        var output = new CaptureLogger();
        var logger = Logger(pool, output, seconds: 1);
        await using (var held = await pool.DataSource!.OpenConnectionAsync(Token))
        {
            var clock = Stopwatch.StartNew();
            var result = await logger.LogAsync(Record(), Token);
            Assert.Null(result.ErrorId);
            Assert.True(clock.Elapsed < TimeSpan.FromSeconds(5));
            Assert.Single(output.Messages);
        }
        Assert.NotNull((await logger.LogAsync(Record(), Token)).ErrorId);
    }

    [Fact]
    public async Task Lost_diagnostic_commit_ack_reports_unknown_without_claiming_a_record_id_or_replaying()
    {
        await using var relay = fixture.CreateCommitProxy();
        await using var pool = fixture.CreatePool(proxy: relay);
        var record = Record();
        relay.Arm();
        var result = await Logger(pool, new()).LogAsync(record, Token);
        await relay.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5), Token);
        Assert.Null(result.ErrorId);
        Assert.Equal("unknown", result.Persistence);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var count = new NpgsqlCommand("SELECT count(*) FROM public.error_log WHERE event_id=$1", connection);
        count.Parameters.AddWithValue(record.EventId);
        Assert.Equal(1L, await count.ExecuteScalarAsync(Token));
    }

    [Fact]
    public void Capture_ignores_request_values_and_requires_typed_authorized_scope()
    {
        using var pool = fixture.CreatePool();
        var context = new DefaultHttpContext();
        context.Request.Path = "/PRIVATE_SENTINEL";
        context.Request.QueryString = new("?password=PRIVATE_SENTINEL&client_id=999");
        context.Request.Headers.Authorization = "Bearer PRIVATE_SENTINEL";
        context.Request.Headers.Cookie = "session_token=PRIVATE_SENTINEL";
        context.Request.Body = new MemoryStream(Encoding.UTF8.GetBytes("{\"password\":\"PRIVATE_SENTINEL\"}"));
        context.Items[typeof(AuthorizedLogScope)] = new AuthorizedLogScope(5, Guid.NewGuid());
        var captured = Logger(pool, new()).Capture(new InvalidOperationException("password=PRIVATE_SENTINEL"), context, Guid.NewGuid().ToString("N"));
        Assert.Equal(5, captured.ClientId);
        Assert.NotEqual(999, captured.ClientId);
        Assert.Equal("unmatched", captured.Endpoint);
        Assert.Equal(LogActor.Anonymous, captured.Actor);
        Assert.Null(captured.Metadata);
        Assert.Equal(0, context.Request.Body.Position);
        Assert.DoesNotContain("PRIVATE_SENTINEL", JsonSerializer.Serialize(captured));
    }

    [Theory]
    [InlineData("Production", true)]
    [InlineData("Development", true)]
    [InlineData("Production", false)]
    public async Task Http_errors_preserve_safe_envelope_and_do_not_expose_inputs(string environment, bool databaseEnabled)
    {
        await using var app = App(environment, databaseEnabled);
        using var client = app.CreateClient();
        using var request = new HttpRequestMessage(HttpMethod.Post, "/p05b-test/fail/PRIVATE_SENTINEL?password=PRIVATE_SENTINEL");
        request.Headers.Add("Cookie", "session_token=PRIVATE_SENTINEL");
        request.Headers.Authorization = new("Bearer", "PRIVATE_SENTINEL");
        request.Content = new StringContent("{\"password\":\"PRIVATE_SENTINEL\"}", Encoding.UTF8, "application/json");
        using var response = await client.SendAsync(request, Token);
        Assert.Equal(HttpStatusCode.InternalServerError, response.StatusCode);
        var body = await response.Content.ReadAsStringAsync(Token);
        Assert.DoesNotContain("PRIVATE_SENTINEL", body);
        Assert.DoesNotContain("InvalidOperationException", body);
        var json = Json(body);
        Assert.False(json.GetProperty("success").GetBoolean());
        var error = json.GetProperty("error");
        Assert.Equal(DiagnosticErrorLogger.UnknownCode, error.GetProperty("error_code").GetString());
        Assert.Equal(JsonValueKind.Null, error.GetProperty("technical_details").ValueKind);
        Assert.Equal(databaseEnabled ? JsonValueKind.Number : JsonValueKind.Null, error.GetProperty("error_id").ValueKind);
        Assert.Equal("no-store", response.Headers.CacheControl!.ToString());
        using var ordinary = await client.GetAsync("/p05b-test/validation", Token);
        Assert.Equal(HttpStatusCode.BadRequest, ordinary.StatusCode);
        Assert.Equal("{\"detail\":\"Expected validation result\"}", await ordinary.Content.ReadAsStringAsync(Token));
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/healthz", Token)).StatusCode);
        if (databaseEnabled)
        {
            await using var connection = await fixture.OpenControlConnectionAsync(Token);
            await using var verify = new NpgsqlCommand("SELECT endpoint,stack_trace,user_id FROM public.error_log WHERE id=$1", connection);
            verify.Parameters.AddWithValue(error.GetProperty("error_id").GetInt32());
            await using var reader = await verify.ExecuteReaderAsync(Token);
            Assert.True(await reader.ReadAsync(Token));
            Assert.Equal("/p05b-test/fail/{id}", reader.GetString(0));
            Assert.Contains(nameof(P05bFaultController.Fail), reader.GetString(1));
            Assert.DoesNotContain("PRIVATE_SENTINEL", reader.GetString(1));
            Assert.Equal("Anonymous", reader.GetString(2));
        }
    }

    [Fact]
    public async Task Started_response_is_aborted_without_appending_a_second_reply()
    {
        await using var pool = fixture.CreatePool();
        var context = new DefaultHttpContext();
        var feature = new StartedResponseFeature();
        context.Features.Set<IHttpResponseFeature>(feature);
        var lifetime = new LifetimeFeature();
        context.Features.Set<IHttpRequestLifetimeFeature>(lifetime);
        var middleware = new ExceptionLoggingMiddleware(_ => throw new InvalidOperationException("password=PRIVATE_SENTINEL"));
        await middleware.InvokeAsync(context, Logger(pool, new()),
            NullLogger<ExceptionLoggingMiddleware>.Instance, new TestHostEnvironment());
        Assert.True(lifetime.Aborted);
        Assert.Equal(0, feature.Body.Length);
    }

    [Fact]
    public async Task Cancelled_request_is_not_converted_to_an_error_record_or_500()
    {
        await using var pool = fixture.CreatePool();
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(Token);
        await cancellation.CancelAsync();
        var context = new DefaultHttpContext { RequestAborted = cancellation.Token };
        var output = new CaptureLogger();
        await new ExceptionLoggingMiddleware(_ => throw new OperationCanceledException(cancellation.Token))
            .InvokeAsync(context, Logger(pool, output), NullLogger<ExceptionLoggingMiddleware>.Instance,
                new TestHostEnvironment());
        Assert.Empty(output.Messages);
        Assert.NotEqual(500, context.Response.StatusCode);
    }

    [Fact]
    public void Sanitization_uses_allowlisted_values_and_preserves_valid_bounded_json()
    {
        var sanitizer = Sanitizer(new() { MetadataMaxBytes = 64 });
        var metadata = Json("{\"row_count\":3,\"outcome\":\"PRIVATE_SENTINEL\",\"password\":\"PRIVATE_SENTINEL\",\"nested\":{\"cookie\":\"PRIVATE_SENTINEL\"}}");
        var safe = sanitizer.Metadata(metadata)!;
        Assert.True(Encoding.UTF8.GetByteCount(safe) <= 64);
        Assert.Equal(JsonValueKind.Object, Json(safe).ValueKind);
        Assert.DoesNotContain("PRIVATE_SENTINEL", safe);
        Assert.Equal(LogSanitizer.Redacted, sanitizer.Text("Authorization: Bearer PRIVATE_SENTINEL", 1000));
        Assert.Equal("𐀀𐀀", sanitizer.Text("𐀀𐀀𐀀", 2));
        var fields = new Dictionary<string, long>
        {
            ["record_id"] = 1, ["target_user_id"] = 2, ["client_data_id"] = 3, ["retry_count"] = 4,
            ["row_count"] = 5, ["template_id"] = 6, ["upload_id"] = 7
        };
        Assert.Equal("{\"_truncated\":true}", sanitizer.Metadata(Json(JsonSerializer.Serialize(fields))));
    }

    [Fact]
    public void Compact_audit_redacts_unapproved_values_and_rejects_unsupported_size_or_depth()
    {
        var audit = new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), LogActor.System, 5, "UPDATE", "Tests");
        var record = new DataChangeAudit(audit, 1, Json("{\"field\":\"private old\"}"), Json("{\"field\":\"private new\"}"), new HashSet<string>());
        var result = Sanitizer().Compact(record);
        Assert.Equal(LogSanitizer.Redacted, Json(result.New!).GetProperty("field").GetString());
        var deep = new DataChangeAudit(audit, 1, null, Json("{\"field\":{\"a\":{\"b\":3}}}"), new HashSet<string> { "field" });
        Assert.Throws<ArgumentException>(() => Sanitizer(new() { MetadataMaxDepth = 1 }).Compact(deep));
        var big = record with { NewRow = Json(JsonSerializer.Serialize(new { field = new string('x', 1001) })), ValueFields = new HashSet<string> { "field" } };
        Assert.Throws<ArgumentException>(() => Sanitizer().Compact(big));
    }

    [Theory]
    [InlineData("DiagnosticTimeoutSeconds", 0)]
    [InlineData("DiagnosticTimeoutSeconds", 11)]
    [InlineData("MetadataMaxBytes", 5001)]
    [InlineData("MetadataMaxDepth", 17)]
    [InlineData("AuditDetailsMaxCharacters", 1001)]
    public void Invalid_logging_limits_are_rejected(string property, int value)
    {
        var settings = new LogPersistenceOptions();
        typeof(LogPersistenceOptions).GetProperty(property)!.SetValue(settings, value);
        Assert.False(Validator.TryValidateObject(settings, new(settings), new List<ValidationResult>(), true));
    }

    private WebApplicationFactory<Program> App(string environment, bool enabled) =>
        new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseEnvironment(environment);
            builder.ConfigureServices(services =>
            {
                services.PostConfigure<PostgresOptions>(options =>
                {
                    options.Enabled = enabled;
                    options.ConnectionString = fixture.ConnectionString;
                    options.MaximumPoolSize = 2;
                });
                // Fault routes exist only in the test assembly/application part.
                services.AddControllers().AddApplicationPart(typeof(P05bFaultController).Assembly);
            });
        });

    private async Task ControlAsync(string sql, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(token);
    }

    public sealed class CaptureLogger : ILogger<DiagnosticErrorLogger>
    {
        public List<string> Messages { get; } = [];
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel level) => true;
        public void Log<TState>(LogLevel level, EventId id, TState state, Exception? error, Func<TState, Exception?, string> formatter) =>
            Messages.Add(formatter(state, error) + (error?.ToString() ?? ""));
    }

    private sealed class StartedResponseFeature : IHttpResponseFeature
    {
        public int StatusCode { get; set; } = 200;
        public string? ReasonPhrase { get; set; }
        public IHeaderDictionary Headers { get; set; } = new HeaderDictionary();
        public Stream Body { get; set; } = new MemoryStream();
        public bool HasStarted => true;
        public void OnStarting(Func<object, Task> callback, object state) { }
        public void OnCompleted(Func<object, Task> callback, object state) { }
    }

    private sealed class LifetimeFeature : IHttpRequestLifetimeFeature
    {
        public CancellationToken RequestAborted { get; set; }
        public bool Aborted { get; private set; }
        public void Abort() => Aborted = true;
    }

    private sealed class TestHostEnvironment : IHostEnvironment
    {
        public string EnvironmentName { get; set; } = Environments.Production;
        public string ApplicationName { get; set; } = nameof(DiagnosticErrorTests);
        public string ContentRootPath { get; set; } = AppContext.BaseDirectory;
        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
    }
}

[ApiController]
public sealed class P05bFaultController : ControllerBase
{
    [HttpPost("/p05b-test/fail/{id}")]
    public IActionResult Fail(string id) => throw new InvalidOperationException("password=PRIVATE_SENTINEL SQL parameters PRIVATE_SENTINEL");

    [HttpGet("/p05b-test/validation")]
    public IActionResult ValidationResult() => BadRequest(new { detail = "Expected validation result" });
}
