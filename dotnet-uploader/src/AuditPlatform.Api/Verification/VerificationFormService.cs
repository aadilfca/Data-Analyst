using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Verification;

/// <summary>Tenant-scoped full-replacement Form evidence operations.</summary>
public sealed class VerificationFormService(PostgresTransactions transactions, VerificationSessionService sessions,
    VerificationStatusService status, PostgresAuditWriter audit, IOptions<VerificationRuntimeOptions> options)
{
    private sealed record Field(int Id, string Name, string Type, string Scope, string[] Operands, string? LinkedColumn);
    private sealed record Value(int FieldId, string? Text, DateOnly? Date, decimal? Number);
    private sealed record Entry(int Group, string Scope, int Sort, List<Value> Values);

    public Task<VerificationRuntimeResult> GetAsync(VerifiedSession? session, int lineId, int documentId,
        CancellationToken ct = default) => Run(async token =>
    {
        if (lineId < 1 || documentId < 1) return Invalid("invalid_form_request");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveAsync(db, session, lineId, inner, false);
            if (scope.Failure is not null) return scope.Failure;
            if (!await ActiveFormAsync(db, scope.Owner!.TenantId, documentId, inner)) return NotFound("verification_form_not_found");
            var entries = await ReadEntriesAsync(db, scope.Owner.TenantId, [lineId], documentId, inner);
            return Success(new { voucher_line_id = lineId, document_id = documentId, entries });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SaveAsync(VerifiedSession? session, VerificationFormSaveRequest request,
        CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.VoucherLineId < 1 || request.DocumentId < 1 || request.Entries is null)
            return Invalid("invalid_form_request");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveAsync(db, session, request.VoucherLineId, inner, true);
            if (scope.Failure is not null) return scope.Failure;
            if (!await ActiveFormAsync(db, scope.Owner!.TenantId, request.DocumentId, inner)) return NotFound("verification_form_not_found");
            var fields = await ReadFieldsAsync(db, scope.Owner.TenantId, request.DocumentId, inner);
            if (!TryNormalize(request.Entries, fields, out var entries, out string? error)) return Invalid(error!);
            await using (var delete = db.CreateCommand("DELETE FROM public.voucher_line_form_entries WHERE rulebook_source='native' AND tenant_id=$1 AND voucher_line_id=$2 AND document_id=$3"))
            { delete.Parameters.AddWithValue(scope.Owner.TenantId); delete.Parameters.AddWithValue(request.VoucherLineId); delete.Parameters.AddWithValue(request.DocumentId); await delete.ExecuteNonQueryAsync(inner); }
            foreach (var entry in entries)
            {
                int id;
                await using (var insert = db.CreateCommand("INSERT INTO public.voucher_line_form_entries(tenant_id,rulebook_source,voucher_line_id,document_id,bill_group_index,row_scope,sort_order,updated_at) VALUES($1,'native',$2,$3,$4,$5,$6,now()) RETURNING id"))
                {
                    insert.Parameters.AddWithValue(scope.Owner.TenantId); insert.Parameters.AddWithValue(request.VoucherLineId); insert.Parameters.AddWithValue(request.DocumentId);
                    insert.Parameters.AddWithValue(entry.Group); insert.Parameters.AddWithValue(entry.Scope); insert.Parameters.AddWithValue(entry.Sort);
                    id = (int)(await insert.ExecuteScalarAsync(inner))!;
                }
                foreach (var value in entry.Values)
                {
                    await using var insertValue = db.CreateCommand("INSERT INTO public.voucher_line_form_values(entry_id,field_id,value_text,value_date,value_numeric,updated_at) VALUES($1,$2,$3,$4,$5,now())");
                    insertValue.Parameters.AddWithValue(id); insertValue.Parameters.AddWithValue(value.FieldId);
                    insertValue.Parameters.AddWithValue((object?)value.Text ?? DBNull.Value);
                    insertValue.Parameters.AddWithValue((object?)value.Date ?? DBNull.Value);
                    insertValue.Parameters.AddWithValue((object?)value.Number ?? DBNull.Value);
                    await insertValue.ExecuteNonQueryAsync(inner);
                }
            }
            var statuses = await status.RecomputeAsync(db, scope.Owner!, [request.VoucherLineId], inner);
            await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), new LogActor("authenticated", scope.Owner!.UserId, scope.Owner.Username),
                scope.Owner.ClientId, "VERIFICATION_FORM_SAVED", "Verification", JsonSerializer.SerializeToElement(new { voucher_line_id = request.VoucherLineId, document_id = request.DocumentId, entry_count = entries.Count })), inner);
            var saved = await ReadEntriesAsync(db, scope.Owner.TenantId, [request.VoucherLineId], request.DocumentId, inner);
            return Success(new
            {
                voucher_line_id = request.VoucherLineId,
                document_id = request.DocumentId,
                entries = saved,
                cleared_status = statuses.GetValueOrDefault(request.VoucherLineId)?.ClearedStatus
            });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> CopySourcesAsync(VerifiedSession? session, int lineId, int documentId,
        CancellationToken ct = default) => Run(async token =>
    {
        if (lineId < 1 || documentId < 1) return Invalid("invalid_form_request");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveAsync(db, session, lineId, inner, false);
            if (scope.Failure is not null) return scope.Failure;
            int? template;
            await using (var form = db.CreateCommand("SELECT NULLIF(definition->>'template_id','')::int FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='documents' AND id=$2 AND is_active IS TRUE AND definition->>'document_type'='form'"))
            { form.Parameters.AddWithValue(scope.Owner!.TenantId); form.Parameters.AddWithValue(documentId); var value = await form.ExecuteScalarAsync(inner); template = value is int id ? id : null; }
            if (!await ActiveFormAsync(db, scope.Owner!.TenantId, documentId, inner)) return NotFound("verification_form_not_found");
            var fields = await ReadFieldsAsync(db, scope.Owner.TenantId, documentId, inner);
            var result = new List<object>();
            if (template is not null)
            {
                await using var command = db.CreateCommand("""
                SELECT l.client_data_id,l.document_id,d.definition->>'name',cd.row_data::text
                FROM public.verification_details a
                JOIN public.verification_details v ON v.client_data_id=a.client_data_id AND v.range_status='active'
                JOIN public.verification_sessions s ON s.id=v.session_id AND s.client_id=$2
                JOIN public.voucher_line_documents l ON l.voucher_line_id=v.id AND l.status='active' AND l.rulebook_source='native' AND l.tenant_id=$4
                JOIN public.platform_audit_rule_entities d ON d.tenant_id=$4 AND d.entity_kind='documents' AND d.id=l.document_id AND d.is_active IS TRUE AND (d.definition->>'template_id')::int=$3
                JOIN public.client_data cd ON cd.id=l.client_data_id AND cd.client_id=$2 AND cd.is_deleted IS FALSE
                WHERE a.id=$1 ORDER BY l.linked_at,l.id
                """);
                command.Parameters.AddWithValue(lineId); command.Parameters.AddWithValue(scope.Owner!.ClientId); command.Parameters.AddWithValue(template.Value); command.Parameters.AddWithValue(scope.Owner.TenantId);
                await using var reader = await command.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner))
                {
                    using var row = JsonDocument.Parse(reader.GetString(3));
                    result.Add(new { source_client_data_id = reader.GetInt32(0), source_document_id = reader.GetInt32(1), source_document_name = reader.GetString(2), row_data = row.RootElement.Clone() });
                }
            }
            return Success(new { voucher_line_id = lineId, document_id = documentId, template_id = template, fields = fields.Select(f => new { id = f.Id, field_name = f.Name, field_type = f.Type, field_scope = f.Scope, linked_column = f.LinkedColumn }), sources = result });
        }, token);
    }, ct);

    internal async Task<VerificationRuntimeResult?> SaveWithinTransactionAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, int lineId, VerificationFormSaveRequest request, CancellationToken ct)
    {
        if (request.DocumentId < 1 || request.Entries is null || request.VoucherLineId != lineId) return Invalid("invalid_form_request");
        if (!await ActiveFormAsync(db, owner.TenantId, request.DocumentId, ct)) return NotFound("verification_form_not_found");
        var fields = await ReadFieldsAsync(db, owner.TenantId, request.DocumentId, ct);
        if (!TryNormalize(request.Entries, fields, out var entries, out var error)) return Invalid(error!);
        await using (var delete = db.CreateCommand("DELETE FROM public.voucher_line_form_entries WHERE rulebook_source='native' AND tenant_id=$1 AND voucher_line_id=$2 AND document_id=$3"))
        { delete.Parameters.AddWithValue(owner.TenantId); delete.Parameters.AddWithValue(lineId); delete.Parameters.AddWithValue(request.DocumentId); await delete.ExecuteNonQueryAsync(ct); }
        foreach (var entry in entries)
        {
            int id;
            await using (var insert = db.CreateCommand("INSERT INTO public.voucher_line_form_entries(tenant_id,rulebook_source,voucher_line_id,document_id,bill_group_index,row_scope,sort_order,updated_at) VALUES($1,'native',$2,$3,$4,$5,$6,now()) RETURNING id"))
            { insert.Parameters.AddWithValue(owner.TenantId); insert.Parameters.AddWithValue(lineId); insert.Parameters.AddWithValue(request.DocumentId); insert.Parameters.AddWithValue(entry.Group); insert.Parameters.AddWithValue(entry.Scope); insert.Parameters.AddWithValue(entry.Sort); id = (int)(await insert.ExecuteScalarAsync(ct))!; }
            foreach (var value in entry.Values)
            { await using var ins = db.CreateCommand("INSERT INTO public.voucher_line_form_values(entry_id,field_id,value_text,value_date,value_numeric,updated_at) VALUES($1,$2,$3,$4,$5,now())"); ins.Parameters.AddWithValue(id); ins.Parameters.AddWithValue(value.FieldId); ins.Parameters.AddWithValue((object?)value.Text ?? DBNull.Value); ins.Parameters.AddWithValue((object?)value.Date ?? DBNull.Value); ins.Parameters.AddWithValue((object?)value.Number ?? DBNull.Value); await ins.ExecuteNonQueryAsync(ct); }
        }
        return null;
    }

    private async Task<(VerificationRuntimeResult? Failure, VerificationReviewContext? Owner)> ResolveAsync(PostgresTransactionContext db,
        VerifiedSession? session, int lineId, CancellationToken ct, bool writing)
    {
        int? sessionId; await using (var find = db.CreateCommand("SELECT session_id FROM public.verification_details WHERE id=$1")) { find.Parameters.AddWithValue(lineId); sessionId = await find.ExecuteScalarAsync(ct) as int?; }
        if (sessionId is null) return (NotFound("verification_detail_not_found"), null);
        var resolved = await sessions.ResolveReviewContextAsync(db, session, sessionId.Value, ct);
        if (resolved.Failure is not null) return (resolved.Failure, null);
        await using var check = db.CreateCommand("SELECT 1 FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id AND s.client_id=$2 JOIN public.client_data c ON c.id=d.client_data_id AND c.client_id=s.client_id JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id AND h.tenant_id=$3 WHERE d.id=$1 AND d.range_status='active'" + (writing ? " FOR UPDATE OF d" : ""));
        check.Parameters.AddWithValue(lineId); check.Parameters.AddWithValue(resolved.Context!.ClientId); check.Parameters.AddWithValue(resolved.Context.TenantId);
        if (await check.ExecuteScalarAsync(ct) is null) return (NotFound("verification_detail_not_found"), null);
        return (null, resolved.Context);
    }
    private static async Task<bool> ActiveFormAsync(PostgresTransactionContext db, int tenant, int document, CancellationToken ct)
    { await using var q = db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='documents' AND id=$2 AND is_active IS TRUE AND definition->>'document_type'='form')"); q.Parameters.AddWithValue(tenant); q.Parameters.AddWithValue(document); return (bool)(await q.ExecuteScalarAsync(ct) ?? false); }
    private static async Task<List<Field>> ReadFieldsAsync(PostgresTransactionContext db, int tenant, int document, CancellationToken ct)
    {
        var fields = new List<Field>(); await using var q = db.CreateCommand("SELECT (f->>'id')::int,f->>'field_name',COALESCE(f->>'field_type','text'),COALESCE(f->>'field_scope','header'),(f->'arithmetic_operands')::text,f->>'linked_column' FROM public.platform_audit_rule_entities d CROSS JOIN LATERAL jsonb_array_elements(COALESCE(d.definition->'form_fields','[]'::jsonb)) f WHERE d.tenant_id=$1 AND d.entity_kind='documents' AND d.id=$2 AND d.is_active IS TRUE AND d.definition->>'document_type'='form' ORDER BY COALESCE((f->>'sort_order')::int,0),(f->>'id')::int"); q.Parameters.AddWithValue(tenant); q.Parameters.AddWithValue(document); await using var r = await q.ExecuteReaderAsync(ct);
        while (await r.ReadAsync(ct)) { string[] operands = []; if (!r.IsDBNull(4)) { try { using var j = JsonDocument.Parse(r.GetString(4)); operands = j.RootElement.ValueKind == JsonValueKind.Array ? j.RootElement.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.String).Select(x => x.GetString()!).ToArray() : []; } catch (JsonException) { } } fields.Add(new(r.GetInt32(0), r.GetString(1), r.GetString(2), r.GetString(3), operands, r.IsDBNull(5) ? null : r.GetString(5))); }
        return fields;
    }
    private static bool TryNormalize(List<VerificationFormEntryInput> raw, List<Field> fields, out List<Entry> entries, out string? error)
    {
        entries = []; error = null; var byId = fields.ToDictionary(f => f.Id);
        for (int i = 0; i < raw.Count; i++)
        {
            var item = raw[i]; if (item is null || item.BillGroupIndex < 0 || item.SortOrder < -1 || item.RowScope is not ("header" or "item") || item.Values is null) { error = "invalid_form_entry"; return false; }
            var vals = new Dictionary<int, Value>();
            foreach (var input in item.Values)
            {
                if (input is null || !byId.TryGetValue(input.FieldId, out var field)) { error = "form_field_not_in_document"; return false; }
                if (field.Scope != item.RowScope) { error = "form_field_scope_mismatch"; return false; }
                if (vals.ContainsKey(field.Id)) { error = "duplicate_form_field"; return false; }
                if (field.Type == "arithmetic_multiply") continue;
                Value? value = NormalizeValue(field, input.Value, out error); if (value is null) { if (error is not null) return false; continue; }
                vals[field.Id] = value;
            }
            foreach (var field in fields.Where(f => f.Scope == item.RowScope && f.Type == "arithmetic_multiply"))
            {
                var byName = fields.ToDictionary(f => f.Name.Trim(), f => f, StringComparer.OrdinalIgnoreCase);
                var operands = field.Operands.Where(byName.ContainsKey).Select(name => byName[name]).ToArray();
                if (operands.Length != 2 || operands.Any(o => o.Type != "numeric" || o.Scope != field.Scope)) continue;
                if (vals.TryGetValue(operands[0].Id, out var a) && vals.TryGetValue(operands[1].Id, out var b) && a.Number.HasValue && b.Number.HasValue)
                { decimal product; try { product = checked(a.Number.Value * b.Number.Value); } catch (OverflowException) { error = "form_numeric_overflow"; return false; } vals[field.Id] = new(field.Id, null, null, product); }
            }
            var clean = vals.Values.OrderBy(v => fields.FindIndex(f => f.Id == v.FieldId)).ToList();
            if (clean.Count > 0) entries.Add(new(item.BillGroupIndex, item.RowScope, item.SortOrder < 0 ? i : item.SortOrder, clean));
        }
        return true;
    }
    private static Value? NormalizeValue(Field field, JsonElement input, out string? error)
    {
        error = null; if (input.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined || input.ValueKind == JsonValueKind.String && string.IsNullOrWhiteSpace(input.GetString())) return null;
        string text = input.ValueKind == JsonValueKind.String ? input.GetString()! : input.GetRawText();
        if (field.Type == "date")
        { if (!DateOnly.TryParseExact(text, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var date)) { error = "invalid_form_date"; return null; } return new(field.Id, null, date, null); }
        if (field.Type == "numeric")
        { if (!decimal.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var number)) { error = "invalid_form_numeric"; return null; } return new(field.Id, null, null, number); }
        if (field.Type != "text") { error = "invalid_form_field_type"; return null; }
        text = text.Trim(); return string.IsNullOrWhiteSpace(text) ? null : new(field.Id, text, null, null);
    }
    internal static async Task<List<object>> ReadEntriesAsync(PostgresTransactionContext db, int tenant, int[] lines, int document, CancellationToken ct)
    {
        var entries = new SortedDictionary<(int Group, int Sort, int Id), Dictionary<string, object?>>();
        await using var q = db.CreateCommand("WITH winning AS (SELECT voucher_line_id FROM public.voucher_line_form_entries WHERE rulebook_source='native' AND tenant_id=$1 AND voucher_line_id=ANY($2) AND document_id=$3 ORDER BY updated_at DESC,voucher_line_id DESC LIMIT 1) SELECT e.id,e.voucher_line_id,e.document_id,e.bill_group_index,e.row_scope,e.sort_order,(f->>'id')::int,f->>'field_name',f->>'field_type',v.value_text,v.value_date,v.value_numeric FROM public.voucher_line_form_entries e LEFT JOIN public.voucher_line_form_values v ON v.entry_id=e.id LEFT JOIN public.platform_audit_rule_entities d ON d.tenant_id=e.tenant_id AND d.entity_kind='documents' AND d.id=e.document_id LEFT JOIN LATERAL jsonb_array_elements(COALESCE(d.definition->'form_fields','[]'::jsonb)) f ON (f->>'id')::int=v.field_id WHERE e.rulebook_source='native' AND e.tenant_id=$1 AND e.voucher_line_id=(SELECT voucher_line_id FROM winning) AND e.document_id=$3 ORDER BY e.bill_group_index,e.sort_order,e.id,COALESCE((f->>'sort_order')::int,0),(f->>'id')::int"); q.Parameters.AddWithValue(tenant); q.Parameters.AddWithValue(lines); q.Parameters.AddWithValue(document); await using var r = await q.ExecuteReaderAsync(ct);
        while (await r.ReadAsync(ct))
        { int id = r.GetInt32(0); var key = (r.GetInt32(3), r.GetInt32(5), id); if (!entries.TryGetValue(key, out var e)) { e = new() { ["id"] = id, ["voucher_line_id"] = r.GetInt32(1), ["document_id"] = r.GetInt32(2), ["bill_group_index"] = key.Item1, ["row_scope"] = r.GetString(4), ["sort_order"] = key.Item2, ["values"] = new List<object>() }; entries.Add(key, e); } if (!r.IsDBNull(6)) { object? value = r.GetString(8) switch { "date" => r.IsDBNull(10) ? null : r.GetFieldValue<DateOnly>(10).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture), "numeric" or "arithmetic_multiply" => r.IsDBNull(11) ? null : Convert.ToDouble(r.GetValue(11), CultureInfo.InvariantCulture), _ => r.IsDBNull(9) ? null : r.GetString(9) }; if (value is not null) ((List<object>)e["values"]!).Add(new { field_id = r.GetInt32(6), field_name = r.GetString(7), value }); } }
        return entries.Values.Select(e => (object)e).ToList();
    }
    private async Task<T> Run<T>(Func<CancellationToken, Task<T>> action, CancellationToken ct) { if (!options.Value.Enabled) return (T)(object)new VerificationRuntimeResult("disabled", Code: "verification_runtime_disabled"); try { return await action(ct); } catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; } catch (PostgresException e) when (e.SqlState == PostgresErrorCodes.SerializationFailure || e.SqlState == PostgresErrorCodes.DeadlockDetected) { return (T)(object)new VerificationRuntimeResult("conflict", Code: "verification_transaction_retry"); } }
    private static VerificationRuntimeResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value)); private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code); private static VerificationRuntimeResult NotFound(string code) => new("not_found", Code: code);
}
