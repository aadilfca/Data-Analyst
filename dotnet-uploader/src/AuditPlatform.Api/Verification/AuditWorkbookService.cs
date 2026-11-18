using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Reports;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Verification;

/// <summary>Read-only line-grain Audit Workbook projection and Verification export.</summary>
public sealed class AuditWorkbookService(PostgresReadSnapshot snapshots, VerificationSessionService sessions,
    VerificationStatusService status, IOptions<VerificationRuntimeOptions> verificationOptions,
    IOptions<ReportInputOptions> workbookOptions)
{
    private static readonly string[] AccountCodeAliases = ["Account Detail", "Account Details", "Account Code",
        "Account No", "Account Number", "Code", "Ledger Code", "AccountID", "Account Head", "Ledger Detail"];
    private static readonly string[] DescriptionAliases = ["Description", "Voucher Detail", "Narration", "Remarks", "Particulars"];
    private const string ExcelContentType = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

    private sealed record Detail(int Id, int ClientDataId, string? TransactionRef, string? ClearedStatus,
        int? AppliedCategoryId, string? AppliedCategoryName, string? AmountText, string? Date,
        JsonElement RowData, string? AccountCode, JsonElement? Description);
    private sealed record LoadOutcome(VerificationRuntimeResult? Failure, WorkbookData? Data, string? CashbookName);
    private sealed record WorkbookData(int ClientId, IReadOnlyList<WorkbookVoucher> Vouchers, int TotalLines)
    {
        public object Response => new { total_vouchers = Vouchers.Count, total_lines = TotalLines,
            client_id = ClientId, vouchers = Vouchers };
    }
    private sealed record WorkbookVoucher(
        [property: JsonPropertyName("sr")] int Sr,
        [property: JsonPropertyName("voucher_no")] string VoucherNo,
        [property: JsonPropertyName("date")] string? Date,
        [property: JsonPropertyName("line_count")] int LineCount,
        [property: JsonPropertyName("amount")] decimal Amount,
        [property: JsonPropertyName("status")] string Status,
        [property: JsonPropertyName("lines")] IReadOnlyList<WorkbookLine> Lines);
    private sealed record WorkbookLine(
        [property: JsonPropertyName("detail_id")] int DetailId,
        [property: JsonPropertyName("client_data_id")] int ClientDataId,
        [property: JsonPropertyName("account_code")] string? AccountCode,
        [property: JsonPropertyName("account_name")] string? AccountName,
        [property: JsonPropertyName("category_name")] string? CategoryName,
        [property: JsonPropertyName("description")] JsonElement? Description,
        [property: JsonPropertyName("amount")] decimal Amount,
        [property: JsonPropertyName("status")] string Status,
        [property: JsonPropertyName("procedures")] ProcedureCount Procedures,
        [property: JsonPropertyName("observations")] ObservationSet Observations);
    private sealed record ProcedureCount([property: JsonPropertyName("done")] int Done,
        [property: JsonPropertyName("total")] int Total);
    private sealed record ObservationSet([property: JsonPropertyName("count")] int Count,
        [property: JsonPropertyName("entries")] IReadOnlyList<Observation> Entries);
    private sealed record Observation([property: JsonPropertyName("document_id")] int DocumentId,
        [property: JsonPropertyName("document_name")] string? DocumentName,
        [property: JsonPropertyName("text")] string Text);

    public async Task<VerificationRuntimeResult> GetAsync(VerifiedSession? session, int sessionId,
        CancellationToken ct = default)
    {
        if (sessionId < 1) return Invalid("verification_session_not_found");
        var loaded = await snapshots.ExecuteAsync((db, token) => LoadAsync(db, session, sessionId, token), ct);
        if (loaded.Failure is not null) return loaded.Failure;
        object response = loaded.Data!.Response;
        if (JsonSerializer.SerializeToUtf8Bytes(response).LongLength > verificationOptions.Value.MaximumSourceBytes)
            return new("too_large", Code: "verification_workbook_capacity");
        return Success(response);
    }

    internal async Task<(VerificationRuntimeResult? Failure, VerificationWorkbookDownload? Download)> ExportAsync(
        VerifiedSession? session, int sessionId, CancellationToken ct = default)
    {
        if (sessionId < 1) return (Invalid("verification_session_not_found"), null);
        var loaded = await snapshots.ExecuteAsync((db, token) => LoadAsync(db, session, sessionId, token), ct);
        if (loaded.Failure is not null) return (loaded.Failure, null);
        var data = loaded.Data!;
        var rows = new List<VerificationWorkbookExportRow>();
        int rowNumber = 0;
        foreach (var voucher in data.Vouchers)
        foreach (var line in voucher.Lines)
        {
            rowNumber++;
            string observations = string.Join("\n", line.Observations.Entries
                .Where(entry => !string.IsNullOrWhiteSpace(entry.Text))
                .Select(entry => string.IsNullOrWhiteSpace(entry.DocumentName)
                    ? entry.Text : $"{entry.DocumentName}: {entry.Text}"));
            rows.Add(new(rowNumber, voucher.Date ?? "", line.AccountCode ?? "", line.CategoryName ?? "",
                line.AccountName ?? "", line.Amount, $"{line.Procedures.Done}/{line.Procedures.Total}",
                observations, line.Status));
        }
        if (rows.Count == 0) return (Invalid("verification_export_empty"), null);
        byte[] bytes;
        try { bytes = VerificationWorkbookWriter.Write(rows, workbookOptions.Value, ct); }
        catch (InvalidDataException) { return (new("too_large", Code: "verification_export_capacity"), null); }
        string safeName = SafeFilename(loaded.CashbookName ?? "Verification");
        string filename = $"Verification_{safeName}_{DateTimeOffset.Now:yyyyMMdd}.xlsx";
        return (null, new(bytes, filename, ExcelContentType));
    }

    private async Task<LoadOutcome> LoadAsync(PostgresTransactionContext db, VerifiedSession? session,
        int sessionId, CancellationToken ct)
    {
        var (owner, failure) = await sessions.ResolveReadContextAsync(db, session, sessionId, ct);
        if (failure is not null) return new(failure, null, null);
        await using (var header = db.CreateCommand("SELECT cashbook_name FROM public.verification_sessions WHERE id=$1 AND client_id=$2 AND status!='Cancelled'"))
        {
            header.Parameters.AddWithValue(sessionId); header.Parameters.AddWithValue(owner!.ClientId);
            var name = await header.ExecuteScalarAsync(ct) as string;
            if (name is null) return new(new("not_found", Code: "verification_session_not_found"), null, null);
            var details = await ReadDetailsAsync(db, sessionId, owner.ClientId, ct);
            if (details is null) return new(new("too_large", Code: "verification_workbook_capacity"), null, name);
            if (details.Count == 0) return new(null, new(owner.ClientId, [], 0), name);

            var relevant = details.Where(detail => !string.IsNullOrEmpty(detail.TransactionRef)).ToArray();
            var progressInputs = relevant.Select(detail => new VerificationAuditWorkbookLineInput(detail.Id,
                detail.ClientDataId, detail.TransactionRef, detail.RowData, detail.AmountText,
                detail.AppliedCategoryId, detail.AppliedCategoryName)).ToArray();
            var progress = await status.AuditWorkbookProcedureProgressAsync(db, owner, progressInputs, ct);
            var accountNames = await ReadAccountNamesAsync(db, owner.ClientId,
                relevant.Select(detail => detail.AccountCode).Where(code => !string.IsNullOrEmpty(code))
                    .Cast<string>().Distinct(StringComparer.Ordinal).ToArray(), ct);
            var observations = await ReadObservationsAsync(db, owner.TenantId, owner.ClientId,
                relevant.Select(detail => detail.ClientDataId).Distinct().ToArray(), ct);

            var groups = new List<(string Ref, string? Date, List<WorkbookLine> Lines, List<string?> Statuses, decimal Amount)>();
            var byReference = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var detail in relevant)
            {
                string reference = detail.TransactionRef!;
                if (!byReference.TryGetValue(reference, out int groupIndex))
                {
                    groupIndex = groups.Count;
                    byReference[reference] = groupIndex;
                    groups.Add((reference, detail.Date, [], [], 0m));
                }
                decimal amount = ParseAmount(detail.AmountText);
                var p = progress.GetValueOrDefault(detail.Id) ?? new(0, 0, detail.AppliedCategoryId, detail.AppliedCategoryName);
                var lineObservations = observations.GetValueOrDefault(detail.ClientDataId) ?? [];
                var line = new WorkbookLine(detail.Id, detail.ClientDataId, detail.AccountCode,
                    accountNames.GetValueOrDefault(detail.AccountCode ?? ""), p.CategoryName,
                    detail.Description, amount, detail.ClearedStatus ?? "Pending",
                    new(p.Done, p.Total), new(lineObservations.Count, lineObservations));
                var group = groups[groupIndex];
                group.Lines.Add(line);
                group.Statuses.Add(detail.ClearedStatus);
                group.Amount += amount;
                groups[groupIndex] = group;
            }

            var vouchers = groups.Select((group, index) => new WorkbookVoucher(index + 1, group.Ref,
                group.Date, group.Lines.Count, group.Amount, Rollup(group.Statuses), group.Lines)).ToArray();
            int totalLines = vouchers.Sum(voucher => voucher.LineCount);
            return new(null, new(owner.ClientId, vouchers, totalLines), name);
        }
    }

    private async Task<List<Detail>?> ReadDetailsAsync(PostgresTransactionContext db, int sessionId, int clientId,
        CancellationToken ct)
    {
        var result = new List<Detail>();
        long bytes = 0;
        int rowLimit = checked(verificationOptions.Value.MaximumSourceRows + 1);
        await using var command = db.CreateCommand("""
            SELECT vd.id,vd.client_data_id,vd.transaction_ref,vd.cleared_status,vd.category_id_applied,
                   vd.category_name_applied,cd.extract_amount::text,cd.extract_date::text,cd.row_data::text
            FROM public.verification_details vd
            JOIN public.verification_sessions vs ON vs.id=vd.session_id AND vs.client_id=$2
            JOIN public.client_data cd ON cd.id=vd.client_data_id AND cd.client_id=vs.client_id
            WHERE vd.session_id=$1 AND vd.range_status='active'
            ORDER BY cd.id,vd.id LIMIT $3
            """);
        command.Parameters.AddWithValue(sessionId); command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(rowLimit);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            if (result.Count >= verificationOptions.Value.MaximumSourceRows) return null;
            string rowText = reader.IsDBNull(8) ? "{}" : reader.GetString(8);
            bytes = checked(bytes + Encoding.UTF8.GetByteCount(rowText));
            if (bytes > verificationOptions.Value.MaximumSourceBytes) return null;
            JsonElement rowData = ParseObject(rowText);
            string? accountCode = AliasText(rowData, AccountCodeAliases);
            JsonElement? description = AliasValue(rowData, DescriptionAliases);
            result.Add(new(reader.GetInt32(0), reader.GetInt32(1), NullableString(reader, 2), NullableString(reader, 3),
                NullableInt(reader, 4), NullableString(reader, 5), NullableString(reader, 6), NullableString(reader, 7),
                rowData, accountCode, description));
        }
        return result;
    }

    private static async Task<Dictionary<string, string>> ReadAccountNamesAsync(PostgresTransactionContext db,
        int clientId, string[] accountCodes, CancellationToken ct)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        if (accountCodes.Length == 0) return result;
        int? uploadId = null;
        await using (var upload = db.CreateCommand("""
            SELECT id FROM public.trial_balance_uploads WHERE client_id=$1 AND status='parsed'
            ORDER BY (period_end IS NULL),period_end DESC,uploaded_at DESC LIMIT 1
            """))
        {
            upload.Parameters.AddWithValue(clientId);
            var value = await upload.ExecuteScalarAsync(ct);
            if (value is int id) uploadId = id;
        }
        if (uploadId is null) return result;
        await using var command = db.CreateCommand("SELECT account_code,account_name FROM public.trial_balance_accounts WHERE upload_id=$1 AND account_code=ANY($2)");
        command.Parameters.AddWithValue(uploadId.Value); command.Parameters.AddWithValue(accountCodes);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) if (!reader.IsDBNull(1)) result[reader.GetString(0)] = reader.GetString(1);
        return result;
    }

    private static async Task<Dictionary<int, IReadOnlyList<Observation>>> ReadObservationsAsync(
        PostgresTransactionContext db, int tenantId, int clientId, int[] clientDataIds, CancellationToken ct)
    {
        var winners = new Dictionary<(int ClientDataId, int DocumentId), Observation>();
        if (clientDataIds.Length == 0) return [];
        await using var command = db.CreateCommand("""
            SELECT vd.client_data_id,o.document_id,o.observation_text,o.updated_at,o.id,
                   COALESCE(d.definition->>'name',CASE o.document_id
                       WHEN -1 THEN '[System] Account Head Recommendation'
                       WHEN -2 THEN '[System] Voucher Line Correctness' END)
            FROM public.voucher_line_observations o
            JOIN public.verification_details vd ON vd.id=o.voucher_line_id
            JOIN public.verification_sessions vs ON vs.id=vd.session_id AND vs.client_id=$3
            LEFT JOIN public.platform_audit_rule_entities d ON d.tenant_id=o.tenant_id
                AND d.entity_kind='documents' AND d.id=o.document_id
            WHERE vd.client_data_id=ANY($1) AND o.tenant_id=$2 AND o.rulebook_source='native'
            ORDER BY vd.client_data_id,o.document_id,o.updated_at DESC,o.id DESC
            """);
        command.Parameters.AddWithValue(clientDataIds); command.Parameters.AddWithValue(tenantId); command.Parameters.AddWithValue(clientId);
        await using (var reader = await command.ExecuteReaderAsync(ct))
        {
            while (await reader.ReadAsync(ct))
            {
                int clientDataId = reader.GetInt32(0), documentId = reader.GetInt32(1);
                string text = reader.IsDBNull(2) ? "" : reader.GetString(2).Trim();
                var key = (clientDataId, documentId);
                if (!winners.ContainsKey(key))
                    winners[key] = new(documentId, NullableString(reader, 5), text);
            }
        }
        return winners.GroupBy(pair => pair.Key.ClientDataId).ToDictionary(group => group.Key,
            group => (IReadOnlyList<Observation>)group.Where(pair => pair.Value.Text.Length > 0).OrderBy(pair => pair.Key.DocumentId)
                .Select(pair => pair.Value).ToArray());
    }

    private static string Rollup(IReadOnlyList<string?> statuses) => statuses.All(status => status == "Cleared")
        ? "Cleared" : statuses.Any(status => status == "Query") ? "Query" : "Pending";
    private static decimal ParseAmount(string? value) => decimal.TryParse(value, NumberStyles.Float,
        CultureInfo.InvariantCulture, out var amount) ? amount : 0m;
    private static string? AliasText(JsonElement row, IReadOnlyList<string> aliases)
    {
        foreach (string alias in aliases)
        {
            if (!row.TryGetProperty(alias, out var value) || value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
                continue;
            string text = value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.GetRawText();
            if (text.Length > 0) return text;
        }
        return null;
    }
    private static JsonElement? AliasValue(JsonElement row, IReadOnlyList<string> aliases)
    {
        foreach (string alias in aliases)
            if (row.TryGetProperty(alias, out var value) && value.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined)
                && !(value.ValueKind == JsonValueKind.String && value.GetString() == "")) return value.Clone();
        return null;
    }
    private static JsonElement ParseObject(string text)
    {
        try
        {
            using var document = JsonDocument.Parse(text);
            if (document.RootElement.ValueKind == JsonValueKind.Object) return document.RootElement.Clone();
        }
        catch (JsonException) { }
        using var empty = JsonDocument.Parse("{}");
        return empty.RootElement.Clone();
    }
    private static int? NullableInt(NpgsqlDataReader reader, int ordinal) => reader.IsDBNull(ordinal) ? null : reader.GetInt32(ordinal);
    private static string? NullableString(NpgsqlDataReader reader, int ordinal) => reader.IsDBNull(ordinal) ? null : reader.GetString(ordinal);
    private static string SafeFilename(string value)
    {
        var safe = new string(value.Select(character => char.IsAsciiLetterOrDigit(character)
            || character is '.' or '_' or '-' ? character : '_').ToArray()).Trim('_');
        return safe.Length == 0 ? "Verification" : safe[..Math.Min(safe.Length, 100)];
    }
    private static VerificationRuntimeResult Success(object value) => new("success", JsonSerializer.SerializeToElement(value));
    private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code);
}

internal sealed record VerificationWorkbookExportRow(int SrNo, string Date, string AccountCode,
    string AccountType, string AccountName, decimal Amount, string Procedures, string Observations, string Status);

internal sealed record VerificationWorkbookDownload(byte[] Bytes, string Filename, string ContentType);
