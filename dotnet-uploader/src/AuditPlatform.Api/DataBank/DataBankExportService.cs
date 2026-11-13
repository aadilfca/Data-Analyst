using System.Globalization;
using System.Text;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;
using AuditPlatform.Api.StorageCleanup;

namespace AuditPlatform.Api.DataBank;

public sealed class DataBankExportService(PostgresReadSnapshot snapshots, DataBankService reads,
    IOptions<DataBankOptions> dataBank, IOptions<DataBankExportOptions> options, TimeProvider? clock = null, StorageCleanupService? cleanup = null)
{
    private readonly SemaphoreSlim requests = new(options.Value.MaximumConcurrentRequests);
    public async Task<DataBankDownload> DownloadAsync(VerifiedSession? session, DataBankQuery query, bool backup, CancellationToken ct = default, bool forPurge = false)
    {
        if (!options.Value.Enabled || !dataBank.Value.Enabled || backup && !dataBank.Value.HistoryEnabled)
            return new("unavailable", Code: "databank_exports_disabled");
        if (session is null) return new("unauthenticated", Code: "not_authenticated");
        if(forPurge&&(!backup||cleanup?.EnabledForPurge!=true)) return new("unavailable",Code:"cleanup_disabled");
        if (query.TemplateId is not > 0 || !DataBankService.Valid(query with { Operation = backup ? "accounts" : "preview", Page = 1, Limit = 50 }))
            return new("invalid", Code: "invalid_export_scope");
        if (!await requests.WaitAsync(0, ct)) return new("busy", Code: "databank_export_busy");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct); deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var captured = await snapshots.ExecuteAsync(async (db, token) =>
            {
                await DataBankService.Authorize(db, session, query.ClientId, token, admin: backup);
                var t = await reads.TemplateWithin(db, session.TenantId!.Value, query.TemplateId.Value, token);
                CleanupCapture? proof=null;
                if(forPurge)
                {
                    proof=await cleanup!.CaptureWithin(db,session,query.ClientId,t.Id,"purge",token);
                    if(proof.Blockers.Count>0) throw new CleanupRejected("conflict","cleanup_protected_or_review_required");
                }
                return (Workbook:await Capture(db, query, t, backup, options.Value, (clock ?? TimeProvider.System).GetUtcNow(), token),Proof:proof);
            }, deadline.Token);
            var input=captured.Workbook;
            // ZIP/XML work runs after the input snapshot has closed. All inputs are
            // bounded and request-local, with no stored artifacts or shared files.
            byte[] bytes = DataBankWorkbookWriter.Write(input, options.Value, deadline.Token);
            await snapshots.ExecuteAsync(async (db, token) => { await DataBankService.Authorize(db, session, query.ClientId, token, admin: backup); return true; }, deadline.Token);
            string name = new(input.Template.Name.Select(c => c < ' ' || "<>:\"/\\|?*".Contains(c) ? '_' : c).Take(100).ToArray());
            deadline.Token.ThrowIfCancellationRequested();
            return new("success", bytes, $"{name}_{(backup ? "backup" : "matching")}_{input.CapturedAt.UtcDateTime:yyyy-MM-dd}.xlsx",
                PurgeProof:captured.Proof is null?null:cleanup!.Sign(session,captured.Proof,"backup"));
        }
        catch (DataBankRejected error) { return new(error.Status, Code: error.Code); }
        catch (CleanupRejected error) { return new(error.Status, Code: error.Code); }
        catch (InvalidDataException) { return new("too_large", Code: "complete_workbook_exceeds_limits_or_contains_unsupported_cells"); }
        catch (PostgresException error) when (error.SqlState is "42P01" or "42703") { return new("review", Code: "export_schema_review_required"); }
        catch (PostgresException error) when (error.SqlState is "57014" or "55P03") { return new("timeout", Code: "export_database_timeout"); }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested && deadline.IsCancellationRequested) { return new("timeout", Code: "export_timeout"); }
        finally { requests.Release(); }
    }
    private static async Task<DataBankWorkbook> Capture(PostgresTransactionContext db, DataBankQuery q, DataBankTemplate t, bool backup,
        DataBankExportOptions options, DateTimeOffset at, CancellationToken ct)
    {
        await using var command = db.CreateCommand("");
        string where;
        if (backup)
        {
            command.Parameters.AddWithValue(q.ClientId); command.Parameters.AddWithValue(t.Id);
            where = "cd.client_id=$1 AND cd.template_id=$2 AND COALESCE(cd.is_deleted,FALSE)=FALSE AND COALESCE(cd.change_detection_status,'')<>'deleted'";
        }
        else where = await DataBankService.PreviewPredicate(db, q, t, command, ct, sunday: true);
        const string currentFrom = "public.client_data cd LEFT JOIN public.uploaded_files u ON u.id=cd.upload_id AND u.client_id=cd.client_id AND u.template_id=cd.template_id";
        command.CommandText = $"SELECT count(*),COALESCE(sum(octet_length(cd.row_data::text)+octet_length((to_jsonb(cd)-'row_data')::text)+COALESCE(octet_length(u.ledger_account_code),0)+COALESCE(octet_length(u.ledger_account_name),0)),0) FROM {currentFrom} WHERE {where}";
        long currentCount, inputBytes;
        await using (var reader = await command.ExecuteReaderAsync(ct)) { await reader.ReadAsync(ct); currentCount = reader.GetInt64(0); inputBytes = reader.GetInt64(1); }
        long historyCount = 0;
        if (backup)
        {
            await using var count = db.CreateCommand("SELECT count(*),COALESCE(sum(octet_length(row_data::text)+COALESCE(octet_length(changed_fields::text),0)+COALESCE(octet_length(source_metadata::text),0)+512),0) FROM public.client_data_history WHERE client_id=$1 AND template_id=$2");
            count.Parameters.AddWithValue(q.ClientId); count.Parameters.AddWithValue(t.Id);
            await using var reader = await count.ExecuteReaderAsync(ct); await reader.ReadAsync(ct); historyCount = reader.GetInt64(0); inputBytes += reader.GetInt64(1);
        }
        if (currentCount + historyCount > options.MaximumRows || inputBytes > options.MaximumInputBytes)
            throw new DataBankRejected("too_large", "complete_export_exceeds_limits");
        command.CommandText = $"SELECT cd.id,cd.row_data::text,(to_jsonb(cd)-'row_data')::text,u.ledger_account_code,u.ledger_account_name FROM {currentFrom} WHERE {where} ORDER BY cd.extract_date ASC NULLS LAST,cd.source_order_upload_id ASC NULLS LAST,cd.source_row_number ASC NULLS LAST,cd.id LIMIT {options.MaximumRows + 1}";
        var current = new List<DataBankStoredRow>(); long actualBytes = 0;
        void Budget(string? raw)
        {
            actualBytes += raw is null ? 0 : Encoding.UTF8.GetByteCount(raw);
            if (actualBytes > options.MaximumInputBytes) throw new DataBankRejected("too_large", "complete_export_exceeds_limits");
        }
        await using (var reader = await command.ExecuteReaderAsync(ct))
        while (await reader.ReadAsync(ct))
        {
            string raw = reader.GetString(1), meta = reader.GetString(2); Budget(raw); Budget(meta);
            string? account = DataBankHistoryService.Text(reader, 3), accountName = DataBankHistoryService.Text(reader, 4); Budget(account); Budget(accountName);
            current.Add(new(reader.GetInt32(0), raw, meta, account, accountName));
            if (current.Count > options.MaximumRows) throw new DataBankRejected("too_large", "complete_export_exceeds_limits");
        }
        var history = new List<DataBankStoredHistory>();
        if (backup)
        {
            await using var old = db.CreateCommand($"""
                SELECT h.id,h.client_data_id,h.action,h.changed_at,h.changed_by,h.source_upload_id,h.replacing_upload_id,h.row_data::text,h.changed_fields::text,
                  COALESCE(h.ledger_account_code,h.source_metadata->>'ledger_account_code',u.ledger_account_code),
                  COALESCE(NULLIF(h.ledger_account_name,''),NULLIF(h.source_metadata->>'ledger_account_name',''),NULLIF(u.ledger_account_name,'')),h.source_metadata::text
                FROM public.client_data_history h LEFT JOIN public.uploaded_files u ON u.id=h.source_upload_id AND u.client_id=h.client_id AND u.template_id=h.template_id
                WHERE h.client_id=$1 AND h.template_id=$2 ORDER BY h.changed_at DESC,h.id DESC LIMIT {options.MaximumRows + 1}
                """);
            old.Parameters.AddWithValue(q.ClientId); old.Parameters.AddWithValue(t.Id);
            await using var reader = await old.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                string raw = reader.GetString(7); string? changes = DataBankHistoryService.Text(reader, 8), meta = DataBankHistoryService.Text(reader, 11);
                Budget(raw); Budget(changes); Budget(meta);
                history.Add(new(reader.GetInt32(0), reader.GetInt32(1), reader.GetString(2), reader.IsDBNull(3) ? null : reader.GetDateTime(3).ToString("O", CultureInfo.InvariantCulture),
                    DataBankHistoryService.Text(reader, 4), reader.IsDBNull(5) ? null : reader.GetInt32(5), reader.IsDBNull(6) ? null : reader.GetInt32(6),
                    raw, changes, DataBankHistoryService.Text(reader, 9), DataBankHistoryService.Text(reader, 10), meta));
                if (current.Count + history.Count > options.MaximumRows) throw new DataBankRejected("too_large", "complete_export_exceeds_limits");
            }
        }
        if (current.Count != currentCount || history.Count != historyCount) throw new DataBankRejected("review", "export_completeness_unavailable");
        return new(t, q, backup, at, current, history);
    }
}
