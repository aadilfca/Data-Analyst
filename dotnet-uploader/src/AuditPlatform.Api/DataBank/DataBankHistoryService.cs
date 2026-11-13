using System.Globalization;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Data;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.DataBank;

internal static class DataBankHistoryService
{
    internal static string? Text(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetString(index);
    private static string? At(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetDateTime(index).ToString("O", CultureInfo.InvariantCulture);
    internal static (Dictionary<string, JsonElement> Values, Dictionary<string, string> Tokens) Project(string raw, DataBankTemplate t)
    {
        using var json = JsonDocument.Parse(raw);
        if (json.RootElement.ValueKind != JsonValueKind.Object) throw new DataBankRejected("review", "history_row_review_required");
        var values = new Dictionary<string, JsonElement>(); var tokens = new Dictionary<string, string>();
        var names = t.Columns.ToDictionary(c => c.StorageKey, c => c.Name, StringComparer.Ordinal);
        foreach (var field in json.RootElement.EnumerateObject())
        {
            string name = names.GetValueOrDefault(field.Name, field.Name);
            if (!values.TryAdd(name, field.Value.Clone())) throw new DataBankRejected("review", "history_key_review_required");
            if (field.Value.ValueKind == JsonValueKind.Number) tokens[name] = field.Value.GetRawText();
        }
        return (values, tokens);
    }
    private static object WireChanges(string? raw, DataBankTemplate t)
    {
        if (raw is null or "null") return new { values = (object?)null, numeric_text = new { } };
        var projected = Project(raw, t);
        var tokens = new Dictionary<string, Dictionary<string, string>>();
        foreach (var field in projected.Values)
        {
            if (field.Value.ValueKind != JsonValueKind.Object) throw new DataBankRejected("review", "history_fields_review_required");
            var numbers = new Dictionary<string, string>();
            foreach (var value in field.Value.EnumerateObject()) if (value.Value.ValueKind == JsonValueKind.Number) numbers[value.Name] = value.Value.GetRawText();
            tokens[field.Key] = numbers;
        }
        return new { values = (object)projected.Values, numeric_text = tokens };
    }
    private static string Difference(string oldRaw, string newRaw)
    {
        using var old = JsonDocument.Parse(oldRaw.Trim() == "null" ? "{}" : oldRaw); using var next = JsonDocument.Parse(newRaw.Trim() == "null" ? "{}" : newRaw);
        if (old.RootElement.ValueKind != JsonValueKind.Object || next.RootElement.ValueKind != JsonValueKind.Object) throw new DataBankRejected("review", "revision_fields_review_required");
        var fields = old.RootElement.EnumerateObject().Select(p => p.Name).Union(next.RootElement.EnumerateObject().Select(p => p.Name), StringComparer.Ordinal);
        var differences = new Dictionary<string, object>();
        foreach (string key in fields)
        {
            JsonElement? left = old.RootElement.TryGetProperty(key, out var a) ? a : null;
            JsonElement? right = next.RootElement.TryGetProperty(key, out var b) ? b : null;
            if (left is {} x && right is {} y && JsonElement.DeepEquals(x, y) || left is null && right is null) continue;
            differences[key] = new { old_value = left?.Clone(), new_value = right?.Clone() };
        }
        return JsonSerializer.Serialize(differences);
    }
    internal static async Task<object> History(PostgresTransactionContext db, DataBankQuery q, DataBankTemplate t, int bound, CancellationToken ct)
    {
        if (q.Account is not null && t.Purpose != "account_ledger") throw new DataBankRejected("invalid", "select_ledger_template");
        if (q.Account is not null)
        {
            await using var account = db.CreateCommand("""
                SELECT 1 FROM public.uploaded_files WHERE client_id=$1 AND template_id=$2 AND ledger_account_code=$3
                UNION ALL SELECT 1 FROM public.client_data_history h LEFT JOIN public.uploaded_files u ON u.id=h.source_upload_id AND u.client_id=h.client_id AND u.template_id=h.template_id
                WHERE h.client_id=$1 AND h.template_id=$2 AND COALESCE(h.ledger_account_code,h.source_metadata->>'ledger_account_code',u.ledger_account_code)=$3 LIMIT 1
                """);
            account.Parameters.AddWithValue(q.ClientId); account.Parameters.AddWithValue(t.Id); account.Parameters.AddWithValue(q.Account.Trim());
            if (await account.ExecuteScalarAsync(ct) is null) throw new DataBankRejected("invalid", "history_account_not_found");
        }
        // Presentation inference is a SELECT-only unique match on BOTH sides.
        // The saved action/fields remain untouched and are exported unchanged.
        const string cte = """
            WITH classified AS (
              SELECT h.*,COALESCE(h.ledger_account_code,h.source_metadata->>'ledger_account_code',u.ledger_account_code) AS account_code,
                COALESCE(NULLIF(h.ledger_account_name,''),NULLIF(h.source_metadata->>'ledger_account_name',''),NULLIF(u.ledger_account_name,'')) AS account_name,
                CASE WHEN replacement.id IS NOT NULL THEN 'changed' ELSE h.action END AS effective_action,
                replacement.id AS replacement_id,r.row_data AS replacement_data
              FROM public.client_data_history h
              LEFT JOIN public.uploaded_files u ON u.id=h.source_upload_id AND u.client_id=h.client_id AND u.template_id=h.template_id
              LEFT JOIN LATERAL (
                SELECT min(c.id) AS id FROM public.client_data c
                WHERE h.action='deleted' AND h.replacing_upload_id IS NOT NULL AND cardinality($3::text[])>0
                  AND c.client_id=h.client_id AND c.template_id=h.template_id AND c.upload_id=h.replacing_upload_id AND c.is_deleted IS FALSE
                  AND c.row_data<>h.row_data AND c.row_data-$3::text[]=h.row_data-$3::text[]
                HAVING count(*)=1 AND (SELECT count(*) FROM public.client_data_history h2
                  WHERE h2.client_id=h.client_id AND h2.template_id=h.template_id AND h2.action='deleted'
                    AND h2.replacing_upload_id=h.replacing_upload_id AND h2.row_data-$3::text[]=h.row_data-$3::text[])=1
              ) replacement ON TRUE
              LEFT JOIN public.client_data r ON r.id=replacement.id AND r.client_id=h.client_id AND r.template_id=h.template_id
              WHERE h.client_id=$1 AND h.template_id=$2
            )
            """;
        const string where = "WHERE ($4::text IS NULL OR account_code=$4) AND ($5='all' OR effective_action=$5)";
        await using var command = db.CreateCommand(cte + " SELECT count(*) FROM classified " + where);
        command.Parameters.AddWithValue(q.ClientId); command.Parameters.AddWithValue(t.Id);
        command.Parameters.AddWithValue(t.Purpose == "account_ledger" ? Array.Empty<string>() : t.Columns.Where(c => c.Type == "Numeric").Select(c => c.StorageKey).ToArray());
        command.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)q.Account?.Trim() ?? DBNull.Value); command.Parameters.AddWithValue(q.Action);
        long total = (long)(await command.ExecuteScalarAsync(ct))!;
        command.CommandText = cte + $"""
            SELECT id,client_data_id,action,changed_at,changed_by,source_upload_id,replacing_upload_id,source_identity,
              CASE WHEN octet_length(row_data::text)<={bound} THEN row_data::text END,
              CASE WHEN COALESCE(octet_length(changed_fields::text),0)<={bound} THEN changed_fields::text END,
              account_code,account_name,effective_action,replacement_id,
              CASE WHEN COALESCE(octet_length(replacement_data::text),0)<={bound} THEN replacement_data::text END,
              changed_fields IS NOT NULL,replacement_data IS NOT NULL
            FROM classified {where} ORDER BY changed_at DESC,id DESC LIMIT $6 OFFSET $7
            """;
        command.Parameters.AddWithValue(q.Limit); command.Parameters.AddWithValue((q.Page - 1) * q.Limit);
        var rows = new List<object>(); long bytes = 0; var savedFields = new SortedSet<string>(StringComparer.Ordinal);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            string raw = Text(reader, 8) ?? throw new DataBankRejected("too_large", "history_row_too_large");
            string? changes = Text(reader, 9), replacement = Text(reader, 14);
            if (reader.GetBoolean(15) && changes is null || reader.GetBoolean(16) && replacement is null) throw new DataBankRejected("too_large", "history_row_too_large");
            bytes += Encoding.UTF8.GetByteCount(raw) + Encoding.UTF8.GetByteCount(changes ?? "") + Encoding.UTF8.GetByteCount(replacement ?? "");
            if (bytes > bound) throw new DataBankRejected("too_large", "history_response_too_large");
            if (replacement is not null) changes = Difference(raw, replacement);
            var data = Project(raw, t);
            var changeWire = JsonSerializer.SerializeToElement(WireChanges(changes, t));
            foreach (string name in data.Values.Keys) if (t.Columns.All(c => c.Name != name)) savedFields.Add(name);
            if (changeWire.GetProperty("values").ValueKind == JsonValueKind.Object)
                foreach (var field in changeWire.GetProperty("values").EnumerateObject()) if (t.Columns.All(c => c.Name != field.Name)) savedFields.Add(field.Name);
            rows.Add(new { id = reader.GetInt32(0), client_data_id = reader.GetInt32(1), action = reader.GetString(2), changed_at = At(reader, 3),
                changed_by = Text(reader, 4), source_upload_id = reader.IsDBNull(5) ? (int?)null : reader.GetInt32(5),
                replacing_upload_id = reader.IsDBNull(6) ? (int?)null : reader.GetInt32(6), source_identity = Text(reader, 7),
                row_data = data.Values, numeric_text = data.Tokens, changed_fields = changeWire.GetProperty("values"),
                changed_numeric_text = changeWire.GetProperty("numeric_text"), ledger_account_code = Text(reader, 10), ledger_account_name = Text(reader, 11),
                display_action = reader.GetString(12), replacement_client_data_id = reader.IsDBNull(13) ? (int?)null : reader.GetInt32(13) });
        }
        return new { client_id = q.ClientId, template_id = t.Id, template_name = t.Name, page = q.Page, limit = q.Limit, total,
            total_pages = (total + q.Limit - 1) / q.Limit, data_purpose = t.Purpose, document_key_column = t.Document?.Name,
            columns = t.Columns.Select(c => new { column_name = c.Name, storage_key = c.StorageKey, data_type = c.Type })
                .Concat(savedFields.Select(key => new { column_name = key, storage_key = key, data_type = "Text" })).ToArray(), rows };
    }
    internal static async Task<object> Accounts(PostgresTransactionContext db, DataBankQuery q, DataBankTemplate t, int bound, CancellationToken ct)
    {
        if (t.Purpose != "account_ledger") throw new DataBankRejected("invalid", "select_ledger_template");
        await using var command = db.CreateCommand("""
            WITH choices AS (
              SELECT ledger_account_code AS code,COALESCE(NULLIF(ledger_account_name,''),ledger_account_code) AS name,0 AS priority,id
              FROM public.uploaded_files WHERE client_id=$1 AND template_id=$2 AND status='Imported'
              UNION ALL
              SELECT COALESCE(h.ledger_account_code,h.source_metadata->>'ledger_account_code',u.ledger_account_code),
                COALESCE(NULLIF(h.ledger_account_name,''),NULLIF(h.source_metadata->>'ledger_account_name',''),NULLIF(u.ledger_account_name,''),h.ledger_account_code),1,h.id
              FROM public.client_data_history h LEFT JOIN public.uploaded_files u ON u.id=h.source_upload_id AND u.client_id=h.client_id AND u.template_id=h.template_id
              WHERE h.client_id=$1 AND h.template_id=$2
            ), latest AS (SELECT DISTINCT ON(code) code,COALESCE(name,code) AS name FROM choices
              WHERE code IS NOT NULL AND code<>'' ORDER BY code,priority,id DESC)
            SELECT code,name FROM latest ORDER BY lower(name),code LIMIT $3
            """);
        command.Parameters.AddWithValue(q.ClientId); command.Parameters.AddWithValue(t.Id); command.Parameters.AddWithValue(bound + 1);
        var rows = new List<object>(); await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) rows.Add(new { account_code = reader.GetString(0), account_name = reader.GetString(1) });
        if (rows.Count > bound) throw new DataBankRejected("too_large", "history_account_capacity");
        return new { accounts = rows };
    }
    internal static async Task Flags(PostgresTransactionContext db, int client, int template, List<Dictionary<string, object?>> rows, CancellationToken ct)
    {
        if (rows.Count == 0) return;
        await using var command = db.CreateCommand("""
            SELECT c.id,(SELECT count(*) FROM public.client_data_history h WHERE h.client_id=c.client_id AND h.template_id=c.template_id AND h.client_data_id=c.id),
              (SELECT count(*) FROM public.data_audit_log a WHERE a.client_data_id=c.id)
            FROM public.client_data c WHERE c.client_id=$1 AND c.template_id=$2 AND c.id=ANY($3)
            """);
        command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(template); command.Parameters.AddWithValue(rows.Select(r => checked((int)(long)r["id"]!)).ToArray());
        var counts = new Dictionary<int, (long Upload, long Legacy)>();
        await using (var reader = await command.ExecuteReaderAsync(ct)) while (await reader.ReadAsync(ct)) counts[reader.GetInt32(0)] = (reader.GetInt64(1), reader.GetInt64(2));
        foreach (var row in rows)
        {
            var n = counts.GetValueOrDefault(checked((int)(long)row["id"]!));
            row["has_history"] = n.Upload + n.Legacy > 0; row["revision_count"] = n.Legacy; row["upload_revision_count"] = n.Upload;
        }
    }
    internal static async Task<object> Revisions(PostgresTransactionContext db, DataBankQuery q, DataBankTemplate t, int bound, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT id FROM public.client_data WHERE id=$1 AND client_id=$2 AND template_id=$3");
        command.Parameters.AddWithValue(q.RowId!.Value); command.Parameters.AddWithValue(q.ClientId); command.Parameters.AddWithValue(t.Id);
        if (await command.ExecuteScalarAsync(ct) is null) throw new DataBankRejected("not_found", "row_not_found");
        bool uploads = q.RevisionSource == "uploads";
        string from = uploads ? "public.client_data_history a WHERE a.client_data_id=$1 AND a.client_id=$2 AND a.template_id=$3"
            : "public.data_audit_log a JOIN public.client_data c ON c.id=a.client_data_id WHERE c.id=$1 AND c.client_id=$2 AND c.template_id=$3";
        command.CommandText = "SELECT count(*) FROM " + from;
        long total = (long)(await command.ExecuteScalarAsync(ct))!;
        string projection = uploads ? "a.action,a.changed_by,a.changed_at,a.changed_fields::text,NULL::text"
            : "a.action,a.\"user\",a.changed_at,a.old_row_data::text,a.new_row_data::text";
        // Measure JSON in SQL before transferring a potentially huge old record.
        command.CommandText = $"SELECT action,actor,stamp,CASE WHEN COALESCE(octet_length(old),0)<={bound} THEN old END,CASE WHEN COALESCE(octet_length(next),0)<={bound} THEN next END,old IS NOT NULL,next IS NOT NULL FROM (SELECT {projection} FROM {from} ORDER BY a.changed_at,a.id LIMIT $4 OFFSET $5) r(action,actor,stamp,old,next)";
        command.Parameters.AddWithValue(q.Limit); command.Parameters.AddWithValue((q.Page - 1) * q.Limit);
        var revisions = new List<object>(); long bytes = 0;
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            string? old = Text(reader, 3), next = Text(reader, 4);
            if (reader.GetBoolean(5) && old is null || reader.GetBoolean(6) && next is null) throw new DataBankRejected("too_large", "revision_too_large");
            bytes += Encoding.UTF8.GetByteCount(old ?? "") + Encoding.UTF8.GetByteCount(next ?? "");
            if (bytes > bound) throw new DataBankRejected("too_large", "revision_response_too_large");
            string? raw = uploads ? old : Difference(old ?? "{}", next ?? "{}");
            var wire = JsonSerializer.SerializeToElement(WireChanges(raw, t));
            revisions.Add(new { action = reader.GetString(0), user = Text(reader, 1), changed_at = At(reader, 2),
                changed_fields = wire.GetProperty("values"), numeric_text = wire.GetProperty("numeric_text"), details_available = raw is not null });
        }
        return new { client_id = q.ClientId, template_id = t.Id, client_data_id = q.RowId, source = q.RevisionSource, total,
            page = q.Page, limit = q.Limit, total_pages = (total + q.Limit - 1) / q.Limit, revisions };
    }
}
