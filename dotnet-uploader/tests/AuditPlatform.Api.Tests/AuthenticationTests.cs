using System.ComponentModel.DataAnnotations;
using System.Net;
using System.Net.Http.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class AuthenticationTests
{
    [Theory]
    [InlineData("https://audit.example", true)]
    [InlineData("https://evil.example", false)]
    [InlineData("null", false)]
    [InlineData("", false)]
    public async Task Mutation_requires_exact_configured_origin(string origin, bool allowed)
    {
        var reached = false;
        var middleware = new CsrfMiddleware(_ => { reached = true; return Task.CompletedTask; });
        var context = new DefaultHttpContext();
        context.Request.Method = "POST";
        if (origin.Length > 0) context.Request.Headers.Origin = origin;
        await middleware.InvokeAsync(context, Options.Create(new AuthenticationOptions
            { PublicOrigin = "https://audit.example" }));
        Assert.Equal(allowed, reached);
        if (!allowed) Assert.Equal(403, context.Response.StatusCode);
    }

    [Theory]
    [InlineData("https://audit.example", true)]
    [InlineData("http://127.0.0.1:5080", true)]
    [InlineData("https://audit.example/path", false)]
    [InlineData("https://audit.example/", false)]
    [InlineData("https://user@audit.example", false)]
    public void Origin_configuration_must_be_canonical(string origin, bool valid)
    {
        var options = new AuthenticationOptions { PublicOrigin = origin };
        var results = new List<ValidationResult>();
        Assert.Equal(valid, Validator.TryValidateObject(options, new(options), results, true));
    }
}

public sealed class AuthenticationDatabaseTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>, IAsyncLifetime
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    private PostgresDataSourceFactory pool = null!;
    private PostgresSessionStore store = null!;

    public async ValueTask InitializeAsync()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var command = new NpgsqlCommand("""
            CREATE TABLE public.clients(id integer PRIMARY KEY, client_name text, client_code text);
            CREATE TABLE public.users(id integer PRIMARY KEY, username text UNIQUE NOT NULL,
                password_hash text NOT NULL, is_active boolean, role text, last_login timestamptz);
            CREATE TABLE public.sessions(token text PRIMARY KEY, user_id integer NOT NULL REFERENCES public.users(id),
                expiry timestamptz NOT NULL, selected_client_id integer REFERENCES public.clients(id));
            INSERT INTO public.clients VALUES (5,'Synthetic client','SYN');
            INSERT INTO public.users(id,username,password_hash,is_active,role)
                VALUES (7,'synthetic','$2b$04$abcdefghijklmnopqrstuunCb8AcUlO2CukFVuQ5U9hCvDhIsHd22',true,'admin');
            """, connection);
        await command.ExecuteNonQueryAsync(Token);
        pool = fixture.CreatePool();
        var limits = Options.Create(new LogPersistenceOptions());
        store = new(new PostgresTransactions(pool, Options.Create(new PostgresOptions())),
            new PostgresAuditWriter(new LogSanitizer(limits), limits));
    }

    [Fact]
    public async Task Bcrypt_compatible_hash_session_expiry_deactivation_and_logout()
    {
        Assert.Equal("invalid", (await store.LoginAsync("synthetic", "wrong", Token)).Result);
        Assert.Equal("invalid", (await store.LoginAsync("missing", "wrong", Token)).Result);
        var login = await store.LoginAsync("synthetic", "synthetic-password", Token);
        Assert.Equal("ok", login.Result);
        Assert.Matches("^[0-9a-f]{64}$", login.Token!);
        var session = await store.FindAsync(login.Token!, Token);
        Assert.NotNull(session);
        Assert.Equal("admin", session.Role);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using (var existing = new NpgsqlCommand("INSERT INTO public.sessions VALUES ($1,7,now()+interval '1 hour',5)", connection))
        {
            existing.Parameters.AddWithValue(new string('b', 64));
            await existing.ExecuteNonQueryAsync(Token);
        }
        Assert.Equal("Synthetic client", (await store.FindAsync(new string('b', 64), Token))!.ClientName);
        await using (var update = new NpgsqlCommand("UPDATE public.sessions SET selected_client_id=5 WHERE token=$1", connection))
        {
            update.Parameters.AddWithValue(login.Token!); await update.ExecuteNonQueryAsync(Token);
        }
        session = await store.FindAsync(login.Token!, Token);
        Assert.Equal("Synthetic client", session!.ClientName);
        await using (var disable = new NpgsqlCommand("UPDATE public.users SET is_active=false WHERE id=7", connection))
            await disable.ExecuteNonQueryAsync(Token);
        Assert.Null(await store.FindAsync(login.Token!, Token));
        Assert.Equal("inactive", (await store.LoginAsync("synthetic", "synthetic-password", Token)).Result);
        await using (var enable = new NpgsqlCommand("UPDATE public.users SET is_active=true WHERE id=7", connection))
            await enable.ExecuteNonQueryAsync(Token);
        await store.LogoutAsync(login.Token!, Token);
        Assert.Null(await store.FindAsync(login.Token!, Token));
        await using var expire = new NpgsqlCommand("INSERT INTO public.sessions VALUES ($1,7,now()-interval '1 second',NULL)", connection);
        expire.Parameters.AddWithValue(new string('a', 64));
        await expire.ExecuteNonQueryAsync(Token);
        Assert.Null(await store.FindAsync(new string('a', 64), Token));
        Assert.True(await store.IsExpiredAsync(new string('a', 64), Token));

        await using var app = new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseEnvironment("Testing");
            builder.ConfigureServices(services =>
            {
                services.PostConfigure<PostgresOptions>(settings =>
                {
                    settings.Enabled = true; settings.ConnectionString = fixture.ConnectionString;
                });
                services.PostConfigure<AuthenticationOptions>(settings =>
                {
                    settings.Enabled = true; settings.PublicOrigin = "http://127.0.0.1:5080";
                });
            });
        });
        using var client = app.CreateClient();
        using var blocked = await client.PostAsJsonAsync("/api/auth/login",
            new { username = "synthetic", password = "synthetic-password" }, Token);
        Assert.Equal(HttpStatusCode.Forbidden, blocked.StatusCode);
        client.DefaultRequestHeaders.Add("Origin", "http://127.0.0.1:5080");
        using var response = await client.PostAsJsonAsync("/api/auth/login",
            new { username = "synthetic", password = "synthetic-password" }, Token);
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Contains("httponly", response.Headers.GetValues("Set-Cookie").Single(), StringComparison.OrdinalIgnoreCase);
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/api/auth/current-client", Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await client.PostAsync("/api/auth/logout", null, Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, (await client.GetAsync("/api/auth/current-client", Token)).StatusCode);
    }

    public ValueTask DisposeAsync()
    {
        pool.Dispose();
        return ValueTask.CompletedTask;
    }
}
