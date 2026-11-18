using System.ComponentModel.DataAnnotations;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Health;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class PostgresFoundationTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    [Fact]
    public async Task Disabled_database_has_no_pool_and_keeps_liveness_available()
    {
        await using var app = CreateApp(options =>
        {
            options.Enabled = false;
            options.ConnectionString = "deliberately invalid and unused";
        });
        using var client = app.CreateClient();
        Assert.Null(app.Services.GetRequiredService<PostgresDataSourceFactory>().DataSource);
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/healthz", TestCancellation)).StatusCode);
        await AssertHealthAsync(client, HttpStatusCode.ServiceUnavailable, "database_not_configured");
    }

    [Fact]
    public async Task Healthy_database_returns_ready_and_does_not_create_application_tables()
    {
        await using var app = CreateApp();
        using var client = app.CreateClient();
        await AssertHealthAsync(client, HttpStatusCode.OK);
        var pool = app.Services.GetRequiredService<PostgresDataSourceFactory>();
        Assert.Same(pool, app.Services.GetRequiredService<PostgresDataSourceFactory>());
        await using var connection = await pool.DataSource!.OpenConnectionAsync(TestCancellation);
        await using var command = new NpgsqlCommand(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'", connection);
        Assert.Equal(0L, await command.ExecuteScalarAsync(TestCancellation));
    }

    [Fact]
    public async Task Application_readiness_requires_enabled_shell_and_healthy_database()
    {
        await using (var app = CreateApp())
        {
            using var client = app.CreateClient();
            await AssertApplicationHealthAsync(client, HttpStatusCode.ServiceUnavailable, "application_shell_not_configured");
        }

        await using (var app = CreateApp(configureShell: shell => shell.Enabled = true,
            configureAuthentication: auth =>
        {
            auth.Enabled = true;
            auth.PublicOrigin = "http://127.0.0.1:5080";
        }, configureUsers: users => users.Enabled = true,
            configureClients: clients => clients.Enabled = true))
        {
            using var client = app.CreateClient();
            await AssertApplicationHealthAsync(client, HttpStatusCode.OK);
            using var home = await client.GetAsync("/", TestCancellation);
            Assert.Equal(HttpStatusCode.OK, home.StatusCode);
            Assert.Equal("text/html", home.Content.Headers.ContentType?.MediaType);
        }
    }

    [Fact]
    public async Task Unreachable_database_returns_sanitized_503_while_liveness_stays_200()
    {
        // Hold a local TCP port without accepting a PostgreSQL handshake. This
        // reliably exercises the deadline without touching another service.
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        await using var app = CreateApp(options =>
        {
            var connection = new NpgsqlConnectionStringBuilder(fixture.ConnectionString)
            {
                Port = port, Password = "PRIVATE_PASSWORD_SENTINEL"
            };
            options.ConnectionString = connection.ConnectionString;
            options.ReadinessTimeoutSeconds = 1;
        });
        using var client = app.CreateClient();
        var watch = Stopwatch.StartNew();
        await AssertHealthAsync(client, HttpStatusCode.ServiceUnavailable, "database_timeout");
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(6));
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/healthz", TestCancellation)).StatusCode);
    }

    [Fact]
    public async Task Authentication_failure_is_sanitized()
    {
        await using var app = CreateApp(options =>
        {
            var connection = new NpgsqlConnectionStringBuilder(fixture.ConnectionString)
            {
                Password = "PRIVATE_PASSWORD_SENTINEL"
            };
            options.ConnectionString = connection.ConnectionString;
        });
        using var client = app.CreateClient();
        await AssertHealthAsync(client, HttpStatusCode.ServiceUnavailable, "database_unavailable");
    }

    [Fact]
    public async Task Exhausted_pool_times_out_then_recovers_after_lease_disposal()
    {
        await using var app = CreateApp(options =>
        {
            options.MaximumPoolSize = 1;
            options.ReadinessTimeoutSeconds = 1;
        });
        using var client = app.CreateClient();
        var source = app.Services.GetRequiredService<PostgresDataSourceFactory>().DataSource!;
        await using (var held = await source.OpenConnectionAsync(TestCancellation))
        {
            var watch = Stopwatch.StartNew();
            await AssertHealthAsync(client, HttpStatusCode.ServiceUnavailable, "database_timeout");
            Assert.True(watch.Elapsed < TimeSpan.FromSeconds(6));
            Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/healthz", TestCancellation)).StatusCode);
        }
        await AssertHealthAsync(client, HttpStatusCode.OK);
    }

    [Fact]
    public async Task Pool_reuses_connection_resets_state_and_honors_application_limits()
    {
        await using var pool = CreatePool(settings =>
        {
            settings.MaximumPoolSize = 1;
            settings.ConnectionTimeoutSeconds = 2;
            settings.CommandTimeoutSeconds = 4;
            settings.ConnectionString += ";Pooling=false;Maximum Pool Size=50;Timeout=0;Command Timeout=0;No Reset On Close=true;Include Error Detail=true;Log Parameters=true";
        });
        int backend;
        await using (var connection = await pool.DataSource!.OpenConnectionAsync(TestCancellation))
        {
            backend = connection.ProcessID;
            await using var mutate = new NpgsqlCommand("SET application_name = 'fixture_changed'", connection);
            await mutate.ExecuteNonQueryAsync(TestCancellation);
        }
        await using var reused = await pool.DataSource!.OpenConnectionAsync(TestCancellation);
        Assert.Equal(backend, reused.ProcessID);
        await using var command = new NpgsqlCommand("SHOW application_name", reused);
        Assert.Equal("AuditPlatform.Api", await command.ExecuteScalarAsync(TestCancellation));
        Assert.Equal(4, command.CommandTimeout);
        var settings = new NpgsqlConnectionStringBuilder(pool.DataSource.ConnectionString);
        Assert.True(settings.Pooling);
        Assert.Equal(1, settings.MaxPoolSize);
        Assert.Equal(2, settings.Timeout);
        Assert.False(settings.IncludeErrorDetail);
        Assert.False(settings.LogParameters);
        Assert.False(settings.NoResetOnClose);
    }

    [Fact]
    public async Task Query_timeout_does_not_strand_the_pool()
    {
        await using var pool = CreatePool(settings => settings.MaximumPoolSize = 1);
        await using (var connection = await pool.DataSource!.OpenConnectionAsync(TestCancellation))
        {
            await using var slow = new NpgsqlCommand("SELECT pg_sleep(10)", connection) { CommandTimeout = 1 };
            await Assert.ThrowsAsync<NpgsqlException>(async () => await slow.ExecuteScalarAsync(TestCancellation));
        }
        var check = new PostgresReadinessCheck(pool, Options.Create(Settings()));
        Assert.Null(await check.CheckAsync(TestCancellation));
    }

    [Fact]
    public async Task Caller_cancellation_propagates_and_releases_pool_capacity()
    {
        await using var pool = CreatePool(settings => settings.MaximumPoolSize = 1);
        var check = new PostgresReadinessCheck(pool, Options.Create(Settings()));
        await using (var held = await pool.DataSource!.OpenConnectionAsync(TestCancellation))
        {
            using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(TestCancellation);
            cancellation.CancelAfter(TimeSpan.FromMilliseconds(100));
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => check.CheckAsync(cancellation.Token));
        }
        Assert.Null(await check.CheckAsync(TestCancellation));
    }

    [Fact]
    public async Task Disposing_pool_prevents_new_leases()
    {
        var pool = CreatePool();
        await using (var connection = await pool.DataSource!.OpenConnectionAsync(TestCancellation))
            Assert.True(connection.ProcessID > 0);
        await pool.DisposeAsync();
        await Assert.ThrowsAsync<ObjectDisposedException>(async () => await pool.DataSource!.OpenConnectionAsync(TestCancellation));
    }

    [Fact]
    public async Task Parameterized_values_preserve_decimal_and_treat_sql_as_data()
    {
        await using var pool = CreatePool();
        await using var connection = await pool.DataSource!.OpenConnectionAsync(TestCancellation);
        const decimal amount = 123456789012345.6789012345678m;
        const string text = "'; DROP TABLE client_data; --";
        await using var command = new NpgsqlCommand("SELECT $1::numeric, $2::text", connection);
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Numeric, Value = amount });
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Text, Value = text });
        await using var reader = await command.ExecuteReaderAsync(TestCancellation);
        Assert.True(await reader.ReadAsync(TestCancellation));
        Assert.Equal(amount, reader.GetDecimal(0));
        Assert.Equal(text, reader.GetString(1));
        Assert.False(await reader.ReadAsync(TestCancellation));
    }

    [Theory]
    [InlineData("MaximumPoolSize", 0)]
    [InlineData("MaximumPoolSize", 101)]
    [InlineData("ConnectionTimeoutSeconds", 0)]
    [InlineData("ConnectionTimeoutSeconds", 31)]
    [InlineData("CommandTimeoutSeconds", 0)]
    [InlineData("CommandTimeoutSeconds", 301)]
    [InlineData("ReadinessTimeoutSeconds", 0)]
    [InlineData("ReadinessTimeoutSeconds", 16)]
    public void Invalid_limits_are_rejected(string property, int value)
    {
        var settings = Settings();
        typeof(PostgresOptions).GetProperty(property)!.SetValue(settings, value);
        Assert.False(IsValid(settings));
    }

    [Theory]
    [InlineData("")]
    [InlineData("Host=127.0.0.1;Username=test")]
    [InlineData("Host=127.0.0.1;Database=test")]
    [InlineData("Database=test;Username=test")]
    [InlineData("Host=remote.example;Database=test;Username=test;SSL Mode=Require")]
    [InlineData("Host=localhost,remote.example;Database=test;Username=test")]
    [InlineData("PRIVATE_PASSWORD_SENTINEL=invalid")]
    public void Missing_invalid_or_insecure_settings_are_rejected_without_echoing_values(string connectionString)
    {
        var settings = Settings();
        settings.ConnectionString = connectionString;
        var failures = new List<ValidationResult>();
        Assert.False(Validator.TryValidateObject(settings, new(settings), failures, true));
        Assert.All(failures, failure => Assert.DoesNotContain("PRIVATE_PASSWORD_SENTINEL", failure.ErrorMessage!));
    }

    [Fact]
    public void Remote_tls_and_disabled_settings_validate_without_connecting()
    {
        var settings = Settings();
        settings.ConnectionString = "Host=remote.example;Database=test;Username=test;SSL Mode=VerifyFull";
        Assert.True(IsValid(settings));
        settings.Enabled = false;
        settings.ConnectionString = "unused";
        settings.MaximumPoolSize = 0;
        Assert.True(IsValid(settings));
    }

    [Fact]
    public void Invalid_database_settings_prevent_host_startup()
    {
        var app = CreateApp(options => options.MaximumPoolSize = 0);
        // The entry point handles startup validation by returning exit code 1;
        // WebApplicationFactory must therefore refuse to expose a live server.
        try { Assert.ThrowsAny<Exception>(() => app.CreateClient()); }
        finally
        {
            // A factory whose host failed during startup has no hosted-service
            // collection for DisposeAsync to stop. Startup rejection is the
            // behavior under test; don't let this test-host cleanup bug mask it.
            try { app.Dispose(); }
            catch (ArgumentNullException) { }
        }
    }

    [Theory]
    [InlineData("remote.example", "auditplatform_foundation_11111111111111111111111111111111", "auditplatform_test", false)]
    [InlineData("127.0.0.1", "postgres", "auditplatform_test", false)]
    [InlineData("127.0.0.1", "auditplatform_foundation_notaguid", "auditplatform_test", false)]
    [InlineData("127.0.0.1", "auditplatform_foundation_11111111111111111111111111111111", "postgres", false)]
    [InlineData("127.0.0.1", "auditplatform_foundation_11111111111111111111111111111111", "auditplatform_test", true)]
    public void Test_guard_rejects_external_or_unowned_targets(string host, string database, string user, bool wrongPort)
    {
        var connection = new NpgsqlConnectionStringBuilder(fixture.ConnectionString)
        {
            Host = host, Database = database, Username = user, Port = wrongPort ? fixture.Port + 1 : fixture.Port
        };
        Assert.Throws<InvalidOperationException>(() => PostgresFixture.RequireSafeTarget(connection.ConnectionString, fixture.Port));
    }

    private static CancellationToken TestCancellation => TestContext.Current.CancellationToken;

    private PostgresOptions Settings() => new()
    {
        Enabled = true, ConnectionString = fixture.ConnectionString
    };

    private PostgresDataSourceFactory CreatePool(Action<PostgresOptions>? configure = null)
    {
        PostgresFixture.RequireSafeTarget(fixture.ConnectionString, fixture.Port);
        var settings = Settings();
        configure?.Invoke(settings);
        return new(Options.Create(settings));
    }

    private WebApplicationFactory<Program> CreateApp(Action<PostgresOptions>? configure = null,
        Action<ApplicationShellOptions>? configureShell = null, Action<AuthenticationOptions>? configureAuthentication = null,
        Action<UserManagementOptions>? configureUsers = null, Action<ClientManagementOptions>? configureClients = null)
    {
        PostgresFixture.RequireSafeTarget(fixture.ConnectionString, fixture.Port);
        return new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseEnvironment("Testing");
            builder.ConfigureServices(services => services.PostConfigure<PostgresOptions>(options =>
            {
                options.Enabled = true;
                options.ConnectionString = fixture.ConnectionString;
                options.MaximumPoolSize = 2;
                options.ConnectionTimeoutSeconds = 2;
                options.CommandTimeoutSeconds = 5;
                options.ReadinessTimeoutSeconds = 2;
                configure?.Invoke(options);
            }));
            builder.ConfigureServices(services => services.PostConfigure<ApplicationShellOptions>(options => configureShell?.Invoke(options)));
            builder.ConfigureServices(services => services.PostConfigure<AuthenticationOptions>(options => configureAuthentication?.Invoke(options)));
            builder.ConfigureServices(services => services.PostConfigure<UserManagementOptions>(options => configureUsers?.Invoke(options)));
            builder.ConfigureServices(services => services.PostConfigure<ClientManagementOptions>(options => configureClients?.Invoke(options)));
        });
    }

    private static bool IsValid(PostgresOptions settings) =>
        Validator.TryValidateObject(settings, new(settings), new List<ValidationResult>(), true);

    private static async Task AssertHealthAsync(HttpClient client, HttpStatusCode expected, string? reason = null)
    {
        using var response = await client.GetAsync("/health/ready", TestCancellation);
        Assert.Equal(expected, response.StatusCode);
        var body = await response.Content.ReadAsStringAsync(TestCancellation);
        using var json = JsonDocument.Parse(body);
        Assert.Equal(reason is null ? "ready" : "not_ready", json.RootElement.GetProperty("status").GetString());
        if (reason is not null)
            Assert.Equal(reason, json.RootElement.GetProperty("reason").GetString());
        Assert.DoesNotContain("PRIVATE_PASSWORD_SENTINEL", body);
        Assert.DoesNotContain("127.0.0.1", body);
        Assert.DoesNotContain("auditplatform_foundation_", body);
    }

    private static async Task AssertApplicationHealthAsync(HttpClient client, HttpStatusCode expected, string? reason = null)
    {
        using var response = await client.GetAsync("/health/app-ready", TestCancellation);
        Assert.Equal(expected, response.StatusCode);
        using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync(TestCancellation));
        Assert.Equal(reason is null ? "ready" : "not_ready", json.RootElement.GetProperty("status").GetString());
        if (reason is not null) Assert.Equal(reason, json.RootElement.GetProperty("reason").GetString());
    }
}
