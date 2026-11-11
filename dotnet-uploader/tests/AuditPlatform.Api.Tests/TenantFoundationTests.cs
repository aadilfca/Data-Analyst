using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Tenancy;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TenantFoundationTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;

    [Fact]
    public async Task Candidate_preserves_legacy_rows_and_resolves_only_active_session_memberships()
    {
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await ExecuteAsync(connection, """
            CREATE TABLE public.users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE, role TEXT, last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY, client_code TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES public.users(id),
                expiry TIMESTAMPTZ NOT NULL, selected_client_id INTEGER REFERENCES public.clients(id));
            CREATE TABLE public.client_data(id INTEGER PRIMARY KEY, client_id INTEGER REFERENCES public.clients(id),
                row_data JSONB NOT NULL);
            INSERT INTO public.users VALUES (7,'auditor','synthetic-hash',TRUE,'admin',NULL),
                (8,'other','other-hash',TRUE,'analyst',NULL);
            INSERT INTO public.clients VALUES (5,'SYN','Synthetic business',TRUE),
                (6,'SYN2','Other business',TRUE);
            INSERT INTO public.sessions VALUES
                (repeat('a',64),7,now()+interval '1 hour',5),
                (repeat('b',64),8,now()+interval '1 hour',6),
                (repeat('c',64),7,now()-interval '1 hour',5);
            INSERT INTO public.client_data VALUES (11,5,'{"amount":"123.45"}');
            """);
        // Prove PostgreSQL rolls back the exact candidate DDL before applying it.
        await using (var rollback = await connection.BeginTransactionAsync(Token))
        {
            var sql = await PostgresFixture.ReadTenantFoundationAsync(Token);
            await using var candidate = new NpgsqlCommand(sql, connection, rollback);
            await candidate.ExecuteNonQueryAsync(Token);
            await rollback.RollbackAsync(Token);
        }
        await using (var check = new NpgsqlCommand("SELECT to_regclass('public.platform_tenants') IS NULL", connection))
            Assert.True((bool)(await check.ExecuteScalarAsync(Token))!);
        await fixture.ApplyTenantFoundationAsync(Token);
        await ExecuteAsync(connection, """
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE
                VALUES (101,'Synthetic firm','audit_firm'),(102,'Synthetic business','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id) VALUES (101,7),(102,8);
            INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES (5,101),(6,102);
            """);
        var duplicateHome = await Assert.ThrowsAsync<PostgresException>(() => ExecuteAsync(connection,
            "INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES (5,102)"));
        Assert.Equal(PostgresErrorCodes.UniqueViolation, duplicateHome.SqlState);
        var missingUser = await Assert.ThrowsAsync<PostgresException>(() => ExecuteAsync(connection,
            "INSERT INTO public.platform_tenant_memberships(tenant_id,user_id) VALUES (101,999)"));
        Assert.Equal(PostgresErrorCodes.ForeignKeyViolation, missingUser.SqlState);

        using var pool = fixture.CreatePool();
        var resolver = new TenantContextResolver(new PostgresTransactions(pool,
            Options.Create(new PostgresOptions())));
        var user = Session('a', 7, 5);
        var businessUser = Session('b', 8, 6);
        Assert.Equal(new TenantContext(101, "audit_firm", 7), await resolver.ResolveAsync(user, 101, Token));
        Assert.Equal(new TenantContext(102, "business", 8), await resolver.ResolveAsync(businessUser, 102, Token));
        Assert.Null(await resolver.ResolveAsync(user, 102, Token));
        Assert.Null(await resolver.ResolveAsync(businessUser, 101, Token));
        Assert.Null(await resolver.ResolveAsync(Session('a', 8, 5), 101, Token));
        Assert.Null(await resolver.ResolveAsync(Session('c', 7, 5), 101, Token));
        Assert.Null(await resolver.ResolveAsync(null, 101, Token));
        Assert.Null(await resolver.ResolveAsync(user, -1, Token));

        await ExecuteAsync(connection, "INSERT INTO public.platform_tenant_memberships(tenant_id,user_id) VALUES (102,7)");
        Assert.Equal(new TenantContext(102, "business", 7), await resolver.ResolveAsync(user, 102, Token));
        await ExecuteAsync(connection, "DELETE FROM public.platform_tenant_memberships WHERE tenant_id=102 AND user_id=7");
        Assert.Null(await resolver.ResolveAsync(user, 102, Token));

        await ExecuteAsync(connection, "UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE tenant_id=101 AND user_id=7");
        Assert.Null(await resolver.ResolveAsync(user, 101, Token));
        await ExecuteAsync(connection, "UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE tenant_id=101 AND user_id=7; UPDATE public.platform_tenants SET is_active=FALSE WHERE id=101");
        Assert.Null(await resolver.ResolveAsync(user, 101, Token));
        await ExecuteAsync(connection, "UPDATE public.platform_tenants SET is_active=TRUE WHERE id=101; UPDATE public.users SET is_active=FALSE WHERE id=7");
        Assert.Null(await resolver.ResolveAsync(user, 101, Token));
        await ExecuteAsync(connection, "UPDATE public.users SET is_active=TRUE WHERE id=7");
        Assert.NotNull(await resolver.ResolveAsync(user, 101, Token));

        // A second execution must stop rather than silently bless an unknown schema.
        var duplicate = await Assert.ThrowsAsync<PostgresException>(() => fixture.ApplyTenantFoundationAsync(Token));
        Assert.Equal(PostgresErrorCodes.DuplicateTable, duplicate.SqlState);
        await using var preserved = new NpgsqlCommand("""
            SELECT (SELECT count(*) FROM public.users), (SELECT count(*) FROM public.clients),
                   (SELECT count(*) FROM public.sessions),
                   (SELECT row_data->>'amount' FROM public.client_data WHERE id=11),
                   (SELECT count(*) FROM public.platform_tenant_client_homes)
            """, connection);
        await using var reader = await preserved.ExecuteReaderAsync(Token);
        Assert.True(await reader.ReadAsync(Token));
        Assert.Equal(2L, reader.GetInt64(0));
        Assert.Equal(2L, reader.GetInt64(1));
        Assert.Equal(3L, reader.GetInt64(2));
        Assert.Equal("123.45", reader.GetString(3));
        Assert.Equal(2L, reader.GetInt64(4));
    }

    private static VerifiedSession Session(char token, int userId, int clientId) =>
        new(new string(token, 64), userId, "synthetic", null, DateTime.UtcNow.AddHours(1), clientId, null, null);

    private static async Task ExecuteAsync(NpgsqlConnection connection, string sql)
    {
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(Token);
    }
}
