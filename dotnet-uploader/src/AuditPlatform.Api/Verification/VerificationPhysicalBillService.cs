using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Verification;

/// <summary>Tenant-scoped full-replacement Physical Bill evidence operations.</summary>
public sealed class VerificationPhysicalBillService(PostgresTransactions transactions, VerificationSessionService sessions,
    VerificationStatusService status, PostgresAuditWriter audit, IOptions<VerificationRuntimeOptions> options)
{
    private sealed record Bill(string? Number, DateOnly? Date, string? Vendor, List<Item> Items);
    private sealed record Item(string? Name, double? Quantity, double? Rate, double? Amount, int? SourceId, string? SourceType);
    internal sealed record PhysicalBillReadResult(int VoucherLineId, object[] Bills, int Count);

    public Task<VerificationRuntimeResult> GetAsync(VerifiedSession? session, int lineId, CancellationToken ct = default) => Run(async token =>
    {
        if (lineId < 1) return Invalid("invalid_verification_detail_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveAsync(db, session, lineId, inner, false); if (scope.Failure is not null) return scope.Failure;
            var siblings = await SiblingsAsync(db, scope.Line!.Value.ClientDataId, scope.Owner!.ClientId, inner);
            return Success(await ReadBillsAsync(db, scope.Owner!.TenantId, siblings, lineId, inner));
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SaveAsync(VerifiedSession? session, VerificationPhysicalBillSaveRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.VoucherLineId < 1 || request.DocumentId < 1 || request.Bills is null) return Invalid("invalid_physical_bill_request");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveAsync(db, session, request.VoucherLineId, inner, true); if (scope.Failure is not null) return scope.Failure;
            var parsed = await NormalizeAsync(db, scope.Owner!, request.Bills, inner); if (parsed.Error is not null) return Invalid(parsed.Error);
            if (!await ActivePhysicalBillAsync(db, scope.Owner!.TenantId, request.DocumentId, inner)) return NotFound("verification_physical_bill_not_found");
            await ReplaceAsync(db, scope.Owner.TenantId, request.VoucherLineId, request.DocumentId, parsed.Bills, inner);
            var statuses = await status.RecomputeAsync(db, scope.Owner!, [request.VoucherLineId], inner);
            await AuditAsync(db, scope.Owner!, "VERIFICATION_PHYSICAL_BILL_SAVED", new { voucher_line_id = request.VoucherLineId, document_id = request.DocumentId, bill_count = parsed.Bills.Count }, inner);
            var result = await ReadBillsAsync(db, scope.Owner.TenantId, [request.VoucherLineId], request.VoucherLineId, inner);
            return Success(new { voucher_line_id = request.VoucherLineId, bills = result.Bills, count = result.Count, cleared_status = statuses.GetValueOrDefault(request.VoucherLineId)?.ClearedStatus });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> PreviewAsync(VerifiedSession? session, int sourceRowId, string sourceType, CancellationToken ct = default) => Run(async token =>
    {
        if (session?.TenantId is not > 0 || sourceRowId < 1 || sourceType is not ("grn" or "vendor_bill")) return Invalid("invalid_physical_bill_source");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            int? selectedClient; await using (var auth = db.CreateCommand("""
                SELECT c.id FROM public.sessions s JOIN public.users u ON u.id=s.user_id AND u.is_active IS TRUE
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.tenant_id=$3 AND m.is_active IS TRUE
                JOIN public.clients c ON c.id=s.selected_client_id AND c.is_active IS TRUE
                JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=$3
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND s.selected_tenant_id=$3 AND s.context_version=$4
                  AND (m.tenant_role='admin' OR EXISTS(SELECT 1 FROM public.platform_engagement_assignments a
                    JOIN public.platform_audit_engagements e ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                    WHERE a.user_id=u.id AND a.firm_tenant_id=$3 AND e.client_id=c.id AND a.is_active IS TRUE AND e.is_active IS TRUE))
                """))
            { auth.Parameters.AddWithValue(session.Token); auth.Parameters.AddWithValue(session.UserId); auth.Parameters.AddWithValue(session.TenantId.Value); auth.Parameters.AddWithValue(session.ContextVersion); var value = await auth.ExecuteScalarAsync(inner); selectedClient = value is int id ? id : null; }
            if (selectedClient is null) return new VerificationRuntimeResult("unauthenticated", Code: "session_expired_or_context_changed");
            int? templateId = null; int sourceClient = 0; string? rowText = null; bool deleted = false;
            await using (var q = db.CreateCommand("SELECT c.client_id,c.template_id,c.row_data::text,c.is_deleted FROM public.client_data c JOIN public.platform_tenant_client_homes h ON h.client_id=c.client_id AND h.tenant_id=$2 WHERE c.id=$1"))
            { q.Parameters.AddWithValue(sourceRowId); q.Parameters.AddWithValue(session.TenantId.Value); await using var r = await q.ExecuteReaderAsync(inner); if (!await r.ReadAsync(inner)) return NotFound("verification_source_row_not_found"); sourceClient = r.GetInt32(0); templateId = r.GetInt32(1); rowText = r.GetString(2); deleted = r.GetBoolean(3); }
            if (sourceClient != selectedClient.Value) return NotFound("verification_source_row_not_found"); if (deleted) return Invalid("verification_source_row_deleted");
            string? templateName; await using (var t = db.CreateCommand("SELECT t.template_name FROM public.templates t JOIN public.platform_tenant_templates d ON d.template_id=t.id AND d.tenant_id=$2 WHERE t.id=$1 AND t.is_active IS TRUE"))
            { t.Parameters.AddWithValue(templateId!.Value); t.Parameters.AddWithValue(session.TenantId.Value); templateName = await t.ExecuteScalarAsync(inner) as string; }
            string expected = sourceType == "grn" ? "Store Receiving" : "Vendor Bill"; if (templateName is null || !string.Equals(templateName.Trim(), expected, StringComparison.OrdinalIgnoreCase)) return Invalid("verification_source_template_mismatch");
            var columns = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase); await using (var q = db.CreateCommand("SELECT c.display_name,p.storage_key FROM public.template_column_mappings p JOIN public.platform_tenant_columns c ON c.tenant_id=p.tenant_id AND c.column_id=p.column_id WHERE p.tenant_id=$1 AND p.template_id=$2")) { q.Parameters.AddWithValue(session.TenantId.Value); q.Parameters.AddWithValue(templateId.Value); await using var r = await q.ExecuteReaderAsync(inner); while (await r.ReadAsync(inner)) columns[r.GetString(0).Trim()] = r.GetString(1); }
            using var dataDoc = JsonDocument.Parse(rowText!); var data = dataDoc.RootElement; object? Resolve(string name) { if (!columns.TryGetValue(name, out var key) || !data.TryGetProperty(key, out var value) || value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined || string.IsNullOrWhiteSpace(value.ToString())) return null; return value.ValueKind == JsonValueKind.String ? value.GetString() : value.ToString(); }
            object? vendor = Resolve("Vendor"), bill = Resolve(sourceType == "grn" ? "Bill no" : "vendor Bill no"); double? quantity = Number(Resolve("Quantity")), rate = Number(Resolve("Rate")); int precision = await PrecisionAsync(db, session.TenantId.Value, inner); double product = quantity.GetValueOrDefault() * rate.GetValueOrDefault(); if (quantity.HasValue && rate.HasValue && !double.IsFinite(product)) return Invalid("verification_source_amount_overflow"); double? amount = quantity.HasValue && rate.HasValue ? Math.Round(product, precision, MidpointRounding.ToEven) : null;
            var resolvedFields = new List<string>(); var skipped = new List<object>(); foreach (var pair in new[] { ("Vendor Name", vendor), ("Bill No", bill), ("Quantity", (object?)quantity), ("Rate", (object?)rate) }) { if (pair.Item2 is not null) resolvedFields.Add(pair.Item1); else skipped.Add(new { field = pair.Item1, reason = "not configured or blank/non-numeric on source row" }); }
            return Success(new { source_client_data_id = sourceRowId, source_client_id = sourceClient, source_type = sourceType, source_template_id = templateId, source_template_name = templateName, vendor_name = vendor, bill_no = bill, quantity, rate, amount_preview = amount, fields_resolved = resolvedFields, fields_skipped = skipped });
        }, token);
    }, ct);

    internal async Task<VerificationRuntimeResult?> SaveWithinTransactionAsync(PostgresTransactionContext db, VerificationReviewContext owner,
        int lineId, VerificationPhysicalBillSaveRequest request, CancellationToken ct)
    {
        if (request.DocumentId < 1 || request.VoucherLineId != lineId || request.Bills is null) return Invalid("invalid_physical_bill_request");
        if (!await ActivePhysicalBillAsync(db, owner.TenantId, request.DocumentId, ct)) return NotFound("verification_physical_bill_not_found");
        var parsed = await NormalizeAsync(db, owner, request.Bills, ct); if (parsed.Error is not null) return Invalid(parsed.Error);
        await ReplaceAsync(db, owner.TenantId, lineId, request.DocumentId, parsed.Bills, ct); return null;
    }

    private async Task<(VerificationRuntimeResult? Failure, VerificationReviewContext? Owner, (int SessionId, int ClientDataId)? Line)> ResolveAsync(PostgresTransactionContext db, VerifiedSession? session, int line, CancellationToken ct, bool write)
    {
        int? sid; await using (var q = db.CreateCommand("SELECT session_id FROM public.verification_details WHERE id=$1")) { q.Parameters.AddWithValue(line); sid = await q.ExecuteScalarAsync(ct) as int?; }
        if (sid is null) return (NotFound("verification_detail_not_found"), null, null);
        var resolved = await sessions.ResolveReviewContextAsync(db, session, sid.Value, ct); if (resolved.Failure is not null) return (resolved.Failure, null, null);
        await using var q2 = db.CreateCommand("SELECT d.session_id,d.client_data_id FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id AND s.client_id=$2 JOIN public.client_data c ON c.id=d.client_data_id AND c.client_id=s.client_id JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id AND h.tenant_id=$3 WHERE d.id=$1 AND d.range_status='active'" + (write ? " FOR UPDATE OF d" : "")); q2.Parameters.AddWithValue(line); q2.Parameters.AddWithValue(resolved.Context!.ClientId); q2.Parameters.AddWithValue(resolved.Context.TenantId); await using var r = await q2.ExecuteReaderAsync(ct); return await r.ReadAsync(ct) ? (null, resolved.Context, (r.GetInt32(0), r.GetInt32(1))) : (NotFound("verification_detail_not_found"), null, null);
    }
    private static async Task<int[]> SiblingsAsync(PostgresTransactionContext db, int clientData, int client, CancellationToken ct) { var ids = new List<int>(); await using var q = db.CreateCommand("SELECT d.id FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id AND s.client_id=$2 WHERE d.client_data_id=$1 ORDER BY d.id"); q.Parameters.AddWithValue(clientData); q.Parameters.AddWithValue(client); await using var r = await q.ExecuteReaderAsync(ct); while (await r.ReadAsync(ct)) ids.Add(r.GetInt32(0)); return ids.ToArray(); }
    private static async Task<bool> ActivePhysicalBillAsync(PostgresTransactionContext db, int tenant, int id, CancellationToken ct) { await using var q = db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='documents' AND id=$2 AND is_active IS TRUE AND definition->>'document_type'='physical_bill')"); q.Parameters.AddWithValue(tenant); q.Parameters.AddWithValue(id); return (bool)(await q.ExecuteScalarAsync(ct) ?? false); }
    private async Task<(List<Bill> Bills, string? Error)> NormalizeAsync(PostgresTransactionContext db, VerificationReviewContext owner, List<VerificationPhysicalBillInput> raw, CancellationToken ct)
    {
        var output = new List<Bill>(); int places = await PrecisionAsync(db, owner.TenantId, ct);
        foreach (var b in raw)
        {
            if (b is null || b.Items is null) return ([], "invalid_physical_bill_entry"); DateOnly? date = null; if (!string.IsNullOrWhiteSpace(b.BillDate)) { if (!DateOnly.TryParseExact(b.BillDate, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsed)) return ([], "invalid_physical_bill_date"); date = parsed; }
            var items = new List<Item>(); foreach (var i in b.Items) { if (i is null) return ([], "invalid_physical_bill_item"); double? qty = ParseNumber(i.Quantity, out bool badQ), rate = ParseNumber(i.Rate, out bool badR); if (badQ || badR) return ([], "invalid_physical_bill_numeric"); if (string.IsNullOrWhiteSpace(i.ItemName) && qty is null && rate is null && i.SourceDocumentRowId is null && string.IsNullOrWhiteSpace(i.SourceTemplate)) continue; if (i.SourceTemplate is not null && i.SourceTemplate is not ("grn" or "vendor_bill")) return ([], "invalid_physical_bill_source_type"); if (i.SourceDocumentRowId is int source) { if (!await SourceBelongsAsync(db, owner.ClientId, source, ct)) return ([], "verification_source_row_not_found"); if (i.SourceTemplate is not null && !await SourceMatchesTemplateAsync(db, owner.TenantId, source, i.SourceTemplate, ct)) return ([], "verification_source_template_mismatch"); } double product = qty.GetValueOrDefault() * rate.GetValueOrDefault(); if (qty.HasValue && rate.HasValue && !double.IsFinite(product)) return ([], "invalid_physical_bill_numeric"); double? amount = qty.HasValue && rate.HasValue ? Math.Round(product, places, MidpointRounding.ToEven) : null; items.Add(new(string.IsNullOrWhiteSpace(i.ItemName) ? null : i.ItemName.Trim(), qty, rate, amount, i.SourceDocumentRowId, i.SourceTemplate)); }
            string? no = string.IsNullOrWhiteSpace(b.BillNo) ? null : b.BillNo.Trim(), vendor = string.IsNullOrWhiteSpace(b.VendorName) ? null : b.VendorName.Trim(); if (no is null && date is null && vendor is null && items.Count == 0) continue; output.Add(new(no, date, vendor, items));
        }
        return (output, null);
    }
    private static double? ParseNumber(JsonElement e, out bool invalid) { invalid = false; if (e.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined || e.ValueKind == JsonValueKind.String && string.IsNullOrWhiteSpace(e.GetString())) return null; string s = e.ValueKind == JsonValueKind.String ? e.GetString()! : e.GetRawText(); if (double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out double n) && double.IsFinite(n)) return n; invalid = true; return null; }
    private static double? Number(object? value) => value is not null && double.TryParse(Convert.ToString(value, CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture, out var n) && double.IsFinite(n) ? n : null;
    private static async Task<bool> SourceBelongsAsync(PostgresTransactionContext db, int client, int source, CancellationToken ct) { await using var q = db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.client_data WHERE id=$1 AND client_id=$2 AND is_deleted IS FALSE)"); q.Parameters.AddWithValue(source); q.Parameters.AddWithValue(client); return (bool)(await q.ExecuteScalarAsync(ct) ?? false); }
    private static async Task<bool> SourceMatchesTemplateAsync(PostgresTransactionContext db, int tenant, int source, string sourceType, CancellationToken ct) { string expected = sourceType == "grn" ? "Store Receiving" : "Vendor Bill"; await using var q = db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.client_data c JOIN public.templates t ON t.id=c.template_id AND t.is_active IS TRUE JOIN public.platform_tenant_templates p ON p.template_id=t.id AND p.tenant_id=$2 WHERE c.id=$1 AND lower(btrim(t.template_name))=lower($3))"); q.Parameters.AddWithValue(source); q.Parameters.AddWithValue(tenant); q.Parameters.AddWithValue(expected); return (bool)(await q.ExecuteScalarAsync(ct) ?? false); }
    private static async Task<int> PrecisionAsync(PostgresTransactionContext db, int tenant, CancellationToken ct) { await using var q = db.CreateCommand("SELECT value FROM public.platform_tenant_settings WHERE tenant_id=$1 AND key='rate_precision'"); q.Parameters.AddWithValue(tenant); string? raw = await q.ExecuteScalarAsync(ct) as string; return int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out int p) && p is 0 or 2 or 4 ? p : 0; }
    private static async Task ReplaceAsync(PostgresTransactionContext db, int tenant, int line, int document, List<Bill> bills, CancellationToken ct)
    {
        await using (var del = db.CreateCommand("DELETE FROM public.voucher_line_physical_bills WHERE rulebook_source='native' AND tenant_id=$1 AND voucher_line_id=$2 AND document_id=$3")) { del.Parameters.AddWithValue(tenant); del.Parameters.AddWithValue(line); del.Parameters.AddWithValue(document); await del.ExecuteNonQueryAsync(ct); }
        for (int bi = 0; bi < bills.Count; bi++) { var b = bills[bi]; int billId; await using (var ins = db.CreateCommand("INSERT INTO public.voucher_line_physical_bills(tenant_id,rulebook_source,voucher_line_id,document_id,bill_no,bill_date,vendor_name,sort_order,updated_at) VALUES($1,'native',$2,$3,$4,$5,$6,$7,now()) RETURNING id")) { ins.Parameters.AddWithValue(tenant); ins.Parameters.AddWithValue(line); ins.Parameters.AddWithValue(document); ins.Parameters.AddWithValue((object?)b.Number ?? DBNull.Value); ins.Parameters.AddWithValue((object?)b.Date ?? DBNull.Value); ins.Parameters.AddWithValue((object?)b.Vendor ?? DBNull.Value); ins.Parameters.AddWithValue(bi); billId = (int)(await ins.ExecuteScalarAsync(ct))!; } for (int ii = 0; ii < b.Items.Count; ii++) { var i = b.Items[ii]; await using var item = db.CreateCommand("INSERT INTO public.voucher_line_physical_bill_items(bill_id,item_name,quantity,rate,amount,source_document_row_id,source_template,sort_order,updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,now())"); item.Parameters.AddWithValue(billId); item.Parameters.AddWithValue((object?)i.Name ?? DBNull.Value); item.Parameters.AddWithValue((object?)i.Quantity ?? DBNull.Value); item.Parameters.AddWithValue((object?)i.Rate ?? DBNull.Value); item.Parameters.AddWithValue((object?)i.Amount ?? DBNull.Value); item.Parameters.AddWithValue((object?)i.SourceId ?? DBNull.Value); item.Parameters.AddWithValue((object?)i.SourceType ?? DBNull.Value); item.Parameters.AddWithValue(ii); await item.ExecuteNonQueryAsync(ct); } }
    }
    internal static async Task<PhysicalBillReadResult> ReadBillsAsync(PostgresTransactionContext db, int tenant, int[] siblings, int? requestedLineId, CancellationToken ct)
    {
        var bills = new List<Dictionary<string, object?>>(); await using var q = db.CreateCommand("""
            WITH winners AS (SELECT DISTINCT ON (document_id) document_id,voucher_line_id FROM public.voucher_line_physical_bills WHERE rulebook_source='native' AND tenant_id=$1 AND voucher_line_id=ANY($2) ORDER BY document_id,updated_at DESC,voucher_line_id DESC)
            SELECT b.id,b.voucher_line_id,b.document_id,b.bill_no,b.bill_date,b.vendor_name,b.sort_order,b.created_at,b.updated_at,d.definition->>'name',d.is_active,i.id,i.item_name,i.quantity,i.rate,i.amount,i.source_document_row_id,i.source_template,i.sort_order,i.created_at,i.updated_at
            FROM winners w JOIN public.voucher_line_physical_bills b ON b.tenant_id=$1 AND b.rulebook_source='native' AND b.voucher_line_id=w.voucher_line_id AND b.document_id=w.document_id
            LEFT JOIN public.platform_audit_rule_entities d ON d.tenant_id=b.tenant_id AND d.entity_kind='documents' AND d.id=b.document_id LEFT JOIN public.voucher_line_physical_bill_items i ON i.bill_id=b.id
            ORDER BY b.document_id,b.sort_order,b.id,i.sort_order,i.id
        """); q.Parameters.AddWithValue(tenant); q.Parameters.AddWithValue(siblings); await using var r = await q.ExecuteReaderAsync(ct); var map = new Dictionary<int, Dictionary<string, object?>>(); while (await r.ReadAsync(ct)) { int id = r.GetInt32(0); if (!map.TryGetValue(id, out var b)) { b = new() { ["id"] = id, ["voucher_line_id"] = r.GetInt32(1), ["document_id"] = r.GetInt32(2), ["bill_no"] = r.IsDBNull(3) ? null : r.GetString(3), ["bill_date"] = r.IsDBNull(4) ? null : r.GetFieldValue<DateOnly>(4).ToString("yyyy-MM-dd"), ["vendor_name"] = r.IsDBNull(5) ? null : r.GetString(5), ["sort_order"] = r.GetInt32(6), ["created_at"] = r.GetValue(7), ["updated_at"] = r.GetValue(8), ["document_name"] = r.IsDBNull(9) ? null : r.GetString(9), ["is_active"] = r.IsDBNull(10) ? (bool?)null : r.GetBoolean(10), ["items"] = new List<object>(), ["bill_total_sum"] = 0d, ["bill_total_complete"] = true }; map.Add(id, b); } if (!r.IsDBNull(11)) { double? amount = r.IsDBNull(15) ? (double?)null : Convert.ToDouble(r.GetValue(15), CultureInfo.InvariantCulture); ((List<object>)b["items"]!).Add(new { id = r.GetInt32(11), item_name = r.IsDBNull(12) ? null : r.GetString(12), quantity = r.IsDBNull(13) ? (double?)null : Convert.ToDouble(r.GetValue(13), CultureInfo.InvariantCulture), rate = r.IsDBNull(14) ? (double?)null : Convert.ToDouble(r.GetValue(14), CultureInfo.InvariantCulture), amount, source_document_row_id = r.IsDBNull(16) ? (int?)null : r.GetInt32(16), source_template = r.IsDBNull(17) ? null : r.GetString(17), sort_order = r.GetInt32(18), created_at = r.GetValue(19), updated_at = r.GetValue(20) }); if (amount.HasValue) b["bill_total_sum"] = (double)b["bill_total_sum"]! + amount.Value; else b["bill_total_complete"] = false; } }
        foreach (var bill in map.Values) { bool complete = (bool)bill["bill_total_complete"]!; bill["bill_total"] = ((List<object>)bill["items"]!).Count > 0 && complete ? bill["bill_total_sum"] : null; bill.Remove("bill_total_sum"); bill.Remove("bill_total_complete"); }
        return new(requestedLineId ?? siblings.FirstOrDefault(), map.Values.ToArray(), map.Count);
    }
    private async Task AuditAsync(PostgresTransactionContext db, VerificationReviewContext owner, string action, object data, CancellationToken ct) => await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), new LogActor("authenticated", owner.UserId, owner.Username), owner.ClientId, action, "Verification", JsonSerializer.SerializeToElement(data)), ct);
    private async Task<T> Run<T>(Func<CancellationToken, Task<T>> action, CancellationToken ct) { if (!options.Value.Enabled) return (T)(object)new VerificationRuntimeResult("disabled", Code: "verification_runtime_disabled"); try { return await action(ct); } catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; } catch (PostgresException e) when (e.SqlState == PostgresErrorCodes.SerializationFailure || e.SqlState == PostgresErrorCodes.DeadlockDetected) { return (T)(object)new VerificationRuntimeResult("conflict", Code: "verification_transaction_retry"); } }
    private static VerificationRuntimeResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value)); private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code); private static VerificationRuntimeResult NotFound(string code) => new("not_found", Code: code);
}
