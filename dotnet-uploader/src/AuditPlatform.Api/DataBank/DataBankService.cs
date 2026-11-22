using System.Globalization;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.DataBank;

// SELECT-only operations, fully materialized before releasing their snapshot.
// There is no source-data cache, DDL, import, schema adoption or write fallback.
public sealed class DataBankService(PostgresReadSnapshot snapshots, IOptions<DataBankOptions> options, TimeProvider? clock = null)
{
    private readonly SemaphoreSlim requests = new(options.Value.MaximumConcurrentRequests);
    private const string Active = "cd.is_deleted IS FALSE AND cd.change_detection_status <> 'deleted'";
    private const string Spaces = " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000";
    private const string NumberPattern = "^[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?$";
    private static void Reject(string status, string code) => throw new DataBankRejected(status, code);

    public async Task<DataBankResult> ReadAsync(VerifiedSession? session, DataBankQuery query, CancellationToken ct = default)
    {
        if (!options.Value.Enabled) return new("unavailable", Code: "databank_disabled");
        if (query.Operation is "history" or "revisions" or "history-accounts" && !options.Value.HistoryEnabled)
            return new("unavailable", Code: "databank_history_disabled");
        if (session is null) return new("unauthenticated", Code: "not_authenticated");
        if (!Valid(query)) return new("invalid", Code: "invalid_databank_request");
        if (!await requests.WaitAsync(0, ct)) return new("busy", Code: "databank_busy");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var value = await snapshots.ExecuteAsync(async (db, token) =>
            {
                await Authorize(db, session, query.ClientId, token);
                var templates = await Templates(db, session.TenantId!.Value, query.TemplateId, token);
                if (query.TemplateId is {} id && templates.All(t => t.Id != id)) Reject("not_found", "template_not_found");
                return query.Operation switch
                {
                    "availability" => await Availability(db, query, templates, token),
                    "metrics" => await Metrics(db, query, templates, token),
                    "calendar" => await Calendar(db, session.TenantId.Value, query, templates, token),
                    "overdue" => await Overdue(db, session.TenantId.Value, query, templates, token),
                    "accounts" => await Accounts(db, query, templates.Single(t => t.Id == query.TemplateId), token),
                    "preview" => await Preview(db, query, templates.Single(t => t.Id == query.TemplateId), token),
                    "history" => await DataBankHistoryService.History(db, query, templates.Single(t => t.Id == query.TemplateId), options.Value.MaximumResponseBytes, token),
                    "revisions" => await DataBankHistoryService.Revisions(db, query, templates.Single(t => t.Id == query.TemplateId), options.Value.MaximumResponseBytes, token),
                    "history-accounts" => await DataBankHistoryService.Accounts(db, query, templates.Single(t => t.Id == query.TemplateId), options.Value.MaximumLedgerAccounts, token),
                    _ => throw new DataBankRejected("unavailable", "databank_operation_unavailable")
                };
            }, deadline.Token);
            // Revocation/context changes during a slow read must discard its result.
            await snapshots.ExecuteAsync(async (db, token) => { await Authorize(db, session, query.ClientId, token); return true; }, deadline.Token);
            if (JsonSerializer.SerializeToUtf8Bytes(value).Length > options.Value.MaximumResponseBytes)
                return new("too_large", Code: "databank_response_too_large_narrow_selection");
            return new("success", value);
        }
        catch (DataBankRejected error) { return new(error.Status, Code: error.Code); }
        catch (PostgresException error) when (error.SqlState is "42P01" or "42703") { return new("review", Code: "databank_schema_review_required"); }
        catch (PostgresException error) when (error.SqlState is "57014" or "55P03") { return new("timeout", Code: "databank_database_timeout"); }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested && deadline.IsCancellationRequested) { return new("timeout", Code: "databank_timeout"); }
        finally { requests.Release(); }
    }

    internal static bool Valid(DataBankQuery q)
    {
        if (q.ClientId <= 0 || q.TemplateId is <= 0 || q.Page < 1 || q.Limit is < 1 or > 200
            || (long)(q.Page - 1) * q.Limit > int.MaxValue || q.Search?.Length > 512 || q.Account?.Length > 120
            || q.Account is not null && string.IsNullOrWhiteSpace(q.Account)
            || q.Quality is not (null or "" or "complete" or "incomplete")
            || q.Minimum is {} min && !double.IsFinite(min) || q.Maximum is {} max && !double.IsFinite(max)
            || q.Minimum > q.Maximum) return false;
        if (q.Operation is "preview" or "accounts" or "history" or "revisions" or "history-accounts" && q.TemplateId is null) return false;
        if (q.Operation is "history" or "revisions" && (q.Limit > 100 || q.Action is not ("all" or "changed" or "deleted"))) return false;
        if (q.Operation == "revisions" && (q.RowId is not > 0 || q.RevisionSource is not ("uploads" or "legacy"))) return false;
        if (q.Operation is "availability" or "preview")
            return DateOnly.TryParseExact(q.Start, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var start)
                && DateOnly.TryParseExact(q.End, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var end) && start <= end;
        if (q.Operation == "calendar") return q.Year is >= 1 and <= 9999 && q.Month is >= 1 and <= 12
            && q.TemplateIds is { Length: > 0 and <= 100 } && q.TemplateIds.All(id => id > 0);
        return true;
    }

    internal static async Task Authorize(PostgresTransactionContext db, VerifiedSession session, int client, CancellationToken ct, bool admin = false)
    {
        if (!session.TenantContextRequired || session.TenantId is not > 0 || !AuthenticationService.ValidToken(session.Token))
            Reject("forbidden", "databank_access_denied");
        await using var command = db.CreateCommand("""
            SELECT c.id FROM public.sessions s JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id
            JOIN public.platform_tenants t ON t.id=m.tenant_id
            JOIN public.platform_tenant_client_homes h ON h.tenant_id=t.id
            JOIN public.clients c ON c.id=h.client_id
            WHERE s.token=$1 AND s.user_id=$2 AND t.id=$3 AND c.id=$4
              AND s.selected_tenant_id=t.id AND s.context_version=$5 AND s.selected_client_id=c.id
              AND s.expiry>now() AND u.is_active IS TRUE AND m.is_active IS TRUE AND t.is_active IS TRUE AND c.is_active IS TRUE
              AND (NOT $6 OR m.tenant_role='admin')
              AND (m.tenant_role='admin' OR (t.tenant_type='audit_firm' AND EXISTS (
                SELECT 1 FROM public.platform_audit_engagements e JOIN public.platform_engagement_assignments a
                ON a.engagement_id=e.id AND a.firm_tenant_id=e.firm_tenant_id
                WHERE e.client_id=c.id AND e.firm_tenant_id=t.id AND e.is_active IS TRUE
                  AND a.user_id=u.id AND a.is_active IS TRUE AND a.team_role IN ('manager','internee'))))
            """);
        command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
        command.Parameters.AddWithValue(session.TenantId ?? 0); command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(session.ContextVersion);
        command.Parameters.AddWithValue(admin);
        if (await command.ExecuteScalarAsync(ct) is null) Reject("forbidden", "databank_access_denied");
    }

    internal async Task<DataBankTemplate> TemplateWithin(PostgresTransactionContext db, int tenant, int id, CancellationToken ct) =>
        (await Templates(db, tenant, id, ct)).SingleOrDefault() ?? throw new DataBankRejected("not_found", "template_not_found");

    private async Task<List<DataBankTemplate>> Templates(PostgresTransactionContext db, int tenant, int? templateId, CancellationToken ct)
    {
        var templates = new List<DataBankTemplate>();
        await using (var command = db.CreateCommand("""
            SELECT t.id,d.display_name,t.data_purpose,COALESCE(t.has_subtotals_by_default,FALSE)
            FROM public.templates t JOIN public.platform_tenant_templates d ON d.template_id=t.id
            WHERE d.tenant_id=$1 AND d.binding_origin='native' AND t.is_active IS TRUE AND ($3::integer IS NULL OR t.id=$3)
            ORDER BY d.display_name,t.id LIMIT $2
            """))
        {
            command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(options.Value.MaximumTemplates + 1);
            command.Parameters.AddWithValue(NpgsqlDbType.Integer, (object?)templateId ?? DBNull.Value);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct)) templates.Add(new(reader.GetInt32(0), reader.GetString(1), reader.GetString(2), reader.GetBoolean(3), []));
        }
        if (templates.Count > options.Value.MaximumTemplates) Reject("too_large", "databank_configuration_capacity");
        var columns = new Dictionary<int, List<TemplateColumn>>();
        long definitionBytes = 0;
        await using (var command = db.CreateCommand("""
            SELECT p.template_id,m.id,c.display_name,p.storage_key,m.data_type,p.reconciliation_role,p.sort_order
            FROM public.template_column_mappings p
            JOIN public.platform_tenant_templates d ON d.tenant_id=p.tenant_id AND d.template_id=p.template_id
            JOIN public.platform_tenant_columns c ON c.tenant_id=p.tenant_id AND c.column_id=p.column_id
            JOIN public.master_columns m ON m.id=p.column_id AND m.column_name=p.storage_key
            WHERE p.tenant_id=$1 AND d.binding_origin='native' AND c.binding_origin='native' AND m.is_active IS TRUE
              AND p.template_id=ANY($2) ORDER BY p.template_id,p.sort_order,m.id LIMIT $3
            """))
        {
            command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(templates.Select(t => t.Id).ToArray());
            command.Parameters.AddWithValue(checked(options.Value.MaximumTemplates * options.Value.MaximumColumns + 1));
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                int id = reader.GetInt32(0); if (!columns.TryGetValue(id, out var list)) columns[id] = list = [];
                definitionBytes += 128 + Encoding.UTF8.GetByteCount(reader.GetString(2)) + Encoding.UTF8.GetByteCount(reader.GetString(3));
                if (definitionBytes > options.Value.MaximumResponseBytes) Reject("too_large", "databank_configuration_capacity");
                if (list.Count >= options.Value.MaximumColumns) Reject("too_large", "databank_configuration_capacity");
                list.Add(new(reader.GetInt32(1), reader.GetString(2), reader.GetString(3), reader.GetString(4), "", reader.IsDBNull(5) ? null : reader.GetString(5), null, false, reader.GetInt32(6), 0, true));
            }
        }
        var result = templates.Select(t => t with { Columns = columns.GetValueOrDefault(t.Id) ?? [] }).ToList();
        if (result.Any(t => t.Columns.Count(c => c.Role == "document_id") > 1
            || t.Columns.Select(c => c.Name).Distinct(StringComparer.Ordinal).Count() != t.Columns.Count
            || t.Columns.Any(c => c.Role == "document_id" && c.Type != "Text" || c.Role == "footing" && c.Type != "Numeric")))
            Reject("review", "databank_template_review_required");
        return result;
    }

    private sealed record Inventory(long Rows, long Filtered, long Sundays, string? First, string? Last, long Bytes);
    private static async Task<Dictionary<int, Inventory>> InventoryRows(PostgresTransactionContext db, DataBankQuery q, CancellationToken ct)
    {
        string size = q.Operation == "metrics" || q.Metrics ? "COALESCE(sum(length(cd.row_data::text)),0)+count(*)*256" : "0::bigint";
        await using var command = db.CreateCommand($"""
            SELECT cd.template_id,count(*),count(*) FILTER(WHERE cd.extract_date BETWEEN $2::date AND $3::date),
                count(*) FILTER(WHERE cd.extract_date BETWEEN $2::date AND $3::date AND EXTRACT(DOW FROM cd.extract_date)=0),
                min(cd.extract_date)::text,max(cd.extract_date)::text,{size}
            FROM public.client_data cd WHERE cd.client_id=$1 AND {Active} GROUP BY cd.template_id
            """);
        command.Parameters.AddWithValue(q.ClientId); command.Parameters.AddWithValue(q.Start ?? "0001-01-01"); command.Parameters.AddWithValue(q.End ?? "9999-12-31");
        var result = new Dictionary<int, Inventory>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) result[reader.GetInt32(0)] = new(reader.GetInt64(1), reader.GetInt64(2), reader.GetInt64(3), Text(reader, 4), Text(reader, 5), reader.GetInt64(6));
        return result;
    }
    private static string? Text(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetString(index);
    private static string Size(long bytes) => bytes < 1024 ? $"{bytes} B" : bytes < 1048576 ? (bytes / 1024d).ToString("F1", CultureInfo.InvariantCulture) + " KB"
        : bytes < 1073741824 ? (bytes / 1048576d).ToString("F1", CultureInfo.InvariantCulture) + " MB" : (bytes / 1073741824d).ToString("F1", CultureInfo.InvariantCulture) + " GB";
    private static async Task<Dictionary<int, string>> Frequencies(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        // Upload frequency is optional client metadata. Older/development
        // schemas may not have the table; Availability already supplies the
        // established Daily default when this dictionary has no entry.
        await using (var exists = db.CreateCommand("SELECT to_regclass('public.client_template_upload_frequency') IS NOT NULL"))
            if (await exists.ExecuteScalarAsync(ct) is not true) return [];
        await using var command = db.CreateCommand("SELECT template_id,frequency FROM public.client_template_upload_frequency WHERE client_id=$1");
        command.Parameters.AddWithValue(client); var result = new Dictionary<int, string>();
        await using var reader = await command.ExecuteReaderAsync(ct); while (await reader.ReadAsync(ct)) result[reader.GetInt32(0)] = reader.GetString(1); return result;
    }
    private static async Task<long> Documents(PostgresTransactionContext db, int client, DataBankTemplate t, long rows, CancellationToken ct)
    {
        if (t.Document is null) return rows;
        await using var command = db.CreateCommand($"SELECT count(DISTINCT NULLIF(BTRIM(cd.row_data->>$3),'')) FROM public.client_data cd WHERE cd.client_id=$1 AND cd.template_id=$2 AND {Active}");
        command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(t.Id); command.Parameters.AddWithValue(t.Document.StorageKey);
        return (long)(await command.ExecuteScalarAsync(ct))!;
    }
    private static async Task<object> Availability(PostgresTransactionContext db, DataBankQuery q, List<DataBankTemplate> templates, CancellationToken ct)
    {
        var inventory = await InventoryRows(db, q, ct); var frequency = await Frequencies(db, q.ClientId, ct); var result = new List<object>();
        foreach (var t in templates.Where(t => q.TemplateId is null || t.Id == q.TemplateId))
        {
            var row = inventory.GetValueOrDefault(t.Id) ?? new Inventory(0, 0, 0, null, null, 0);
            long? documents = q.Metrics ? await Documents(db, q.ClientId, t, row.Rows, ct) : null;
            result.Add(new { template_id = t.Id, template_name = t.Name, filtered_row_count = row.Filtered, total_row_count = row.Rows,
                document_count = documents, date_min = row.First, date_max = row.Last, filtered_sunday_count = row.Sundays,
                status = row.Rows > 0 ? "complete" : "missing", filtered_status = row.Filtered > 0 ? "complete" : "missing",
                estimated_size_bytes = q.Metrics ? (long?)row.Bytes : null, estimated_size_formatted = q.Metrics ? Size(row.Bytes) : null,
                upload_frequency = frequency.GetValueOrDefault(t.Id, "Daily") });
        }
        return result;
    }
    private static async Task<object> Metrics(PostgresTransactionContext db, DataBankQuery q, List<DataBankTemplate> templates, CancellationToken ct)
    {
        var inventory = await InventoryRows(db, q, ct); var result = new List<object>(); long bytes = inventory.Values.Sum(row => row.Bytes);
        foreach (var t in templates)
        {
            var row = inventory.GetValueOrDefault(t.Id) ?? new Inventory(0, 0, 0, null, null, 0);
            result.Add(new { template_id = t.Id, document_count = await Documents(db, q.ClientId, t, row.Rows, ct), estimated_size_bytes = row.Bytes, estimated_size_formatted = Size(row.Bytes) });
        }
        return new { client_id = q.ClientId, templates = result, client_size_bytes = bytes, client_size_formatted = Size(bytes) };
    }

    private static async Task<List<(DateOnly Date, string Label, bool Yearly)>> Holidays(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT holiday_date,label,repeats_yearly FROM public.platform_tenant_holidays WHERE tenant_id=$1 ORDER BY holiday_date,id LIMIT 10001");
        command.Parameters.AddWithValue(tenant); var rows = new List<(DateOnly, string, bool)>();
        await using var reader = await command.ExecuteReaderAsync(ct); while (await reader.ReadAsync(ct)) rows.Add((reader.GetFieldValue<DateOnly>(0), reader.GetString(1), reader.GetBoolean(2)));
        if (rows.Count > 10000) Reject("too_large", "databank_holiday_capacity"); return rows;
    }
    private static async Task<object> Calendar(PostgresTransactionContext db, int tenant, DataBankQuery q, List<DataBankTemplate> templates, CancellationToken ct)
    {
        var ids = q.TemplateIds!.Distinct().ToArray(); if (ids.Any(id => templates.All(t => t.Id != id))) Reject("not_found", "template_not_found");
        var start = new DateOnly(q.Year!.Value, q.Month!.Value, 1); var end = new DateOnly(start.Year, start.Month, DateTime.DaysInMonth(start.Year, start.Month));
        var present = new HashSet<(int, DateOnly)>();
        await using (var command = db.CreateCommand($"SELECT DISTINCT cd.template_id,cd.extract_date FROM public.client_data cd WHERE cd.client_id=$1 AND cd.template_id=ANY($2) AND {Active} AND cd.extract_date BETWEEN $3 AND $4"))
        {
            command.Parameters.AddWithValue(q.ClientId); command.Parameters.AddWithValue(ids); command.Parameters.AddWithValue(start); command.Parameters.AddWithValue(end);
            await using var reader = await command.ExecuteReaderAsync(ct); while (await reader.ReadAsync(ct)) present.Add((reader.GetInt32(0), reader.GetFieldValue<DateOnly>(1)));
        }
        var holidays = await Holidays(db, tenant, ct); var results = new List<object>();
        foreach (int id in ids)
        {
            var days = new List<object>(); int count = 0, missing = 0, sundays = 0, holidayCount = 0;
            for (int day = 1; day <= end.Day; day++)
            {
                var date = new DateOnly(start.Year, start.Month, day); string status; string? label = null;
                if (present.Contains((id, date))) { status = "present"; count++; }
                else if (date.DayOfWeek == DayOfWeek.Sunday) { status = "sunday"; sundays++; }
                else if (holidays.LastOrDefault(h => h.Date == date || h.Yearly && h.Date.Month == date.Month && h.Date.Day == date.Day) is var h && h.Label is not null)
                { status = "holiday"; label = h.Label; holidayCount++; }
                else { status = "missing"; missing++; }
                days.Add(new { date = date.ToString("yyyy-MM-dd"), status, label });
            }
            results.Add(new { template_id = id, template_name = templates.Single(t => t.Id == id).Name, present_count = count, missing_count = missing,
                sunday_count = sundays, holiday_count = holidayCount, days });
        }
        return new { year = start.Year, month = start.Month, templates = results };
    }
    private async Task<object> Overdue(PostgresTransactionContext db, int tenant, DataBankQuery q, List<DataBankTemplate> templates, CancellationToken ct)
    {
        var inventory = await InventoryRows(db, q, ct); var frequency = await Frequencies(db, q.ClientId, ct); var holidays = await Holidays(db, tenant, ct);
        string clientName;
        await using (var command = db.CreateCommand("SELECT client_name FROM public.clients WHERE id=$1"))
        { command.Parameters.AddWithValue(q.ClientId); clientName = (string)(await command.ExecuteScalarAsync(ct))!; }
        var today = DateOnly.FromDateTime((clock ?? TimeProvider.System).GetLocalNow().DateTime); var rows = new List<(int Days, object Value)>();
        foreach (var t in templates)
        {
            if (inventory.GetValueOrDefault(t.Id)?.Last is not {} last) continue;
            string freq = frequency.GetValueOrDefault(t.Id, "Daily"); int allowed = freq switch { "Daily" => 1, "Weekly" => 6, "Monthly" => 22, _ => -1 };
            if (allowed < 0) Reject("review", "databank_frequency_review_required");
            int elapsed = 0; var start = DateOnly.ParseExact(last, "yyyy-MM-dd", CultureInfo.InvariantCulture);
            for (int day = start.DayNumber + 1; day <= today.DayNumber; day++)
            {
                ct.ThrowIfCancellationRequested(); var date = DateOnly.FromDayNumber(day);
                if (date.DayOfWeek != DayOfWeek.Sunday && !holidays.Any(h => h.Date == date || h.Yearly && h.Date.Month == date.Month && h.Date.Day == date.Day)) elapsed++;
            }
            if (elapsed > allowed) rows.Add((elapsed - allowed, new { client_id = q.ClientId, client_name = clientName, template_id = t.Id, template_name = t.Name, last_upload_date = last, frequency = freq, days_overdue = elapsed - allowed }));
        }
        return new { client_id = q.ClientId, overdue = rows.OrderByDescending(r => r.Days).Select(r => r.Value).ToArray() };
    }
    private async Task<object> Accounts(PostgresTransactionContext db, DataBankQuery q, DataBankTemplate t, CancellationToken ct)
    {
        if (t.Purpose != "account_ledger") Reject("invalid", "select_ledger_template");
        await using var command = db.CreateCommand("""
            SELECT account_code,account_name FROM (
                SELECT ledger_account_code AS account_code,COALESCE(NULLIF(ledger_account_name,''),ledger_account_code) AS account_name,
                    row_number() OVER(PARTITION BY ledger_account_code ORDER BY id DESC) AS latest
                FROM public.uploaded_files WHERE client_id=$1 AND template_id=$2 AND status='Imported'
                    AND ledger_account_code IS NOT NULL AND ledger_account_code<>'') accounts
            WHERE latest=1 ORDER BY lower(account_name),account_code LIMIT $3
            """);
        command.Parameters.AddWithValue(q.ClientId); command.Parameters.AddWithValue(t.Id); command.Parameters.AddWithValue(options.Value.MaximumLedgerAccounts + 1);
        var rows = new List<object>(); await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) rows.Add(new { account_code = reader.GetString(0), account_name = reader.GetString(1) });
        if (rows.Count > options.Value.MaximumLedgerAccounts) Reject("too_large", "databank_account_capacity");
        return new { accounts = rows };
    }

    internal static async Task<string> PreviewPredicate(PostgresTransactionContext db, DataBankQuery q, DataBankTemplate t, NpgsqlCommand command, CancellationToken ct, bool sunday = false)
    {
        if (q.Account is not null)
        {
            if (t.Purpose != "account_ledger") Reject("invalid", "select_ledger_template");
            await using var account = db.CreateCommand("SELECT id FROM public.uploaded_files WHERE client_id=$1 AND template_id=$2 AND status='Imported' AND ledger_account_code=$3 LIMIT 1");
            account.Parameters.AddWithValue(q.ClientId); account.Parameters.AddWithValue(t.Id); account.Parameters.AddWithValue(q.Account!.Trim());
            if (await account.ExecuteScalarAsync(ct) is null) Reject("invalid", "uploaded_account_not_found");
        }
        // An account-ledger view must never accidentally broaden into all accounts.
        if (t.Purpose == "account_ledger" && q.Account is null) Reject("invalid", "select_uploaded_ledger_account");
        string Param(object? value, NpgsqlDbType type)
        { command.Parameters.AddWithValue(type, value ?? DBNull.Value); return "$" + command.Parameters.Count.ToString(CultureInfo.InvariantCulture); }
        string client = Param(q.ClientId, NpgsqlDbType.Integer), template = Param(t.Id, NpgsqlDbType.Integer);
        string start = Param(q.Start, NpgsqlDbType.Text), end = Param(q.End, NpgsqlDbType.Text);
        string where = $"cd.client_id={client} AND cd.template_id={template} AND {Active} AND cd.extract_date BETWEEN {start}::date AND {end}::date";
        if (q.Account is not null) where += $" AND EXISTS(SELECT 1 FROM public.uploaded_files u WHERE u.id=cd.upload_id AND u.client_id=cd.client_id AND u.template_id=cd.template_id AND u.status='Imported' AND u.ledger_account_code={Param(q.Account.Trim(), NpgsqlDbType.Text)})";
        if (!string.IsNullOrEmpty(q.Search)) where += $" AND EXISTS(SELECT 1 FROM jsonb_each_text(cd.row_data) pair WHERE lower(pair.value) LIKE lower({Param("%" + q.Search + "%", NpgsqlDbType.Text)}))";
        if (q.Minimum is not null) where += $" AND cd.extract_amount>={Param(q.Minimum, NpgsqlDbType.Double)}";
        if (q.Maximum is not null) where += $" AND cd.extract_amount<={Param(q.Maximum, NpgsqlDbType.Double)}";
        if (q.Quality == "incomplete") where += " AND cd.data_quality_flag='incomplete'";
        if (q.Quality == "complete") where += " AND cd.data_quality_flag IS NULL";
        if (sunday && q.Sunday) where += " AND EXTRACT(DOW FROM cd.extract_date)=0";
        return where;
    }
    private async Task<object> Preview(PostgresTransactionContext db, DataBankQuery q, DataBankTemplate t, CancellationToken ct)
    {
        await using var command = db.CreateCommand("");
        string where = await PreviewPredicate(db, q, t, command, ct);
        string Param(object? value, NpgsqlDbType type)
        { command.Parameters.AddWithValue(type, value ?? DBNull.Value); return "$" + command.Parameters.Count.ToString(CultureInfo.InvariantCulture); }
        command.CommandText = $"SELECT count(*) FROM public.client_data cd WHERE {where} AND EXTRACT(DOW FROM cd.extract_date)=0";
        long sundays = (long)(await command.ExecuteScalarAsync(ct))!;
        if (q.Sunday) where += " AND EXTRACT(DOW FROM cd.extract_date)=0";
        long total; object? metrics = null;
        var scope = new { client_id = q.ClientId, template_id = t.Id, date_start = q.Start, date_end = q.End, search = q.Search,
            amount_min = q.Minimum, amount_max = q.Maximum, sunday_only = q.Sunday, data_quality = q.Quality, ledger_account_code = q.Account };
        if (q.Metrics)
        {
            // Exact PostgreSQL NUMERIC operates on raw JSON text, never REAL helpers.
            // Isolate invalid numeric casts without losing the readable page/count.
            int parameterCount = command.Parameters.Count;
            await using (var save = db.CreateCommand("SAVEPOINT databank_controls")) await save.ExecuteNonQueryAsync(ct);
            try
            {
                string spaces = Param(Spaces, NpgsqlDbType.Text), doc = Param(t.Document?.StorageKey, NpgsqlDbType.Text), pattern = Param(NumberPattern, NpgsqlDbType.Text);
                var footings = t.Columns.Where(c => c.Role == "footing").ToArray();
                var projections = new List<string> { $"NULLIF(BTRIM(cd.row_data->>{doc},{spaces}),'') COLLATE \"C\" AS document_id" };
                for (int i = 0; i < footings.Length; i++) projections.Add($"NULLIF(BTRIM(cd.row_data->>{Param(footings[i].StorageKey, NpgsqlDbType.Text)},{spaces}),'') AS amount_{i}");
                var aggregates = new List<string> { "count(*)", "count(DISTINCT document_id)", "count(*) FILTER(WHERE document_id IS NULL)" };
                for (int i = 0; i < footings.Length; i++)
                {
                    aggregates.Add($"COALESCE(sum(CASE WHEN amount_{i} ~ {pattern} THEN amount_{i}::numeric ELSE 0 END),0)::text");
                    aggregates.Add($"count(*) FILTER(WHERE amount_{i} IS NOT NULL AND amount_{i} !~ {pattern})");
                }
                command.CommandText = $"WITH source AS(SELECT {string.Join(',', projections)} FROM public.client_data cd WHERE {where}) SELECT {string.Join(',', aggregates)} FROM source";
                var totals = new Dictionary<string, string>(); bool valid = true; long documents, missing;
                await using (var reader = await command.ExecuteReaderAsync(ct))
                {
                    await reader.ReadAsync(ct); total = reader.GetInt64(0); documents = reader.GetInt64(1); missing = reader.GetInt64(2);
                    for (int i = 0; i < footings.Length; i++)
                    { string value = reader.GetString(3 + 2 * i); valid &= reader.GetInt64(4 + 2 * i) == 0 && value.Length <= 4096; totals[footings[i].Name] = value; }
                }
                metrics = valid ? new { status = "ok", scope, row_count = total, document_count = documents, missing_document_rows = missing,
                    document_column = t.Document?.Name, footing_configured = footings.Length > 0, footing_totals = totals } : (object)new { status = "unavailable", scope };
                await using var release = db.CreateCommand("RELEASE SAVEPOINT databank_controls"); await release.ExecuteNonQueryAsync(ct);
            }
            catch (PostgresException error) when (error.SqlState is "22003" or "22P02")
            {
                await using (var rollback = db.CreateCommand("ROLLBACK TO SAVEPOINT databank_controls")) await rollback.ExecuteNonQueryAsync(ct);
                await using (var release = db.CreateCommand("RELEASE SAVEPOINT databank_controls")) await release.ExecuteNonQueryAsync(ct);
                while (command.Parameters.Count > parameterCount) command.Parameters.RemoveAt(command.Parameters.Count - 1);
                command.CommandText = $"SELECT count(*) FROM public.client_data cd WHERE {where}";
                total = (long)(await command.ExecuteScalarAsync(ct))!; metrics = new { status = "unavailable", scope };
            }
            // Drop aggregate-only parameters before reusing the row predicate.
            while (command.Parameters.Count > parameterCount) command.Parameters.RemoveAt(command.Parameters.Count - 1);
        }
        else { command.CommandText = $"SELECT count(*) FROM public.client_data cd WHERE {where}"; total = (long)(await command.ExecuteScalarAsync(ct))!; }
        command.CommandText = $"""
            SELECT cd.id,CASE WHEN octet_length(cd.row_data::text)<={options.Value.MaximumResponseBytes} THEN cd.row_data::text END,
                cd.extract_amount,cd.extract_date::text,cd.data_quality_flag,cd.rate_source,cd.document_group_id
            FROM public.client_data cd WHERE {where}
            ORDER BY cd.extract_date ASC NULLS LAST,cd.source_order_upload_id ASC NULLS LAST,cd.source_row_number ASC NULLS LAST,cd.id
            LIMIT {Param(q.Limit, NpgsqlDbType.Integer)} OFFSET {Param((q.Page - 1) * q.Limit, NpgsqlDbType.Integer)}
            """;
        var rows = new List<Dictionary<string, object?>>(); long bytes = 0;
        await using (var reader = await command.ExecuteReaderAsync(ct))
        while (await reader.ReadAsync(ct))
        {
            if (reader.IsDBNull(1)) Reject("too_large", "databank_response_too_large_narrow_selection");
            string raw = reader.GetString(1); bytes += Encoding.UTF8.GetByteCount(raw);
            if (bytes > options.Value.MaximumResponseBytes) Reject("too_large", "databank_response_too_large_narrow_selection");
            using var json = JsonDocument.Parse(raw); var values = new Dictionary<string, JsonElement>(); var tokens = new Dictionary<string, string>();
            if (json.RootElement.ValueKind != JsonValueKind.Object) Reject("review", "databank_row_review_required");
            var names = t.Columns.ToDictionary(c => c.StorageKey, c => c.Name, StringComparer.Ordinal);
            foreach (var property in json.RootElement.EnumerateObject())
            {
                string name = names.GetValueOrDefault(property.Name, property.Name);
                if (values.ContainsKey(name)) Reject("review", "databank_row_key_review_required");
                values[name] = property.Value.Clone();
                if (property.Value.ValueKind == JsonValueKind.Number) tokens[name] = property.Value.GetRawText();
            }
            long id = reader.GetInt32(0); string? date = Text(reader, 3);
            string? document = t.Document is {} column && json.RootElement.TryGetProperty(column.StorageKey, out var cell)
                ? cell.ValueKind == JsonValueKind.String ? cell.GetString()?.Trim() : cell.ValueKind == JsonValueKind.Null ? null : cell.GetRawText().Trim() : null;
            string group = t.Purpose == "account_ledger" ? Text(reader, 6) ?? "__row_" + id
                : string.IsNullOrEmpty(document) ? "__row_" + id : t.Columns.Any(c => c.Role == "document_id") ? document : document + "||" + date;
            rows.Add(new() { ["id"] = id, ["row_data"] = values, ["numeric_text"] = tokens,
                ["extract_amount"] = reader.IsDBNull(2) ? (float?)null : reader.GetFloat(2), ["extract_date"] = date,
                ["data_quality_flag"] = Text(reader, 4), ["rate_source"] = Text(reader, 5), ["document_group_id"] = group });
        }
        if (options.Value.HistoryEnabled) await DataBankHistoryService.Flags(db, q.ClientId, t.Id, rows, ct);
        var result = new Dictionary<string, object?> { ["client_id"] = q.ClientId, ["template_id"] = t.Id, ["template_name"] = t.Name,
            ["total_rows"] = total, ["sunday_count"] = sundays, ["page"] = q.Page, ["limit"] = q.Limit, ["document_key_column"] = t.Document?.Name,
            ["date_column"] = t.Columns.FirstOrDefault(c => c.Type == "Date")?.Name, ["amount_columns"] = t.Columns.Where(c => c.Type == "Numeric").Select(c => c.Name).ToArray(),
            ["rows"] = rows, ["column_order"] = t.Columns.Select(c => c.Name).ToArray() };
        if (q.Metrics) result["preview_metrics"] = metrics;
        return result;
    }
}
