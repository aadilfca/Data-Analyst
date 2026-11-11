using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Users;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TenantUserManagementTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;

    [Fact]
    public async Task Tenant_user_operations_preserve_scope_identity_and_audit_atomicity()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await ExecuteAsync(connection, """
            CREATE TABLE public.users(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE, role TEXT, last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY, client_name TEXT, client_code TEXT);
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES public.users(id),
                expiry TIMESTAMPTZ NOT NULL, selected_client_id INTEGER REFERENCES public.clients(id));
            INSERT INTO public.users(id,username,password_hash,is_active,role) OVERRIDING SYSTEM VALUE VALUES
                (7,'admin-a','hash',TRUE,NULL),(8,'admin-b','hash',TRUE,NULL),
                (9,'shared','hash',TRUE,NULL),(10,'member','hash',TRUE,NULL),(11,'platform','hash',TRUE,'admin');
            SELECT setval(pg_get_serial_sequence('public.users','id'),50,true);
            INSERT INTO public.sessions VALUES
                (repeat('a',64),7,now()+interval '1 hour',NULL),
                (repeat('b',64),8,now()+interval '1 hour',NULL),
                (repeat('c',64),9,now()+interval '1 hour',NULL),
                (repeat('d',64),10,now()+interval '1 hour',NULL),
                (repeat('e',64),7,now()-interval '1 hour',NULL);
            """);
        await fixture.ApplyTenantFoundationAsync(Token);
        await using (var rollback = await connection.BeginTransactionAsync(Token))
        {
            var sql = await PostgresFixture.ReadTenantUserRolesAsync(Token);
            await using var candidate = new NpgsqlCommand(sql, connection, rollback);
            await candidate.ExecuteNonQueryAsync(Token);
            await rollback.RollbackAsync(Token);
        }
        await using (var check = new NpgsqlCommand("""
            SELECT NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name='platform_tenant_memberships' AND column_name='tenant_role')
            """, connection)) Assert.True((bool)(await check.ExecuteScalarAsync(Token))!);
        await fixture.ApplyTenantUserRolesAsync(Token);
        var duplicateMigration = await Assert.ThrowsAsync<PostgresException>(() => fixture.ApplyTenantUserRolesAsync(Token));
        Assert.Equal(PostgresErrorCodes.DuplicateColumn,duplicateMigration.SqlState);
        await ExecuteAsync(connection, """
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES
                (101,'Firm A','audit_firm'),(102,'Firm B','audit_firm');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES
                (101,7,'admin'),(102,8,'admin'),(101,9,'member'),(102,9,'member'),
                (101,10,'member'),(101,11,'member');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id) VALUES (102,11);
            """);
        await using (var defaultRole = new NpgsqlCommand("SELECT tenant_role FROM public.platform_tenant_memberships WHERE tenant_id=102 AND user_id=11", connection))
            Assert.Equal("member",(string)(await defaultRole.ExecuteScalarAsync(Token))!);
        using var pool = fixture.CreatePool();
        var transactions = new PostgresTransactions(pool, Options.Create(new PostgresOptions()));
        var limits = Options.Create(new LogPersistenceOptions());
        var service = new TenantUserService(transactions, new PostgresAuditWriter(new LogSanitizer(limits), limits));
        var admin = Session('a',7);

        Assert.Equal(new TenantSelection(TenantUserStatus.Success,101), await service.ResolveSoleTenantAsync(admin,Token));
        Assert.Equal(TenantUserStatus.AmbiguousTenant, (await service.ResolveSoleTenantAsync(Session('c',9),Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden, (await service.ResolveSoleTenantAsync(Session('d',10),Token)).Status);

        var users = await service.ListAsync(admin,101,true,Token);
        Assert.Equal(TenantUserStatus.Success, users.Status);
        Assert.Equal(new[] { "admin-a", "member", "platform", "shared" }, users.Users.Select(u => u.Username));
        Assert.Equal(TenantUserStatus.Forbidden, (await service.ListAsync(admin,102,true,Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden, (await service.ListAsync(Session('d',10),101,true,Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden, (await service.ListAsync(Session('e',7),101,true,Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden, (await service.ListAsync(Session('a',8),101,true,Token)).Status);
        Assert.Equal(TenantUserStatus.NotFound, (await service.SetActiveAsync(admin,101,8,false,Token)).Status);
        Assert.Equal(TenantUserStatus.SharedIdentity, (await service.UpdateAsync(admin,101,9,"new-name","member",Token)).Status);
        Assert.Equal(TenantUserStatus.SharedIdentity, (await service.ResetPasswordAsync(admin,101,9,"new-password",Token)).Status);
        Assert.Equal(TenantUserStatus.Success, (await service.UpdateAsync(admin,101,9,"shared","admin",Token)).Status);
        await using (var otherRole = new NpgsqlCommand("SELECT tenant_role FROM public.platform_tenant_memberships WHERE tenant_id=102 AND user_id=9", connection))
            Assert.Equal("member",(string)(await otherRole.ExecuteScalarAsync(Token))!);
        Assert.Equal(TenantUserStatus.Success, (await service.UpdateAsync(admin,101,9,"shared","member",Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden, (await service.ResetPasswordAsync(admin,101,11,"new-password",Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden, (await service.UpdateAsync(admin,101,11,"renamed-platform","member",Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden, (await service.SetActiveAsync(admin,101,11,false,Token)).Status);
        await ExecuteAsync(connection, "UPDATE public.users SET is_active=FALSE WHERE id=10");
        Assert.DoesNotContain((await service.ListAsync(admin,101,false,Token)).Users, user => user.Id == 10);
        Assert.False(Assert.Single((await service.ListAsync(admin,101,true,Token)).Users, user => user.Id == 10).IsActive);
        await ExecuteAsync(connection, "UPDATE public.users SET is_active=TRUE WHERE id=10");

        var created = await service.CreateAsync(admin,101,"created-user","synthetic-password","admin",Token);
        Assert.Equal(TenantUserStatus.Success, created.Status);
        Assert.NotNull(created.UserId);
        await using (var createdUser = new NpgsqlCommand("SELECT role,password_hash FROM public.users WHERE id=$1", connection))
        {
            createdUser.Parameters.AddWithValue(created.UserId!.Value);
            await using var reader = await createdUser.ExecuteReaderAsync(Token);
            Assert.True(await reader.ReadAsync(Token));
            Assert.True(reader.IsDBNull(0));
            Assert.True(BCrypt.Net.BCrypt.Verify("synthetic-password", reader.GetString(1)));
        }
        Assert.Equal(TenantUserStatus.Conflict, (await service.CreateAsync(admin,101,"admin-b","pass","member",Token)).Status);
        Assert.Equal(TenantUserStatus.Invalid, (await service.CreateAsync(admin,101,"valid",new string('x',73),"member",Token)).Status);
        Assert.Equal(TenantUserStatus.Success, (await service.UpdateAsync(admin,101,10,"renamed-member","member",Token)).Status);
        Assert.Equal(TenantUserStatus.Success, (await service.ResetPasswordAsync(admin,101,10,"replacement",Token)).Status);
        await using (var verify = new NpgsqlCommand("SELECT password_hash,(SELECT count(*) FROM public.sessions WHERE user_id=10) FROM public.users WHERE id=10", connection))
        {
            await using var reader = await verify.ExecuteReaderAsync(Token);
            Assert.True(await reader.ReadAsync(Token));
            Assert.True(BCrypt.Net.BCrypt.Verify("replacement",reader.GetString(0)));
            Assert.Equal(0L,reader.GetInt64(1));
        }

        Assert.Equal(TenantUserStatus.Success, (await service.SetActiveAsync(admin,101,9,false,Token)).Status);
        var tenantResolver = new TenantContextResolver(transactions);
        Assert.Null(await tenantResolver.ResolveAsync(Session('c',9),101,Token));
        Assert.NotNull(await tenantResolver.ResolveAsync(Session('c',9),102,Token));
        Assert.Equal(TenantUserStatus.Success, (await service.SetActiveAsync(admin,101,9,true,Token)).Status);

        // An unavailable mandatory audit sink must roll back both creation and
        // a password reset, including its session invalidation.
        await ExecuteAsync(connection, "INSERT INTO public.sessions(token,user_id,expiry) VALUES (repeat('g',64),10,now()+interval '1 hour')");
        await ExecuteAsync(connection, "ALTER TABLE public.audit_log RENAME TO audit_log_unavailable");
        try
        {
            await Assert.ThrowsAsync<PostgresException>(() => service.CreateAsync(admin,101,"rolled-back","pass","member",Token));
            await Assert.ThrowsAsync<PostgresException>(() => service.ResetPasswordAsync(admin,101,10,"should-not-stick",Token));
        }
        finally { await ExecuteAsync(connection, "ALTER TABLE public.audit_log_unavailable RENAME TO audit_log"); }
        await using (var check = new NpgsqlCommand("SELECT count(*) FROM public.users WHERE username='rolled-back'", connection))
            Assert.Equal(0L, (long)(await check.ExecuteScalarAsync(Token))!);
        await using (var check = new NpgsqlCommand("SELECT password_hash,(SELECT count(*) FROM public.sessions WHERE user_id=10) FROM public.users WHERE id=10", connection))
        {
            await using var reader = await check.ExecuteReaderAsync(Token);
            Assert.True(await reader.ReadAsync(Token));
            Assert.True(BCrypt.Net.BCrypt.Verify("replacement",reader.GetString(0)));
            Assert.Equal(1L,reader.GetInt64(1));
        }

        await using (var audit = new NpgsqlCommand("SELECT details FROM public.audit_log WHERE action='Create' AND module='TenantUsers'", connection))
        {
            var details = (string)(await audit.ExecuteScalarAsync(Token))!;
            Assert.Contains("tenant_id",details);
            Assert.Contains("target_user_id",details);
            Assert.DoesNotContain("synthetic-password",details);
        }

        // The legacy-shaped API stays disabled unless explicitly configured.
        await using (var disabled = new WebApplicationFactory<Program>())
        using (var disabledClient = disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable, (await disabledClient.GetAsync("/api/users",Token)).StatusCode);

        await using (var app = new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
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
                services.PostConfigure<UserManagementOptions>(settings => settings.Enabled = true);
            });
        }))
        using (var client = app.CreateClient(new WebApplicationFactoryClientOptions { HandleCookies = false }))
        {
            client.DefaultRequestHeaders.Add("Cookie","session_token=" + new string('a',64));
            client.DefaultRequestHeaders.Add("X-Tenant-Id","102");
            using var listed = await client.GetAsync("/api/users?include_inactive=true",Token);
            Assert.Equal(HttpStatusCode.OK,listed.StatusCode);
            using var json = JsonDocument.Parse(await listed.Content.ReadAsStringAsync(Token));
            var first = json.RootElement[0];
            Assert.True(first.TryGetProperty("id",out _));
            Assert.True(first.TryGetProperty("username",out _));
            Assert.True(first.TryGetProperty("is_active",out _));
            Assert.False(first.TryGetProperty("password_hash",out _));
            Assert.DoesNotContain(json.RootElement.EnumerateArray(), element => element.GetProperty("username").GetString() == "admin-b");
            using var noOrigin = await client.PostAsJsonAsync("/api/users",new { username="http-user",password="pass",role=(string?)null },Token);
            Assert.Equal(HttpStatusCode.Forbidden,noOrigin.StatusCode);
            client.DefaultRequestHeaders.Add("Origin","http://127.0.0.1:5080");
            using var missing = await client.PostAsJsonAsync("/api/users",new { password="PRIVATE_SENTINEL" },Token);
            Assert.Equal(HttpStatusCode.UnprocessableEntity,missing.StatusCode);
            var missingBody = await missing.Content.ReadAsStringAsync(Token);
            Assert.DoesNotContain("PRIVATE_SENTINEL",missingBody);
            using (var missingJson = JsonDocument.Parse(missingBody))
            {
                Assert.Equal(JsonValueKind.Array,missingJson.RootElement.GetProperty("detail").ValueKind);
                Assert.Equal("username",missingJson.RootElement.GetProperty("detail")[0].GetProperty("loc")[1].GetString());
            }
            using var malformed = await client.PostAsync("/api/users",
                new StringContent("{\"password\":\"PRIVATE_SENTINEL\"", Encoding.UTF8, "application/json"),Token);
            Assert.Equal(HttpStatusCode.UnprocessableEntity,malformed.StatusCode);
            Assert.DoesNotContain("PRIVATE_SENTINEL",await malformed.Content.ReadAsStringAsync(Token));
            using var createdHttp = await client.PostAsJsonAsync("/api/users",new { username="http-user",password="pass",role=(string?)null },Token);
            Assert.Equal(HttpStatusCode.OK,createdHttp.StatusCode);
            using var createdJson = JsonDocument.Parse(await createdHttp.Content.ReadAsStringAsync(Token));
            var httpUserId = createdJson.RootElement.GetProperty("user_id").GetInt32();
            Assert.Equal(HttpStatusCode.OK,(await client.PutAsJsonAsync($"/api/users/{httpUserId}",new { username="http-renamed",role=(string?)null },Token)).StatusCode);
            Assert.Equal(HttpStatusCode.OK,(await client.PostAsync($"/api/users/{httpUserId}/deactivate",null,Token)).StatusCode);
            Assert.Equal(HttpStatusCode.OK,(await client.PostAsync($"/api/users/{httpUserId}/reactivate",null,Token)).StatusCode);
            Assert.Equal(HttpStatusCode.OK,(await client.PutAsJsonAsync($"/api/users/{httpUserId}/reset-password",new { password="replacement" },Token)).StatusCode);
            client.DefaultRequestHeaders.Remove("Cookie");
            client.DefaultRequestHeaders.Add("Cookie","session_token=" + new string('c',64));
            Assert.Equal(HttpStatusCode.Conflict,(await client.GetAsync("/api/users",Token)).StatusCode);
            client.DefaultRequestHeaders.Remove("Cookie");
            client.DefaultRequestHeaders.Add("Cookie","session_token=" + new string('b',64));
            Assert.Equal(HttpStatusCode.NotFound,(await client.PostAsync($"/api/users/{httpUserId}/deactivate",null,Token)).StatusCode);
        }

        // Concurrent writes to separate tenants retain their own scopes.
        var results = await Task.WhenAll(
            service.CreateAsync(admin,101,"parallel-a","pass","member",Token),
            service.CreateAsync(Session('b',8),102,"parallel-b","pass","member",Token));
        Assert.All(results, r => Assert.Equal(TenantUserStatus.Success,r.Status));
        await using (var count = new NpgsqlCommand("SELECT count(*) FROM public.platform_tenant_memberships WHERE (tenant_id,user_id) IN ((101,$1),(102,$2))", connection))
        {
            count.Parameters.AddWithValue(results[0].UserId!.Value); count.Parameters.AddWithValue(results[1].UserId!.Value);
            Assert.Equal(2L,(long)(await count.ExecuteScalarAsync(Token))!);
        }
        // Legacy behavior intentionally permits removing the last admin; a
        // separately approved safeguard can change that policy later.
        Assert.Equal(TenantUserStatus.Success,(await service.SetActiveAsync(Session('b',8),102,8,false,Token)).Status);
    }

    private static VerifiedSession Session(char value,int userId) =>
        new(new string(value,64),userId,"untrusted", "admin",DateTime.UtcNow.AddHours(1),null,null,null);

    private static async Task ExecuteAsync(NpgsqlConnection connection,string sql)
    {
        await using var command = new NpgsqlCommand(sql,connection);
        await command.ExecuteNonQueryAsync(Token);
    }
}
