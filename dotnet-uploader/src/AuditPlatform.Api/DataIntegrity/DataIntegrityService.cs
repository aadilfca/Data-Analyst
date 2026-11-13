using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.DataBank;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.DataIntegrity;

// All record/catalog evidence is materialized in one read-only snapshot. No file
// paths, source values, DDL, repairs or source-file probes leave this boundary.
public sealed class DataIntegrityService(PostgresReadSnapshot snapshots, DatabaseSafeguardService safeguards,
    IOptions<DataIntegrityOptions> options, TimeProvider? clock = null)
{
    private readonly SemaphoreSlim requests = new(options.Value.MaximumConcurrentRequests);
    public async Task<DataIntegrityResult> ReadAsync(VerifiedSession? session, int clientId, CancellationToken ct = default)
    {
        if (!options.Value.Enabled) return new("unavailable", Code: "data_integrity_disabled");
        if (session is null) return new("unauthenticated", Code: "not_authenticated");
        if (clientId <= 0) return new("invalid", Code: "invalid_data_integrity_request");
        if (!await requests.WaitAsync(0, ct)) return new("busy", Code: "data_integrity_busy");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var value = await snapshots.ExecuteAsync(async (db, token) =>
            {
                await DataBankService.Authorize(db, session, clientId, token);
                await using var client = db.CreateCommand("SELECT left(COALESCE(client_name,'Selected client'),$2) FROM public.clients WHERE id=$1");
                client.Parameters.AddWithValue(clientId);
                client.Parameters.AddWithValue(options.Value.MaximumResponseBytes + 1);
                var name = (string)(await client.ExecuteScalarAsync(token))!;
                // Bound materialization even if a legacy name bypassed native validation.
                // An oversized prefix refuses the whole result; no truncated label is released.
                if (name.Length > options.Value.MaximumResponseBytes) throw new IntegrityCapacityException();
                var modules = new[] { await ClientData(db, clientId, token), await TrialBalance(db, clientId, token), await Reconciliation(db, clientId, token) };
                var indexes = await safeguards.ReadCatalogAsync(db, token);
                var admin = await OptionalRead(db, async () => { await DataBankService.Authorize(db, session, clientId, token, admin: true); return new AdminGrant(); }, token, allowDenied: true);
                var database = admin is null ? null : await OptionalRead(db, async () =>
                {
                    await using var command = db.CreateCommand("SELECT pg_database_size(current_database()),COALESCE((SELECT SUM(pg_relation_size(indexrelid)) FROM pg_stat_user_indexes),0)::bigint");
                    await using var reader = await command.ExecuteReaderAsync(token);
                    await reader.ReadAsync(token);
                    return new IntegrityDatabaseContext(reader.GetInt64(0), IntegrityModule.ByteLabel(reader.GetInt64(0)), reader.GetInt64(1), IntegrityModule.ByteLabel(reader.GetInt64(1)));
                }, token);
                var review = modules.Any(m => m.DuplicateStatus == "Review");
                var incomplete = modules.Any(m => m.DuplicateStatus == "Not fully checked");
                return new IntegrityOverview(clientId, name, (clock ?? TimeProvider.System).GetUtcNow(),
                    review ? incomplete ? "Review; scan incomplete" : "Review" : incomplete ? "Not fully checked" : "Clear",
                    modules, DatabaseSafeguardService.Assess(modules, indexes), database,
                    "Module sizes estimate stored rows and selected-client upload metadata. Legacy source files are unmeasured. Whole-database allocation is administrator-only, shared and sampled; it is not a client size or a reclaimable-space measurement.",
                    "Duplicate checks cover active records in this client using each module's identity. Matching IDs across modules and legitimate repeated detail lines remain separate. Invalid upload links are excluded from associated sizes.");
            }, deadline.Token);
            // Discard evidence if access or the selected context changed during the scan.
            await snapshots.ExecuteAsync(async (db, token) =>
            {
                await DataBankService.Authorize(db, session, clientId, token, admin: value.DatabaseContext is not null);
                return true;
            }, deadline.Token);
            return JsonSerializer.SerializeToUtf8Bytes(value, IntegrityJson.Options).Length > options.Value.MaximumResponseBytes
                ? new("too_large", Code: "data_integrity_response_capacity") : new("success", value);
        }
        catch (DataBankRejected) { return new("forbidden", Code: "data_integrity_access_denied"); }
        catch (IntegrityCapacityException) { return new("too_large", Code: "data_integrity_response_capacity"); }
        catch (PostgresException error) when (error.SqlState is "42P01" or "42703" or "42501") { return new("review", Code: "data_integrity_schema_review_required"); }
        catch (PostgresException error) when (error.SqlState is "57014" or "55P03") { return new("timeout", Code: "data_integrity_database_timeout"); }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested && deadline.IsCancellationRequested) { return new("timeout", Code: "data_integrity_timeout"); }
        finally { requests.Release(); }
    }

    private sealed record AdminGrant;
    private sealed class IntegrityCapacityException : Exception;
    private sealed record RecordEvidence(long Rows, long Bytes, long Unidentified);
    private sealed record DuplicateEvidence(long Groups, long Extras);
    private sealed record AssociatedEvidence(long Records, long Bytes, long Unmeasured, long InvalidLinks);

    // Expected schema/permission gaps are isolated; cancellation, timeout, broken
    // connections and unexpected SQL faults must escape and discard the whole read.
    internal static async Task<T?> OptionalRead<T>(PostgresTransactionContext db, Func<Task<T?>> read, CancellationToken ct, bool allowDenied = false) where T : class
    {
        async Task Control(string sql) { await using var command = db.CreateCommand(sql); await command.ExecuteNonQueryAsync(ct); }
        await Control("SAVEPOINT integrity_optional");
        try
        {
            var value = await read();
            await Control("RELEASE SAVEPOINT integrity_optional");
            return value;
        }
        catch (Exception error) when (error is PostgresException { SqlState: "42P01" or "42703" or "42501" } || allowDenied && error is DataBankRejected)
        {
            await Control("ROLLBACK TO SAVEPOINT integrity_optional");
            await Control("RELEASE SAVEPOINT integrity_optional");
            return null;
        }
    }

    private static Task<RecordEvidence?> Records(PostgresTransactionContext db, int client, string sql, CancellationToken ct) => OptionalRead(db, async () =>
    {
        await using var command = db.CreateCommand(sql); command.Parameters.AddWithValue(client);
        await using var reader = await command.ExecuteReaderAsync(ct); await reader.ReadAsync(ct);
        return new RecordEvidence(reader.GetInt64(0), reader.GetInt64(1), reader.GetInt64(2));
    }, ct);
    private static Task<DuplicateEvidence?> Duplicates(PostgresTransactionContext db, int client, string grouped, CancellationToken ct) => OptionalRead(db, async () =>
    {
        await using var command = db.CreateCommand($"SELECT COUNT(*),COALESCE(SUM(group_size-1),0)::bigint FROM ({grouped}) duplicates");
        command.Parameters.AddWithValue(client);
        await using var reader = await command.ExecuteReaderAsync(ct); await reader.ReadAsync(ct);
        return new DuplicateEvidence(reader.GetInt64(0), reader.GetInt64(1));
    }, ct);

    private static async Task<IntegrityModule> ClientData(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        var data = await Records(db, client, """
            SELECT COUNT(*),(COALESCE(SUM(LENGTH(cd.row_data::text)),0)+COUNT(*)*256)::bigint,
                COUNT(*) FILTER (WHERE template_id IS NULL OR source_identity IS NULL OR BTRIM(source_identity)='')
            FROM public.client_data cd WHERE client_id=$1 AND is_deleted=FALSE AND change_detection_status<>'deleted'
            """, ct);
        var duplicates = await Duplicates(db, client, """
            SELECT template_id,source_identity,COUNT(*) group_size FROM public.client_data
            WHERE client_id=$1 AND is_deleted=FALSE AND change_detection_status<>'deleted' AND source_identity IS NOT NULL
            GROUP BY template_id,source_identity HAVING COUNT(*)>1
            """, ct);
        var associated = await OptionalRead(db, async () =>
        {
            await using var command = db.CreateCommand("""
                SELECT u.id,COALESCE(pg_column_size(u),0),u.file_path IS NOT NULL AND u.file_path<>''
                FROM public.uploaded_files u WHERE u.client_id=$1 ORDER BY u.id
                """);
            command.Parameters.AddWithValue(client);
            long count = 0, bytes = 0, unmeasured = 0;
            await using (var reader = await command.ExecuteReaderAsync(ct))
                while (await reader.ReadAsync(ct)) { count++; bytes = checked(bytes + reader.GetInt32(1)); if (reader.GetBoolean(2)) unmeasured++; }
            await using var links = db.CreateCommand("""
                SELECT COUNT(*) FROM public.client_data cd WHERE cd.client_id=$1 AND cd.upload_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM public.uploaded_files own WHERE own.id=cd.upload_id AND own.client_id=$1)
                """);
            links.Parameters.AddWithValue(client);
            return new AssociatedEvidence(count, bytes, unmeasured, (long)(await links.ExecuteScalarAsync(ct))!);
        }, ct);
        return new("Client Data", data?.Rows, associated?.Records, data?.Bytes, associated?.Bytes, duplicates?.Groups, duplicates?.Extras,
            data?.Unidentified, associated?.Unmeasured, "Active rows, grouped by template and saved source identity.", associated?.InvalidLinks);
    }

    private static async Task<IntegrityModule> TrialBalance(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        var data = await Records(db, client, """
            SELECT COUNT(a.id),COALESCE(SUM(pg_column_size(a)),0)::bigint,
                COUNT(a.id) FILTER (WHERE a.upload_id IS NULL OR a.account_code IS NULL OR BTRIM(a.account_code)='')
            FROM public.trial_balance_uploads u LEFT JOIN public.trial_balance_accounts a ON a.upload_id=u.id WHERE u.client_id=$1
            """, ct);
        var duplicates = await Duplicates(db, client, """
            SELECT a.upload_id,a.account_code,COUNT(*) group_size FROM public.trial_balance_accounts a
            JOIN public.trial_balance_uploads u ON u.id=a.upload_id WHERE u.client_id=$1
            GROUP BY a.upload_id,a.account_code HAVING COUNT(*)>1
            """, ct);
        var associated = await OptionalRead(db, async () =>
        {
            // pg_column_size(composite) can differ between aggregate and per-row
            // projections. Stream the legacy projection in constant memory.
            await using var command = db.CreateCommand("SELECT u.id,COALESCE(pg_column_size(u),0) FROM public.trial_balance_uploads u WHERE client_id=$1 ORDER BY u.id");
            command.Parameters.AddWithValue(client);
            await using var reader = await command.ExecuteReaderAsync(ct);
            long count = 0, bytes = 0;
            while (await reader.ReadAsync(ct)) { count++; bytes = checked(bytes + reader.GetInt32(1)); }
            return new AssociatedEvidence(count, bytes, 0, 0);
        }, ct);
        return new("Trial Balance", data?.Rows, associated?.Records, data?.Bytes, associated?.Bytes, duplicates?.Groups, duplicates?.Extras,
            data?.Unidentified, 0, "Account codes are compared only within the same Trial Balance upload.");
    }

    private static async Task<IntegrityModule> Reconciliation(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        var data = await Records(db, client, """
            SELECT COUNT(*),COALESCE(SUM(pg_column_size(r)),0)::bigint,
                COUNT(*) FILTER (WHERE group_label IS NULL OR BTRIM(group_label)='' OR account_code IS NULL OR BTRIM(account_code)=''
                    OR period_month IS NULL OR BTRIM(period_month)='') FROM public.report_balance_inputs r WHERE client_id=$1
            """, ct);
        var duplicates = await Duplicates(db, client, """
            SELECT LOWER(BTRIM(group_label)),BTRIM(account_code),period_month,COUNT(*) group_size
            FROM public.report_balance_inputs WHERE client_id=$1
            GROUP BY LOWER(BTRIM(group_label)),BTRIM(account_code),period_month HAVING COUNT(*)>1
            """, ct);
        return new("Reconciliation", data?.Rows, 0, data?.Bytes, 0, duplicates?.Groups, duplicates?.Extras, data?.Unidentified, 0,
            "Balance inputs are compared by group, account code, and month.");
    }
}
