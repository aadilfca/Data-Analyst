using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class VerificationMigrationCompatibilityTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    [Fact]
    public async Task Account_control_assignment_migration_creates_client_scoped_routing_table()
    {
        var ct = TestContext.Current.CancellationToken;
        await using var connection = new NpgsqlConnection(fixture.ConnectionString);
        await connection.OpenAsync(ct);
        await using var transaction = await connection.BeginTransactionAsync(ct);
        var schema = "verification_migration_" + Guid.NewGuid().ToString("N");
        await using (var setup = new NpgsqlCommand(
            $"CREATE SCHEMA {schema}; CREATE TABLE {schema}.clients(id integer PRIMARY KEY)", connection, transaction))
            await setup.ExecuteNonQueryAsync(ct);

        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null && !File.Exists(Path.Combine(directory.FullName, "AuditPlatform.slnx")))
            directory = directory.Parent;
        Assert.NotNull(directory);
        var migration = await File.ReadAllTextAsync(Path.Combine(directory.FullName, "database", "migrations",
            "0027_verification_account_control_assignments.sql"), ct);
        migration = migration.Replace("public.", schema + ".", StringComparison.Ordinal);
        await using (var apply = new NpgsqlCommand(migration, connection, transaction))
            await apply.ExecuteNonQueryAsync(ct);

        await using (var insert = new NpgsqlCommand($"""
            INSERT INTO {schema}.clients VALUES (1);
            INSERT INTO {schema}.account_control_assignments(client_id,account_code,verification_workstream)
            VALUES (1,'010','cash')
            """, connection, transaction))
            await insert.ExecuteNonQueryAsync(ct);
        await using var verify = new NpgsqlCommand($"""
            SELECT a.verification_workstream,
                   (SELECT count(*) FROM pg_constraint c
                    WHERE c.conrelid='{schema}.account_control_assignments'::regclass AND c.contype='u'),
                   (SELECT count(*) FROM pg_constraint c
                    WHERE c.conrelid='{schema}.account_control_assignments'::regclass AND c.contype='f')
            FROM {schema}.account_control_assignments a WHERE a.client_id=1 AND a.account_code='010'
            """, connection, transaction);
        await using var reader = await verify.ExecuteReaderAsync(ct);
        Assert.True(await reader.ReadAsync(ct));
        Assert.Equal("cash", reader.GetString(0));
        Assert.Equal(1L, reader.GetInt64(1));
        Assert.Equal(1L, reader.GetInt64(2));
    }

    [Fact]
    public async Task Evidence_indexes_preserve_legacy_upserts_and_separate_native_tenants()
    {
        var ct = TestContext.Current.CancellationToken;
        await using var connection = new NpgsqlConnection(fixture.ConnectionString);
        await connection.OpenAsync(ct);
        await using var transaction = await connection.BeginTransactionAsync(ct);
        async Task Execute(string sql)
        {
            await using var command = new NpgsqlCommand(sql, connection, transaction);
            await command.ExecuteNonQueryAsync(ct);
        }
        await Execute("""
            CREATE TABLE public.voucher_line_procedure_checks (
                voucher_line_id integer, document_id integer, role_name text, tenant_id integer,
                rulebook_source text NOT NULL DEFAULT 'legacy', UNIQUE(voucher_line_id,document_id,role_name));
            CREATE TABLE public.voucher_line_observations (
                voucher_line_id integer, document_id integer, tenant_id integer, observation_text text,
                rulebook_source text NOT NULL DEFAULT 'legacy', UNIQUE(voucher_line_id,document_id));
            CREATE TABLE public.voucher_check_status (
                client_id integer, transaction_ref text, check_id integer, tenant_id integer,
                rulebook_source text NOT NULL DEFAULT 'legacy', UNIQUE(client_id,transaction_ref,check_id));
            INSERT INTO public.voucher_line_observations VALUES (1,1,NULL,'original','legacy');
            """);
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null && !File.Exists(Path.Combine(directory.FullName, "AuditPlatform.slnx")))
            directory = directory.Parent;
        Assert.NotNull(directory);
        var candidate = await File.ReadAllTextAsync(Path.Combine(directory.FullName, "database", "migrations",
            "0022_verification_native_evidence.sql"), ct);
        var start = candidate.IndexOf("DO $$ DECLARE old_unique RECORD; BEGIN", StringComparison.Ordinal);
        var end = candidate.IndexOf("CREATE INDEX IF NOT EXISTS voucher_line_documents_tenant_line", StringComparison.Ordinal);
        Assert.True(start >= 0 && end > start);
        // Execute the actual candidate block; let PostgreSQL generate its own
        // long constraint name so the previous naming defect is exercised.
        await Execute(candidate[start..end]);
        await Execute("""
            INSERT INTO public.voucher_line_procedure_checks VALUES
                (1,1,'reviewer',NULL,'legacy'), (1,1,'reviewer',1,'native'), (1,1,'reviewer',2,'native');
            INSERT INTO public.voucher_line_procedure_checks VALUES (1,1,'reviewer',NULL,'legacy')
                ON CONFLICT(voucher_line_id,document_id,role_name) WHERE rulebook_source='legacy'
                DO UPDATE SET role_name=EXCLUDED.role_name;
            INSERT INTO public.voucher_line_observations VALUES (1,1,1,'native preserved','native');
            INSERT INTO public.voucher_line_observations VALUES (1,1,NULL,'legacy updated','legacy')
                ON CONFLICT(voucher_line_id,document_id) WHERE rulebook_source='legacy'
                DO UPDATE SET observation_text=EXCLUDED.observation_text;
            INSERT INTO public.voucher_check_status VALUES (1,'synthetic',1,NULL,'legacy');
            INSERT INTO public.voucher_check_status VALUES (1,'synthetic',1,NULL,'legacy')
                ON CONFLICT(client_id,transaction_ref,check_id) WHERE rulebook_source='legacy'
                DO UPDATE SET check_id=EXCLUDED.check_id;
            """);
        await using var verify = new NpgsqlCommand("""
            SELECT (SELECT count(*) FROM public.voucher_line_procedure_checks),
                   (SELECT observation_text FROM public.voucher_line_observations WHERE rulebook_source='native'),
                   (SELECT observation_text FROM public.voucher_line_observations WHERE rulebook_source='legacy'),
                   (SELECT count(*) FROM public.voucher_check_status)
            """, connection, transaction);
        await using var reader = await verify.ExecuteReaderAsync(ct);
        Assert.True(await reader.ReadAsync(ct));
        Assert.Equal(3L, reader.GetInt64(0));
        Assert.Equal("native preserved", reader.GetString(1));
        Assert.Equal("legacy updated", reader.GetString(2));
        Assert.Equal(1L, reader.GetInt64(3));
    }
}
