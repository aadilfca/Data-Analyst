using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Tenancy;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class EngagementFoundationTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;

    [Fact]
    public async Task Candidate_restricts_firms_and_active_teams_without_altering_legacy_data()
    {
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await ExecuteAsync(connection, """
            CREATE TABLE public.users(id INTEGER PRIMARY KEY, username TEXT NOT NULL,
                password_hash TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE, role TEXT, last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY, client_code TEXT NOT NULL,
                client_name TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES public.users(id),
                expiry TIMESTAMPTZ NOT NULL, selected_client_id INTEGER REFERENCES public.clients(id));
            CREATE TABLE public.client_data(id INTEGER PRIMARY KEY, client_id INTEGER REFERENCES public.clients(id),
                row_data JSONB NOT NULL);
            CREATE TABLE public.verification_sessions(id INTEGER PRIMARY KEY, client_id INTEGER REFERENCES public.clients(id));
            INSERT INTO public.users VALUES (7,'manager-a','hash',TRUE,'admin',NULL),
                (8,'internee-a','hash',TRUE,'analyst',NULL), (9,'manager-b','hash',TRUE,'admin',NULL),
                (10,'unassigned','hash',TRUE,'analyst',NULL), (11,'business-user','hash',TRUE,'admin',NULL);
            INSERT INTO public.clients VALUES (5,'A','Business X',TRUE),(6,'B','Business X',TRUE),
                (7,'C','Business-owned record',TRUE);
            INSERT INTO public.sessions VALUES
                (repeat('a',64),7,now()+interval '1 hour',5),
                (repeat('b',64),8,now()+interval '1 hour',5),
                (repeat('c',64),9,now()+interval '1 hour',6),
                (repeat('d',64),10,now()+interval '1 hour',5),
                (repeat('e',64),11,now()+interval '1 hour',NULL),
                (repeat('f',64),7,now()-interval '1 hour',5);
            INSERT INTO public.client_data VALUES (21,5,'{"amount":"123.45"}');
            INSERT INTO public.verification_sessions VALUES (31,5);
            """);
        await fixture.ApplyTenantFoundationAsync(Token);
        await ExecuteAsync(connection, """
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES
                (101,'Firm A','audit_firm'),(102,'Firm B','audit_firm'),(103,'Business X','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id) VALUES
                (101,7),(101,8),(101,10),(102,9),(103,11);
            INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES (5,101),(6,102),(7,103);
            """);

        // The exact candidate must roll back before the isolated apply.
        await using (var rollback = await connection.BeginTransactionAsync(Token))
        {
            var sql = await PostgresFixture.ReadEngagementFoundationAsync(Token);
            await using var candidate = new NpgsqlCommand(sql, connection, rollback);
            await candidate.ExecuteNonQueryAsync(Token);
            await rollback.RollbackAsync(Token);
        }
        await using (var check = new NpgsqlCommand("SELECT to_regclass('public.platform_audit_engagements') IS NULL", connection))
            Assert.True((bool)(await check.ExecuteScalarAsync(Token))!);
        await fixture.ApplyEngagementFoundationAsync(Token);
        await ExecuteAsync(connection, """
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE
                VALUES (201,101,5,'Firm A engagement'),(202,102,6,'Firm B engagement'),
                       (203,101,5,'Another period');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role)
                VALUES (201,101,7,'manager'),(201,101,8,'internee'),(202,102,9,'manager');
            """);

        await ExpectSqlStateAsync(connection, PostgresErrorCodes.ForeignKeyViolation,
            "INSERT INTO public.platform_audit_engagements(firm_tenant_id,client_id,name) VALUES (101,6,'Other firm client')");
        await ExpectSqlStateAsync(connection, PostgresErrorCodes.ForeignKeyViolation,
            "INSERT INTO public.platform_audit_engagements(firm_tenant_id,client_id,name) VALUES (103,7,'Business as firm')");
        await ExpectSqlStateAsync(connection, PostgresErrorCodes.ForeignKeyViolation,
            "INSERT INTO public.platform_engagement_assignments VALUES (201,101,9,'manager',TRUE,now())");
        await ExpectSqlStateAsync(connection, PostgresErrorCodes.CheckViolation,
            "INSERT INTO public.platform_engagement_assignments VALUES (203,101,10,'invented',TRUE,now())");
        await ExpectSqlStateAsync(connection, PostgresErrorCodes.UniqueViolation,
            "INSERT INTO public.platform_engagement_assignments VALUES (201,101,7,'manager',TRUE,now())");

        using var pool = fixture.CreatePool();
        var resolver = new EngagementAccessResolver(new PostgresTransactions(pool, Options.Create(new PostgresOptions())));
        var manager = Session('a', 7);
        Assert.Equal(new EngagementEligibility(201,101,7,"manager"), await resolver.ResolveAsync(manager,101,201,Token));
        Assert.Equal(new EngagementEligibility(201,101,8,"internee"), await resolver.ResolveAsync(Session('b',8),101,201,Token));
        Assert.Equal(new EngagementEligibility(202,102,9,"manager"), await resolver.ResolveAsync(Session('c',9),102,202,Token));
        Assert.Null(await resolver.ResolveAsync(Session('c',9),101,201,Token));
        Assert.Null(await resolver.ResolveAsync(manager,102,202,Token));
        Assert.Null(await resolver.ResolveAsync(Session('d',10),101,201,Token));
        Assert.Null(await resolver.ResolveAsync(Session('e',11),103,201,Token));
        Assert.Null(await resolver.ResolveAsync(manager,101,203,Token));
        Assert.Null(await resolver.ResolveAsync(Session('a',8),101,201,Token));
        Assert.Null(await resolver.ResolveAsync(Session('f',7),101,201,Token));
        Assert.Null(await resolver.ResolveAsync(null,101,201,Token));
        Assert.Null(await resolver.ResolveAsync(manager,101,-1,Token));

        await ExecuteAsync(connection, "UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE engagement_id=201 AND user_id=7");
        Assert.Null(await resolver.ResolveAsync(manager,101,201,Token));
        await ExecuteAsync(connection, "UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE engagement_id=201 AND user_id=7; UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE tenant_id=101 AND user_id=7");
        Assert.Null(await resolver.ResolveAsync(manager,101,201,Token));
        await ExecuteAsync(connection, "UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE tenant_id=101 AND user_id=7; UPDATE public.platform_audit_engagements SET is_active=FALSE WHERE id=201");
        Assert.Null(await resolver.ResolveAsync(manager,101,201,Token));
        await ExecuteAsync(connection, "UPDATE public.platform_audit_engagements SET is_active=TRUE WHERE id=201; UPDATE public.platform_tenants SET is_active=FALSE WHERE id=101");
        Assert.Null(await resolver.ResolveAsync(manager,101,201,Token));
        await ExecuteAsync(connection, "UPDATE public.platform_tenants SET is_active=TRUE WHERE id=101; UPDATE public.users SET is_active=FALSE WHERE id=7");
        Assert.Null(await resolver.ResolveAsync(manager,101,201,Token));
        await ExecuteAsync(connection, "UPDATE public.users SET is_active=TRUE WHERE id=7; UPDATE public.clients SET is_active=FALSE WHERE id=5");
        Assert.Null(await resolver.ResolveAsync(manager,101,201,Token));
        await ExecuteAsync(connection, "UPDATE public.clients SET is_active=TRUE WHERE id=5");
        Assert.NotNull(await resolver.ResolveAsync(manager,101,201,Token));

        var duplicate = await Assert.ThrowsAsync<PostgresException>(() => fixture.ApplyEngagementFoundationAsync(Token));
        Assert.Equal(PostgresErrorCodes.DuplicateTable, duplicate.SqlState);
        await using var preserved = new NpgsqlCommand("""
            SELECT (SELECT count(*) FROM public.users), (SELECT count(*) FROM public.clients),
                (SELECT count(*) FROM public.sessions),
                (SELECT row_data->>'amount' FROM public.client_data WHERE id=21),
                (SELECT client_id FROM public.verification_sessions WHERE id=31),
                (SELECT count(*) FROM public.platform_audit_engagements)
            """, connection);
        await using var reader = await preserved.ExecuteReaderAsync(Token);
        Assert.True(await reader.ReadAsync(Token));
        Assert.Equal(5L, reader.GetInt64(0));
        Assert.Equal(3L, reader.GetInt64(1));
        Assert.Equal(6L, reader.GetInt64(2));
        Assert.Equal("123.45", reader.GetString(3));
        Assert.Equal(5, reader.GetInt32(4));
        Assert.Equal(3L, reader.GetInt64(5));
    }

    private static VerifiedSession Session(char token, int userId) =>
        new(new string(token, 64), userId, "synthetic", null, DateTime.UtcNow.AddHours(1), null, null, null);

    private static async Task ExecuteAsync(NpgsqlConnection connection, string sql)
    {
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(Token);
    }

    private static async Task ExpectSqlStateAsync(NpgsqlConnection connection, string state, string sql)
    {
        var error = await Assert.ThrowsAsync<PostgresException>(() => ExecuteAsync(connection, sql));
        Assert.Equal(state, error.SqlState);
    }
}
