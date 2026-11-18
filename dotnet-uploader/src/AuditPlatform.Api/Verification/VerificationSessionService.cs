using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Verification;

/// <summary>Session discovery and lifecycle. All state changes use one supplied PostgreSQL transaction.</summary>
public sealed class VerificationSessionService(PostgresTransactions transactions, PostgresAuditWriter audit,
    IOptions<VerificationRuntimeOptions> options)
{
    private static readonly string[] Workstreams = ["cash", "bank", "local_vendor", "import_vendor", "export_customer", "payroll", "contractor"];
    private static readonly string[] VoucherAliases = ["Voucher ID", "Voucher No", "Voucher Number", "Voucher", "Invoice No", "Invoice Number"];
    private static readonly string[] AccountAliases = ["Account Name", "Account", "Account Detail", "Account Code", "Ledger", "Head"];
    private static readonly string[] DebitAliases = ["Debit", "Debit Amount", "Dr", "Dr Amount", "Debit (Dr)", "Dr. Amount"];
    private static readonly string[] CreditAliases = ["Credit", "Credit Amount", "Cr", "Cr Amount", "Credit (Cr)", "Cr. Amount"];

    private sealed record Actor(int TenantId, int UserId, string Username, string TenantRole);
    private sealed record Source(int Id, string Voucher, string AccountName, string? AccountCode,
        decimal Debit, decimal Credit, DateOnly? Date, decimal? Amount, JsonElement RowData = default);
    private sealed record Account(string Code, string Name);
    private sealed record SessionDetail(int Id, int ClientDataId, string? TransactionRef, string? LineItems,
        string? ProcedureData, string? ValidationStatus, string? ValidationMessage, string? Status,
        string? Comments, string? Observations, string? VerifiedBy, string? VerifiedAt, string? ClearedStatus,
        int? MatrixVersion, int? CategoryId, string? CategoryName, string RowData, string? ExtractAmount,
        string? ExtractDate, int TemplateId, string? ChangeStatus, int Version, string? OldRowData, string GroupId = "");
    private sealed record DocumentColumn(string Key, string? Role, string Type, int Order, bool TenantOwned);

    public Task<VerificationRuntimeResult> StartAsync(VerifiedSession? session, VerificationSessionStartRequest request,
        CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.ClientId < 1 || string.IsNullOrWhiteSpace(request.CashbookName) || request.CashbookName.Length > 240
            || !Workstreams.Contains(request.SessionType, StringComparer.Ordinal)) return Invalid("invalid_verification_session");
        if (!DateOnly.TryParseExact(request.PeriodStart, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var start)
            || !DateOnly.TryParseExact(request.PeriodEnd, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var end)
            || start > end) return Invalid("invalid_verification_period");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await AuthorizeClientAsync(db, session, request.ClientId, inner);
            if (actor is null) return new("forbidden", Code: "verification_access_denied");
            var rulebook = await ReadCurrentRulebookAsync(db, actor.TenantId, inner);
            if (rulebook is null) return new("unavailable", Code: "verification_rulebook_unavailable");
            var routing = await ReadRoutingAsync(db, request.ClientId, inner);
            string[] selectedCodes = routing.Where(x => x.Stream == request.SessionType).Select(x => x.Code).Distinct(StringComparer.Ordinal).ToArray();
            if (selectedCodes.Length == 0) return Invalid("verification_accounts_not_configured");
            var accounts = await ReadLatestAccountNamesAsync(db, request.ClientId, inner);
            string? selectedCode = ResolveSelectedAccount(request.CashbookName, selectedCodes, accounts);
            if (selectedCode is null) return Invalid("verification_account_not_selected");
            var source = await ReadSourceAsync(db, request.ClientId, start, end, token, inner);
            if (source is null) return new("too_large", Code: "verification_source_capacity");
            var selected = ResolveLines(source, request.SessionType, selectedCode, request.CashbookName.Trim(),
                routing.Where(x => x.Stream is "cash" or "bank").Select(x => x.Code).ToHashSet(StringComparer.Ordinal));
            if (selected.Count == 0) return Invalid("verification_transactions_not_found");
            if (selected.Count > options.Value.MaximumSourceRows) return new("too_large", Code: "verification_source_capacity");
            int version = checked((int)rulebook.Value.Version);
            int sessionId;
            await using (var insert = db.CreateCommand("""
                INSERT INTO public.verification_sessions
                    (client_id,cashbook_name,period_start,period_end,created_by,status,total_transactions,session_type,rulebook_version)
                VALUES($1,$2,$3,$4,$5,'In Progress',$6,$7,$8) RETURNING id
                """))
            {
                insert.Parameters.AddWithValue(request.ClientId); insert.Parameters.AddWithValue(request.CashbookName.Trim());
                insert.Parameters.AddWithValue(start); insert.Parameters.AddWithValue(end);
                insert.Parameters.AddWithValue(actor.Username); insert.Parameters.AddWithValue(selected.Count);
                insert.Parameters.AddWithValue(request.SessionType); insert.Parameters.AddWithValue(version);
                sessionId = (int)(await insert.ExecuteScalarAsync(inner))!;
            }
            await InsertDetailsAsync(db, sessionId, selected, inner);
            await SeedPriorStatusesAsync(db, sessionId, null, inner);
            await AuditAsync(db, actor, request.ClientId, "VERIFICATION_SESSION_STARTED",
                new { session_id = sessionId, session_type = request.SessionType, line_count = selected.Count,
                    period_start = start.ToString("yyyy-MM-dd"), period_end = end.ToString("yyyy-MM-dd"), rulebook_version = version }, inner);
            return Success(new { session_id = sessionId, cashbook_name = request.CashbookName.Trim(),
                total_transactions = selected.Count, message = $"Verification session started with {selected.Count} transactions." });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> PreviewAsync(VerifiedSession? session, int clientId, string cashbook,
        string periodStart, string periodEnd, string sessionType, int page, int limit, CancellationToken ct = default) => Run(async token =>
    {
        if (clientId < 1 || string.IsNullOrWhiteSpace(cashbook) || !Workstreams.Contains(sessionType, StringComparer.Ordinal)
            || page < 1 || limit is < 1 or > 200 || !TryPeriod(periodStart, periodEnd, out var start, out var end))
            return Invalid("invalid_verification_preview");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            if (await AuthorizeClientAsync(db, session, clientId, inner) is null) return new("forbidden", Code: "verification_access_denied");
            var routing = await ReadRoutingAsync(db, clientId, inner);
            string[] assigned = routing.Where(x => x.Stream == sessionType).Select(x => x.Code).Distinct(StringComparer.Ordinal).ToArray();
            if (assigned.Length == 0) return Invalid("verification_accounts_not_configured");
            var names = await ReadLatestAccountNamesAsync(db, clientId, inner);
            string? selectedCode = ResolveSelectedAccount(cashbook, assigned, names);
            if (selectedCode is null) return Invalid("verification_account_not_selected");
            var source = await ReadSourceAsync(db, clientId, start, end, token, inner);
            if (source is null) return new("too_large", Code: "verification_source_capacity");
            var treasuryCodes = routing.Where(x => x.Stream is "cash" or "bank").Select(x => x.Code).ToHashSet(StringComparer.Ordinal);
            var lines = ResolveLines(source, sessionType, selectedCode, cashbook.Trim(), treasuryCodes);
            var allDetails = new Dictionary<int, string?>();
            if (lines.Count > 0)
            {
                await using var status = db.CreateCommand("""
                    SELECT DISTINCT ON(client_data_id) client_data_id,cleared_status
                    FROM public.verification_details WHERE client_data_id=ANY($1) AND range_status='active'
                    ORDER BY client_data_id,id DESC
                    """);
                status.Parameters.AddWithValue(lines.Select(x => x.Id).ToArray());
                await using var reader = await status.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner)) allDetails[reader.GetInt32(0)] = NullableString(reader, 1);
            }
            var voucherStatus = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var group in lines.GroupBy(x => x.Voucher, StringComparer.Ordinal))
            {
                var saved = group.Where(x => allDetails.ContainsKey(x.Id)).Select(x => allDetails[x.Id]).ToArray();
                voucherStatus[group.Key] = saved.Length == 0 ? "Not Started"
                    : saved.All(x => x == "Cleared") ? "Cleared"
                    : saved.Any(x => x == "Query") ? "Query" : "Pending";
            }
            var pageRows = lines.Skip(checked((page - 1) * limit)).Take(limit).Select(line =>
            {
                var presentation = PreviewPresentation(line, lines.Where(x => x.Voucher == line.Voucher).ToArray(),
                    sessionType, selectedCode, cashbook.Trim());
                return new { row_data = line.RowData, extract_date = line.Date?.ToString("yyyy-MM-dd"), extract_amount = line.Amount,
                    status = voucherStatus.GetValueOrDefault(line.Voucher, "Not Started"), amount = presentation.Amount,
                    direction = presentation.Direction, treasury_amount = presentation.TreasuryAmount,
                    counterpart_total = presentation.CounterpartTotal, imbalance_amount = presentation.Imbalance,
                    is_balanced = presentation.IsBalanced,
                    debit_credit_keys = line.RowData.ValueKind == JsonValueKind.Object
                        ? line.RowData.EnumerateObject().Where(p => DebitAliases.Concat(CreditAliases).Contains(p.Name, StringComparer.Ordinal)).Select(p => p.Name).ToArray() : [] };
            }).ToArray();
            return Success(new { total_rows = lines.Count, page, limit, rows = pageRows });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> DiscoverAsync(VerifiedSession? session, int clientId, string workstream,
        string? periodStart, string? periodEnd, string? categoryGroup = null, CancellationToken ct = default) => Run(async token =>
    {
        if (clientId < 1 || !Workstreams.Contains(workstream, StringComparer.Ordinal)
            || (periodStart is null) != (periodEnd is null)) return Invalid("invalid_verification_discovery");
        DateOnly start = new(2000, 1, 1), end = new(2999, 12, 31);
        if (periodStart is not null && !TryPeriod(periodStart, periodEnd!, out start, out end)) return Invalid("invalid_verification_period");
        if (workstream == "cash" && categoryGroup is not (null or "cash" or "bank")) return Invalid("invalid_verification_category_group");
        if (workstream != "cash" && categoryGroup is not null) return Invalid("invalid_verification_category_group");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            if (await AuthorizeClientAsync(db, session, clientId, inner) is null) return new("forbidden", Code: "verification_access_denied");
            var routing = await ReadRoutingAsync(db, clientId, inner);
            string[] codes = workstream == "cash"
                ? routing.Where(x => categoryGroup is null || x.Stream == categoryGroup).Where(x => x.Stream is "cash" or "bank").Select(x => x.Code).Distinct(StringComparer.Ordinal).ToArray()
                : routing.Where(x => x.Stream == workstream).Select(x => x.Code).Distinct(StringComparer.Ordinal).ToArray();
            string outputName = workstream is "local_vendor" or "import_vendor" ? "vendors"
                : workstream is "export_customer" or "payroll" or "contractor" ? "accounts" : "cashbooks";
            if (codes.Length == 0) return Success(new Dictionary<string, object?> { [outputName] = Array.Empty<string>(), ["count"] = 0, ["setup_required"] = true });
            var rows = await ReadSourceAsync(db, clientId, start, end, token, inner);
            if (rows is null) return new("too_large", Code: "verification_source_capacity");
            string[] labels;
            if (workstream is "cash" or "bank")
                labels = rows.Where(row => row.AccountCode is not null && codes.Contains(row.AccountCode, StringComparer.Ordinal))
                    .Select(row => row.AccountName).Where(name => name.Length > 0).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
            else
            {
                var treasuryCodes = routing.Where(x => x.Stream is "cash" or "bank").Select(x => x.Code).ToHashSet(StringComparer.Ordinal);
                var names = await ReadLatestAccountNamesAsync(db, clientId, inner);
                labels = rows.GroupBy(row => row.Voucher, StringComparer.Ordinal)
                    .Where(group => !group.Any(row => row.AccountCode is not null && treasuryCodes.Contains(row.AccountCode)))
                    .SelectMany(group => group.Where(row => row.AccountCode is not null && codes.Contains(row.AccountCode, StringComparer.Ordinal)
                        && (row.Debit > 0 || row.Credit > 0)))
                    .Select(row => workstream is "local_vendor" or "import_vendor"
                        ? $"{row.AccountCode} :: {names.GetValueOrDefault(row.AccountCode!, row.AccountCode!)}"
                        : $"{row.AccountCode} :: {names.GetValueOrDefault(row.AccountCode!, row.AccountCode!)}")
                    .Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
            }
            return Success(new Dictionary<string, object?> { [outputName] = labels, ["count"] = labels.Length, ["setup_required"] = false });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> LatestDateAsync(VerifiedSession? session, int clientId, string workstream,
        string? categoryGroup = null, CancellationToken ct = default) => Run(async token =>
    {
        if (clientId < 1 || !Workstreams.Contains(workstream, StringComparer.Ordinal)
            || (workstream is "cash" or "bank" ? categoryGroup is not (null or "cash" or "bank") : categoryGroup is not null))
            return Invalid("invalid_verification_discovery");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            if (await AuthorizeClientAsync(db, session, clientId, inner) is null) return new("forbidden", Code: "verification_access_denied");
            var routing = await ReadRoutingAsync(db, clientId, inner);
            string[] codes = workstream is "cash" or "bank"
                ? routing.Where(x => x.Stream == (categoryGroup ?? workstream)).Select(x => x.Code).Distinct(StringComparer.Ordinal).ToArray()
                : routing.Where(x => x.Stream == workstream).Select(x => x.Code).Distinct(StringComparer.Ordinal).ToArray();
            if (codes.Length == 0) return Success(new { latest_date = (string?)null, setup_required = true });
            var rows = await ReadSourceAsync(db, clientId, new DateOnly(2000, 1, 1), new DateOnly(2999, 12, 31), token, inner);
            if (rows is null) return new("too_large", Code: "verification_source_capacity");
            var treasuryCodes = routing.Where(x => x.Stream is "cash" or "bank").Select(x => x.Code).ToHashSet(StringComparer.Ordinal);
            var eligible = rows.Where(row => row.AccountCode is not null && codes.Contains(row.AccountCode, StringComparer.Ordinal));
            if (workstream is not ("cash" or "bank"))
                eligible = eligible.Where(row => row.Debit > 0 || row.Credit > 0).GroupBy(row => row.Voucher, StringComparer.Ordinal)
                    .Where(group => !group.Any(row => row.AccountCode is not null && treasuryCodes.Contains(row.AccountCode)))
                    .SelectMany(group => group);
            var latest = eligible.Where(row => row.Date.HasValue).Select(row => row.Date!.Value).DefaultIfEmpty().Max();
            return Success(new { latest_date = latest == default ? null : latest.ToString("yyyy-MM-dd"), setup_required = false });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SessionIndexAsync(VerifiedSession? session, int sessionId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1) return new("not_found", Code: "verification_session_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, false, inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            var header = await ReadSessionHeaderAsync(db, sessionId, inner);
            if (header is null) return new("not_found", Code: "verification_session_not_found");
            var details = await ReadSessionDetailsAsync(db, owner.Value.Actor.TenantId, sessionId, activeOnly: true, inner);
            var rows = details.OrderBy(d => d.Id).Select(d => new { detail_id = d.Id, client_data_id = d.ClientDataId,
                voucher_id = d.TransactionRef, validation_status = d.ValidationStatus, verification_status = d.Status,
                cleared_status = d.ClearedStatus, matrix_version_applied = d.MatrixVersion,
                extract_amount = ParseJsonValue(d.ExtractAmount), extract_date = d.ExtractDate }).ToArray();
            return Success(new { session = header, transactions = rows });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SessionProgressAsync(VerifiedSession? session, int sessionId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1) return new("not_found", Code: "verification_session_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            if (await SessionOwnerAsync(db, session, sessionId, false, inner) is null) return new("not_found", Code: "verification_session_not_found");
            await using var command = db.CreateCommand("""
                SELECT total_transactions,verified_count FROM public.verification_sessions WHERE id=$1 AND status!='Cancelled'
                """);
            command.Parameters.AddWithValue(sessionId); await using var reader = await command.ExecuteReaderAsync(inner);
            if (!await reader.ReadAsync(inner)) return new("not_found", Code: "verification_session_not_found");
            return Success(new { total_transactions = reader.GetInt32(0), verified_count = reader.GetInt32(1) });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> VoucherStatsAsync(VerifiedSession? session, int sessionId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1) return new("not_found", Code: "verification_session_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            if (await SessionOwnerAsync(db, session, sessionId, false, inner) is null) return new("not_found", Code: "verification_session_not_found");
            var lines = new Dictionary<string, List<string?>>(StringComparer.Ordinal);
            await using var command = db.CreateCommand("""
                SELECT transaction_ref,cleared_status FROM public.verification_details
                WHERE session_id=$1 AND range_status='active' ORDER BY id
                """);
            command.Parameters.AddWithValue(sessionId); await using var reader = await command.ExecuteReaderAsync(inner);
            while (await reader.ReadAsync(inner))
            {
                if (reader.IsDBNull(0)) continue;
                string refId = reader.GetString(0); if (refId.Length == 0) continue;
                if (!lines.TryGetValue(refId, out var statuses)) lines[refId] = statuses = [];
                statuses.Add(NullableString(reader, 1));
            }
            int cleared = 0, query = 0, pending = 0;
            foreach (var statuses in lines.Values)
            {
                if (statuses.All(s => s == "Cleared")) cleared++;
                else if (statuses.Any(s => s == "Query")) query++;
                else pending++;
            }
            return Success(new { total = lines.Count, cleared, query, pending });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> NextVoucherAsync(VerifiedSession? session, int sessionId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1) return new("not_found", Code: "verification_session_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, false, inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            var details = await ReadSessionDetailsAsync(db, owner.Value.Actor.TenantId, sessionId, activeOnly: true, inner);
            var pending = details.Where(d => d.Status == "Pending").OrderBy(d => d.Id).FirstOrDefault();
            if (pending is null) return Success(new { message = "All transactions in this session have been verified.", completed = true });
            var group = details.Where(d => d.GroupId == pending.GroupId).OrderBy(d => d.ClientDataId).ThenBy(d => d.Id).ToArray();
            return Success(new { voucher_id = pending.TransactionRef, voucher_date = group.FirstOrDefault()?.ExtractDate,
                document_group_id = pending.GroupId, line_count = group.Length,
                lines = group.Select(BuildVerificationLine).ToArray() });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> VoucherNavigationAsync(VerifiedSession? session, int sessionId,
        int currentDetailId, string direction, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1 || currentDetailId < 1 || direction is not ("next" or "prev")) return Invalid("invalid_voucher_navigation");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, false, inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            var result = await VoucherNavigationWithinTransactionAsync(db, owner.Value.Actor.TenantId,
                sessionId, currentDetailId, direction, inner);
            return result is null ? new("not_found", Code: "verification_detail_not_found") : Success(result);
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> AuditedCashbookAsync(VerifiedSession? session, int sessionId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1) return new("not_found", Code: "verification_session_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, false, inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            var header = await ReadSessionHeaderAsync(db, sessionId, inner);
            if (header is null) return new("not_found", Code: "verification_session_not_found");
            var details = await ReadSessionDetailsAsync(db, owner.Value.Actor.TenantId, sessionId, activeOnly: true, inner);
            var transactions = details.OrderBy(d => d.Id).Select(d => new
            {
                detail_id = d.Id, client_data_id = d.ClientDataId, voucher_id = d.TransactionRef,
                row_data = ParseJsonValue(d.RowData), extract_amount = ParseJsonValue(d.ExtractAmount), extract_date = d.ExtractDate,
                line_items = ParseJsonValue(d.LineItems) ?? JsonSerializer.SerializeToElement(Array.Empty<object>()),
                procedure_flags = FlattenProcedureData(d.ProcedureData), observations = d.Observations ?? "", comments = d.Comments ?? "",
                validation_status = d.ValidationStatus, validation_message = d.ValidationMessage, verification_status = d.Status,
                verified_by = d.VerifiedBy, verified_at = d.VerifiedAt, cleared_status = d.ClearedStatus,
                matrix_version_applied = d.MatrixVersion, category_id_applied = d.CategoryId, category_name_applied = d.CategoryName
            }).ToArray();
            return Success(new { session = header, transactions });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> RulebookAsync(VerifiedSession? session, int sessionId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1) return new("not_found", Code: "verification_session_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, false, inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            var current = await ReadCurrentRulebookAsync(db, owner.Value.Actor.TenantId, inner);
            if (current is null) return new("unavailable", Code: "verification_rulebook_unavailable");
            if (owner.Value.RulebookVersion is int oldVersion && oldVersion != current.Value.Version)
                return await VersionMismatch(db, inner, sessionId, owner.Value.Actor.TenantId, oldVersion, current.Value.Version, token);
            return Success(new { session_id = sessionId, rulebook = current.Value.Payload });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> RebaseAsync(VerifiedSession? session, int sessionId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1) return new("not_found", Code: "verification_session_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, true, inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            var current = await ReadCurrentRulebookAsync(db, owner.Value.Actor.TenantId, inner);
            if (current is null) return new("unavailable", Code: "verification_rulebook_unavailable");
            if (owner.Value.RulebookVersion == current.Value.Version)
                return Success(new { session_id = sessionId, rulebook_version = current.Value.Version, recovered = false,
                    message = "Verification session already uses the current Rulebook." });
            await using var update = db.CreateCommand("""
                UPDATE public.verification_sessions SET rulebook_version=$1
                WHERE id=$2 AND client_id=$3 AND status!='Cancelled'
                """);
            update.Parameters.AddWithValue((int)current.Value.Version); update.Parameters.AddWithValue(sessionId);
            update.Parameters.AddWithValue(owner.Value.ClientId); await update.ExecuteNonQueryAsync(inner);
            await AuditAsync(db, owner.Value.Actor, owner.Value.ClientId, "VERIFICATION_RULEBOOK_REBASED",
                new { session_id = sessionId, prior_version = owner.Value.RulebookVersion, rulebook_version = current.Value.Version }, inner);
            return Success(new { session_id = sessionId, rulebook_version = current.Value.Version, recovered = true,
                message = "Verification session is ready to resume with the current Rulebook." });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> PresenceAsync(VerifiedSession? session, int sessionId, string action, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1 || action is not ("enter" or "heartbeat" or "leave" or "close")) return Invalid("invalid_verification_presence");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, action is "enter" or "close", inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            if (action == "enter")
            {
                await using var upsert = db.CreateCommand("""
                    INSERT INTO public.verification_presence(verification_session_id,session_token,user_id,last_heartbeat,ended_at)
                    VALUES($1,$2,$3,now(),NULL) ON CONFLICT(verification_session_id,session_token)
                    DO UPDATE SET last_heartbeat=now(),ended_at=NULL
                    """);
                upsert.Parameters.AddWithValue(sessionId); upsert.Parameters.AddWithValue(session!.Token);
                upsert.Parameters.AddWithValue(session.UserId); await upsert.ExecuteNonQueryAsync(inner);
                return Success(new { active = true, session_id = sessionId });
            }
            if (action == "heartbeat")
            {
                await using var update = db.CreateCommand("""
                    UPDATE public.verification_presence SET last_heartbeat=now(),ended_at=NULL
                    WHERE verification_session_id=$1 AND session_token=$2 AND user_id=$3
                    """);
                update.Parameters.AddWithValue(sessionId); update.Parameters.AddWithValue(session!.Token); update.Parameters.AddWithValue(session.UserId);
                bool active = await update.ExecuteNonQueryAsync(inner) > 0;
                return Success(new { active, session_id = sessionId });
            }
            if (action == "leave")
            {
                await using var update = db.CreateCommand("""
                    UPDATE public.verification_presence SET ended_at=now()
                    WHERE verification_session_id=$1 AND session_token=$2 AND user_id=$3
                    """);
                update.Parameters.AddWithValue(sessionId); update.Parameters.AddWithValue(session!.Token); update.Parameters.AddWithValue(session.UserId);
                await update.ExecuteNonQueryAsync(inner);
                return Success(new { active = false, session_id = sessionId });
            }
            bool closed;
            await using (var close = db.CreateCommand("""
                UPDATE public.verification_sessions SET status='Closed'
                WHERE id=$1 AND client_id=$2 AND status='In Progress'
                  AND EXISTS(SELECT 1 FROM public.verification_presence p WHERE p.verification_session_id=$1
                    AND p.session_token=$3 AND p.user_id=$4 AND p.ended_at IS NULL)
                """))
            {
                close.Parameters.AddWithValue(sessionId); close.Parameters.AddWithValue(owner.Value.ClientId);
                close.Parameters.AddWithValue(session!.Token); close.Parameters.AddWithValue(session.UserId);
                closed = await close.ExecuteNonQueryAsync(inner) > 0;
            }
            await using (var leave = db.CreateCommand("""
                UPDATE public.verification_presence SET ended_at=now()
                WHERE verification_session_id=$1 AND session_token=$2 AND ended_at IS NULL
                """))
            { leave.Parameters.AddWithValue(sessionId); leave.Parameters.AddWithValue(session!.Token); await leave.ExecuteNonQueryAsync(inner); }
            return Success(new { closed, session_id = sessionId });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> ReconcileRangeAsync(VerifiedSession? session, int sessionId,
        string periodStart, string periodEnd, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1 || !TryPeriod(periodStart, periodEnd, out var start, out var end)) return Invalid("invalid_verification_period");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await SessionOwnerAsync(db, session, sessionId, true, inner);
            if (owner is null) return new("not_found", Code: "verification_session_not_found");
            string cashbook; string type;
            await using (var find = db.CreateCommand("SELECT cashbook_name,session_type FROM public.verification_sessions WHERE id=$1 AND client_id=$2"))
            {
                find.Parameters.AddWithValue(sessionId); find.Parameters.AddWithValue(owner.Value.ClientId);
                await using var reader = await find.ExecuteReaderAsync(inner);
                if (!await reader.ReadAsync(inner)) return new("not_found", Code: "verification_session_not_found");
                cashbook = reader.GetString(0); type = reader.GetString(1);
            }
            var routing = await ReadRoutingAsync(db, owner.Value.ClientId, inner);
            var codes = routing.Where(x => x.Stream == type).Select(x => x.Code).Distinct(StringComparer.Ordinal).ToArray();
            var accountNames = await ReadLatestAccountNamesAsync(db, owner.Value.ClientId, inner);
            var selectedCode = ResolveSelectedAccount(cashbook, codes, accountNames);
            if (selectedCode is null) return new("conflict", Code: "verification_routing_changed");
            var source = await ReadSourceAsync(db, owner.Value.ClientId, start, end, token, inner);
            if (source is null) return new("too_large", Code: "verification_source_capacity");
            var treasury = routing.Where(x => x.Stream is "cash" or "bank").Select(x => x.Code).ToHashSet(StringComparer.Ordinal);
            var targets = ResolveLines(source, type, selectedCode, cashbook, treasury).GroupBy(x => x.Id).ToDictionary(g => g.Key, g => g.First().Voucher);
            var existing = new Dictionary<int, (int Id, string Range)>();
            await using (var find = db.CreateCommand("SELECT id,client_data_id,range_status FROM public.verification_details WHERE session_id=$1 ORDER BY id"))
            {
                find.Parameters.AddWithValue(sessionId); await using var reader = await find.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner)) existing[reader.GetInt32(1)] = (reader.GetInt32(0), reader.GetString(2));
            }
            var addedIds = targets.Keys.Where(id => !existing.ContainsKey(id)).ToArray();
            var reactivateIds = targets.Keys.Where(id => existing.TryGetValue(id, out var row) && row.Range == "excluded").ToArray();
            var excludeIds = existing.Where(pair => pair.Value.Range == "active" && !targets.ContainsKey(pair.Key)).Select(pair => pair.Value.Id).ToArray();
            await InsertDetailsAsync(db, sessionId,
                addedIds.Select(id => new Source(id, targets[id], "", null, 0, 0, null, null)).ToList(), inner);
            if (reactivateIds.Length > 0)
            {
                await using var update = db.CreateCommand("""
                    UPDATE public.verification_details SET range_status='active',excluded_at=NULL
                    WHERE session_id=$1 AND client_data_id=ANY($2)
                    """);
                update.Parameters.AddWithValue(sessionId); update.Parameters.AddWithValue(reactivateIds); await update.ExecuteNonQueryAsync(inner);
            }
            if (excludeIds.Length > 0)
            {
                await using var update = db.CreateCommand("""
                    UPDATE public.verification_details SET range_status='excluded',excluded_at=now()
                    WHERE id=ANY($1) AND session_id=$2 AND range_status='active'
                    """);
                update.Parameters.AddWithValue(excludeIds); update.Parameters.AddWithValue(sessionId); await update.ExecuteNonQueryAsync(inner);
            }
            await SeedPriorStatusesAsync(db, sessionId, addedIds, inner);
            int total;
            await using (var count = db.CreateCommand("SELECT count(*) FROM public.verification_details WHERE session_id=$1 AND range_status='active'"))
            { count.Parameters.AddWithValue(sessionId); total = Convert.ToInt32(await count.ExecuteScalarAsync(inner), CultureInfo.InvariantCulture); }
            await using (var update = db.CreateCommand("""
                UPDATE public.verification_sessions SET period_start=$1,period_end=$2,total_transactions=$3
                WHERE id=$4 AND client_id=$5
                """))
            {
                update.Parameters.AddWithValue(start); update.Parameters.AddWithValue(end); update.Parameters.AddWithValue(total);
                update.Parameters.AddWithValue(sessionId); update.Parameters.AddWithValue(owner.Value.ClientId); await update.ExecuteNonQueryAsync(inner);
            }
            await AuditAsync(db, owner.Value.Actor, owner.Value.ClientId, "VERIFICATION_SESSION_RANGE_RECONCILED",
                new { session_id = sessionId, period_start = start.ToString("yyyy-MM-dd"), period_end = end.ToString("yyyy-MM-dd"),
                    added_count = addedIds.Length, reactivated_count = reactivateIds.Length, excluded_count = excludeIds.Length, total_active = total }, inner);
            return Success(new { session_id = sessionId, period_start = start.ToString("yyyy-MM-dd"), period_end = end.ToString("yyyy-MM-dd"),
                added_count = addedIds.Length, reactivated_count = reactivateIds.Length, excluded_count = excludeIds.Length,
                total_active = total, excluded_detail_ids = excludeIds });
        }, token);
    }, ct);

    private async Task<VerificationRuntimeResult> VersionMismatch(PostgresTransactionContext db, CancellationToken ct,
        int sessionId, int tenant, int oldVersion, long currentVersion, CancellationToken token)
    {
        var changes = new List<object>();
        await using var command = db.CreateCommand("""
            SELECT c.config_version,c.entity_kind,c.entity_id,c.action,c.entity_name,COALESCE(u.username,'System'),c.changed_at
            FROM public.platform_audit_rule_changes c LEFT JOIN public.users u ON u.id=c.actor_user_id
            WHERE c.tenant_id=$1 AND c.config_version>$2 ORDER BY c.config_version LIMIT 100
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(oldVersion);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) changes.Add(new { config_version = reader.GetInt64(0), entity_type = reader.GetString(1),
            entity_id = NullableInt(reader, 2), action = reader.GetString(3), entity_name = NullableString(reader, 4),
            changed_by = reader.GetString(5), changed_at = reader.GetDateTime(6) });
        return new("conflict", JsonSerializer.SerializeToElement(new { code = "RULEBOOK_VERSION_MISMATCH",
            message = "This Verification session uses an older Rulebook version.", session_id = sessionId,
            session_version = oldVersion, current_version = currentVersion, changes,
            recovery = new { method = "POST", path = $"/api/verification/session/{sessionId}/rulebook/rebase", preserves_work = true } }),
            "RULEBOOK_VERSION_MISMATCH");
    }

    private async Task<(Actor Actor, int ClientId, int? RulebookVersion)?> SessionOwnerAsync(PostgresTransactionContext db,
        VerifiedSession? session, int sessionId, bool writing, CancellationToken ct)
    {
        if (session is null || !session.TenantContextRequired || session.TenantId is not > 0
            || !AuthenticationService.ValidToken(session.Token) || session.UserId < 1) return null;
        await using var command = db.CreateCommand("""
            SELECT v.client_id,v.rulebook_version,u.username,m.tenant_role
            FROM public.verification_sessions v
            JOIN public.clients c ON c.id=v.client_id AND c.is_active IS TRUE
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=$3
            JOIN public.sessions s ON s.token=$1 AND s.user_id=$2 AND s.expiry>now()
                AND s.selected_client_id=c.id AND s.selected_tenant_id=$3 AND s.context_version=$4
            JOIN public.users u ON u.id=s.user_id AND u.is_active IS TRUE
            JOIN public.platform_tenant_memberships m ON m.tenant_id=$3 AND m.user_id=u.id AND m.is_active IS TRUE
            WHERE v.id=$5 AND v.status!='Cancelled'
              AND (m.tenant_role='admin' OR EXISTS(
                SELECT 1 FROM public.platform_engagement_assignments a
                JOIN public.platform_audit_engagements e ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                WHERE a.user_id=u.id AND a.firm_tenant_id=$3 AND e.client_id=c.id AND a.is_active IS TRUE AND e.is_active IS TRUE))
            """ + (writing ? " FOR UPDATE OF v,s" : ""));
        command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
        command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion); command.Parameters.AddWithValue(sessionId);
        await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct)) return null;
        return (new(session.TenantId.Value, session.UserId, reader.GetString(2), reader.GetString(3)),
            reader.GetInt32(0), NullableInt(reader, 1));
    }

    internal async Task<(VerificationReviewContext? Context, VerificationRuntimeResult? Failure)> ResolveReviewContextAsync(
        PostgresTransactionContext db, VerifiedSession? session, int sessionId, CancellationToken ct)
    {
        var owner = await SessionOwnerAsync(db, session, sessionId, true, ct);
        if (owner is null) return (null, new("not_found", Code: "verification_session_not_found"));
        var current = await ReadCurrentRulebookLockedAsync(db, owner.Value.Actor.TenantId, ct);
        if (current is null) return (null, new("unavailable", Code: "verification_rulebook_unavailable"));
        if (owner.Value.RulebookVersion is int oldVersion && oldVersion != current.Value.Version)
            return (null, await VersionMismatch(db, ct, sessionId, owner.Value.Actor.TenantId, oldVersion, current.Value.Version, ct));
        return (new(owner.Value.Actor.TenantId, owner.Value.Actor.UserId, owner.Value.Actor.Username,
            owner.Value.Actor.TenantRole, owner.Value.ClientId, owner.Value.RulebookVersion,
            checked((int)current.Value.Version), current.Value.Payload), null);
    }

    // Read-only projections use the same selected-client/session and frozen Rulebook checks
    // without taking the write lock used by review mutations.
    internal async Task<(VerificationReviewContext? Context, VerificationRuntimeResult? Failure)> ResolveReadContextAsync(
        PostgresTransactionContext db, VerifiedSession? session, int sessionId, CancellationToken ct)
    {
        var owner = await SessionOwnerAsync(db, session, sessionId, false, ct);
        if (owner is null) return (null, new("not_found", Code: "verification_session_not_found"));
        var current = await ReadCurrentRulebookAsync(db, owner.Value.Actor.TenantId, ct);
        if (current is null) return (null, new("unavailable", Code: "verification_rulebook_unavailable"));
        if (owner.Value.RulebookVersion is int oldVersion && oldVersion != current.Value.Version)
            return (null, await VersionMismatch(db, ct, sessionId, owner.Value.Actor.TenantId, oldVersion, current.Value.Version, ct));
        return (new(owner.Value.Actor.TenantId, owner.Value.Actor.UserId, owner.Value.Actor.Username,
            owner.Value.Actor.TenantRole, owner.Value.ClientId, owner.Value.RulebookVersion,
            checked((int)current.Value.Version), current.Value.Payload), null);
    }

    internal static async Task<Dictionary<string, object?>?> VoucherNavigationWithinTransactionAsync(
        PostgresTransactionContext db, int tenantId, int sessionId, int currentDetailId, string direction, CancellationToken ct)
    {
        var details = await ReadSessionDetailsAsync(db, tenantId, sessionId, activeOnly: true, ct);
        var groups = details.GroupBy(d => d.GroupId, StringComparer.Ordinal).ToArray();
        int currentIndex = Array.FindIndex(groups, group => group.Any(d => d.Id == currentDetailId));
        if (currentIndex < 0) return null;
        int targetIndex = direction == "prev" ? currentIndex - 1 : currentIndex + 1;
        if (targetIndex < 0 || targetIndex >= groups.Length)
            return new(StringComparer.Ordinal)
            {
                ["boundary"] = true,
                ["message"] = direction == "prev" ? "No previous voucher." : "No next voucher.",
                ["voucher_index"] = currentIndex + 1,
                ["total_vouchers"] = groups.Length
            };
        var target = groups[targetIndex].OrderBy(d => d.ClientDataId).ThenBy(d => d.Id).ToArray();
        var anchor = target[0];
        return new(StringComparer.Ordinal)
        {
            ["boundary"] = false, ["voucher_id"] = anchor.TransactionRef, ["voucher_date"] = anchor.ExtractDate,
            ["document_group_id"] = anchor.GroupId, ["line_count"] = target.Length,
            ["lines"] = target.Select(BuildVerificationLine).ToArray(), ["voucher_index"] = targetIndex + 1,
            ["total_vouchers"] = groups.Length
        };
    }

    private async Task<Actor?> AuthorizeClientAsync(PostgresTransactionContext db, VerifiedSession? session, int clientId, CancellationToken ct)
    {
        if (session is null || !session.TenantContextRequired || session.TenantId is not > 0
            || !AuthenticationService.ValidToken(session.Token) || session.UserId < 1) return null;
        await using var command = db.CreateCommand("""
            SELECT u.username,m.tenant_role FROM public.sessions s
            JOIN public.users u ON u.id=s.user_id AND u.is_active IS TRUE
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.tenant_id=$3 AND m.is_active IS TRUE
            JOIN public.platform_tenants t ON t.id=m.tenant_id AND t.is_active IS TRUE
            JOIN public.clients c ON c.id=s.selected_client_id AND c.is_active IS TRUE
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=t.id
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND s.selected_client_id=$4
              AND s.selected_tenant_id=$3 AND s.context_version=$5 AND c.id=$4
              AND (m.tenant_role='admin' OR EXISTS(
                SELECT 1 FROM public.platform_engagement_assignments a
                JOIN public.platform_audit_engagements e ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                WHERE a.user_id=u.id AND a.firm_tenant_id=$3 AND e.client_id=c.id AND a.is_active IS TRUE AND e.is_active IS TRUE))
            """);
        command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
        command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(session.ContextVersion);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? new(session.TenantId.Value, session.UserId, reader.GetString(0), reader.GetString(1)) : null;
    }

    private static async Task<(long Version, JsonElement Payload)?> ReadCurrentRulebookAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT s.config_version,s.payload::text FROM public.platform_audit_rule_snapshots s
            JOIN public.platform_audit_rule_state st ON st.tenant_id=s.tenant_id AND st.config_version=s.config_version
            WHERE s.tenant_id=$1 AND s.schema_version=2 AND s.evaluator_version=1
            """);
        command.Parameters.AddWithValue(tenant); await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct)) return null;
        using var document = JsonDocument.Parse(reader.GetString(1)); return (reader.GetInt64(0), document.RootElement.Clone());
    }

    private static async Task<(long Version, JsonElement Payload)?> ReadCurrentRulebookLockedAsync(
        PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT s.config_version,s.payload::text FROM public.platform_audit_rule_snapshots s
            JOIN public.platform_audit_rule_state st ON st.tenant_id=s.tenant_id AND st.config_version=s.config_version
            WHERE s.tenant_id=$1 AND s.schema_version=2 AND s.evaluator_version=1
            FOR SHARE OF s,st
            """);
        command.Parameters.AddWithValue(tenant); await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct)) return null;
        using var document = JsonDocument.Parse(reader.GetString(1)); return (reader.GetInt64(0), document.RootElement.Clone());
    }

    private static async Task<List<(string Stream, string Code)>> ReadRoutingAsync(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        var result = new List<(string, string)>();
        await using var command = db.CreateCommand("""
            SELECT account_code,verification_workstream FROM public.account_control_assignments
            WHERE client_id=$1 AND verification_workstream=ANY($2) ORDER BY account_code
            """);
        command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(Workstreams);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) result.Add((reader.GetString(1), reader.GetString(0)));
        return result;
    }

    private static async Task<Dictionary<string, string>> ReadLatestAccountNamesAsync(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        await using var command = db.CreateCommand("""
            SELECT a.account_code,COALESCE(a.account_name,'') FROM public.trial_balance_accounts a
            JOIN public.trial_balance_uploads u ON u.id=a.upload_id
            WHERE u.client_id=$1 AND u.status='parsed' AND u.id=(
                SELECT id FROM public.trial_balance_uploads WHERE client_id=$1 AND status='parsed'
                ORDER BY period_end DESC NULLS LAST,uploaded_at DESC,id DESC LIMIT 1)
            """);
        command.Parameters.AddWithValue(client); await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) result[reader.GetString(0)] = reader.GetString(1);
        return result;
    }

    private string? ResolveSelectedAccount(string label, string[] codes, Dictionary<string, string> names)
    {
        string clean = label.Trim();
        return codes.FirstOrDefault(code => string.Equals(code, clean, StringComparison.Ordinal)
            || string.Equals(names.GetValueOrDefault(code), clean, StringComparison.Ordinal)
            || string.Equals($"{code} :: {names.GetValueOrDefault(code, code)}", clean, StringComparison.Ordinal)
            || string.Equals($"{code} - {names.GetValueOrDefault(code)}", clean, StringComparison.Ordinal)
            || string.Equals($"{code} — {names.GetValueOrDefault(code)}", clean, StringComparison.Ordinal));
    }

    private async Task<List<Source>?> ReadSourceAsync(PostgresTransactionContext db, int client, DateOnly start, DateOnly end,
        CancellationToken outer, CancellationToken ct)
    {
        var result = new List<Source>();
        await using var command = db.CreateCommand("""
            SELECT id,row_data::text,extract_date::text,extract_amount::text FROM public.client_data
            WHERE client_id=$1 AND is_deleted=FALSE AND change_detection_status!='deleted'
              AND extract_date BETWEEN $2 AND $3 ORDER BY extract_date,id LIMIT $4
            """);
        command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(start); command.Parameters.AddWithValue(end);
        command.Parameters.AddWithValue(options.Value.MaximumSourceRows + 1);
        await using var reader = await command.ExecuteReaderAsync(ct);
        long capturedBytes = 0; int scannedRows = 0;
        while (await reader.ReadAsync(ct))
        {
            outer.ThrowIfCancellationRequested(); if (++scannedRows > options.Value.MaximumSourceRows) return null;
            string jsonText = reader.GetString(1);
            capturedBytes = checked(capturedBytes + System.Text.Encoding.UTF8.GetByteCount(jsonText));
            if (capturedBytes > options.Value.MaximumSourceBytes) return null;
            JsonDocument json; try { json = JsonDocument.Parse(jsonText); } catch (JsonException) { continue; }
            using (json)
            {
                var row = json.RootElement; if (row.ValueKind != JsonValueKind.Object) continue;
                string? voucher = Alias(row, VoucherAliases); string? account = Alias(row, AccountAliases);
                if (string.IsNullOrWhiteSpace(voucher) || string.IsNullOrWhiteSpace(account)) continue;
                decimal debit = Number(Alias(row, DebitAliases)); decimal credit = Number(Alias(row, CreditAliases));
                string? dateText = reader.IsDBNull(2) ? null : reader.GetString(2);
                DateOnly? date = DateOnly.TryParse(dateText, CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsed) ? parsed : null;
                decimal? amount = decimal.TryParse(reader.IsDBNull(3) ? null : reader.GetString(3), NumberStyles.Float, CultureInfo.InvariantCulture, out var extracted) ? extracted : null;
                result.Add(new(reader.GetInt32(0), voucher, account,
                    Alias(row, ["Account Detail", "Account Details", "Account Code", "Account No", "Account Number", "Code", "Ledger Code", "AccountID", "Account Head", "Ledger Detail"]),
                    debit, credit, date, amount, row.Clone()));
            }
        }
        return result;
    }

    private static List<Source> ResolveLines(List<Source> rows, string type, string selectedCode, string selectedName,
        HashSet<string> treasuryCodes)
    {
        var vouchers = rows.GroupBy(r => r.Voucher, StringComparer.Ordinal).ToArray(); var result = new List<Source>();
        foreach (var voucher in vouchers)
        {
            bool nonTreasury = type is not ("cash" or "bank");
            if (nonTreasury)
            {
                if (voucher.Any(r => r.AccountCode is not null && treasuryCodes.Contains(r.AccountCode))) continue;
                result.AddRange(voucher.Where(r => r.AccountCode == selectedCode && (r.Debit > 0 || r.Credit > 0)));
                continue;
            }
            var selected = voucher.FirstOrDefault(r => r.AccountCode == selectedCode && r.AccountName == selectedName);
            if (selected is null) continue;
            bool payment = selected.Credit > 0;
            bool receipt = !payment && selected.Debit > 0;
            if (payment) result.AddRange(voucher.Where(r => r.Debit > 0 && r.Credit == 0));
            if (receipt) result.AddRange(voucher.Where(r => r.Credit > 0 && r.Debit == 0));
        }
        return result;
    }

    private sealed record PreviewRow(decimal? Amount, string? Direction, decimal? TreasuryAmount,
        decimal? CounterpartTotal, decimal? Imbalance, bool? IsBalanced);

    private static async Task<object?> ReadSessionHeaderAsync(PostgresTransactionContext db, int sessionId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,client_id,cashbook_name,period_start::text,period_end::text,total_transactions,
                   verified_count,status,created_by,created_at::text
            FROM public.verification_sessions WHERE id=$1 AND status!='Cancelled'
            """);
        command.Parameters.AddWithValue(sessionId); await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct)) return null;
        return new { id = reader.GetInt32(0), client_id = reader.GetInt32(1), cashbook_name = reader.GetString(2),
            period_start = NullableString(reader, 3), period_end = NullableString(reader, 4), total_transactions = reader.GetInt32(5),
            verified_count = reader.GetInt32(6), status = reader.GetString(7), created_by = NullableString(reader, 8), created_at = NullableString(reader, 9) };
    }

    private static async Task<List<SessionDetail>> ReadSessionDetailsAsync(PostgresTransactionContext db, int tenantId,
        int sessionId, bool activeOnly, CancellationToken ct)
    {
        var details = new List<SessionDetail>();
        await using (var command = db.CreateCommand("""
            SELECT vd.id,vd.client_data_id,vd.transaction_ref,vd.line_items::text,vd.procedure_data::text,
                   vd.validation_status,vd.validation_message,vd.status,vd.comments,vd.observations,vd.verified_by,
                   vd.verified_at::text,vd.cleared_status,vd.matrix_version_applied,vd.category_id_applied,
                   vd.category_name_applied,cd.row_data::text,cd.extract_amount::text,cd.extract_date::text,
                   cd.template_id,cd.change_detection_status,COALESCE(cd.version,1),
                   (SELECT dal.old_row_data::text FROM public.data_audit_log dal
                    WHERE dal.client_data_id=cd.id AND dal.action='UPDATE' ORDER BY dal.id DESC LIMIT 1)
            FROM public.verification_details vd
            JOIN public.client_data cd ON cd.id=vd.client_data_id
            JOIN public.verification_sessions v ON v.id=vd.session_id AND v.client_id=cd.client_id
            WHERE vd.session_id=$1 AND ($2::boolean IS FALSE OR vd.range_status='active')
            ORDER BY cd.id,vd.id
            """))
        {
            command.Parameters.AddWithValue(sessionId); command.Parameters.AddWithValue(activeOnly);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                details.Add(new(reader.GetInt32(0), reader.GetInt32(1), NullableString(reader, 2), NullableString(reader, 3),
                    NullableString(reader, 4), NullableString(reader, 5), NullableString(reader, 6), NullableString(reader, 7),
                    NullableString(reader, 8), NullableString(reader, 9), NullableString(reader, 10), NullableString(reader, 11),
                    NullableString(reader, 12), NullableInt(reader, 13), NullableInt(reader, 14), NullableString(reader, 15),
                    reader.GetString(16), NullableString(reader, 17), NullableString(reader, 18), reader.GetInt32(19),
                    NullableString(reader, 20), reader.GetInt32(21), NullableString(reader, 22)));
            }
        }
        if (details.Count == 0) return details;
        var templates = details.Select(d => d.TemplateId).Distinct().ToArray();
        var columns = await ReadDocumentColumnsAsync(db, tenantId, templates, ct);
        return details.Select(d => d with { GroupId = DocumentGroupId(d, columns.GetValueOrDefault(d.TemplateId, [])) }).ToList();
    }

    private static async Task<Dictionary<int, List<DocumentColumn>>> ReadDocumentColumnsAsync(PostgresTransactionContext db,
        int tenantId, int[] templateIds, CancellationToken ct)
    {
        var all = new Dictionary<int, List<DocumentColumn>>();
        await using var command = db.CreateCommand("""
            SELECT p.template_id,COALESCE(p.storage_key,m.column_name),p.reconciliation_role,m.data_type,p.sort_order,
                   COALESCE(p.tenant_id=$2,FALSE) AS tenant_owned
            FROM public.template_column_mappings p
            JOIN public.master_columns m ON m.id=p.column_id
            WHERE p.template_id=ANY($1) AND (p.tenant_id=$2 OR p.tenant_id IS NULL)
            ORDER BY p.template_id,p.sort_order
            """);
        command.Parameters.AddWithValue(templateIds); command.Parameters.AddWithValue(tenantId);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            int template = reader.GetInt32(0);
            if (!all.TryGetValue(template, out var rows)) all[template] = rows = [];
            rows.Add(new(reader.GetString(1), NullableString(reader, 2), reader.GetString(3), reader.GetInt32(4), reader.GetBoolean(5)));
        }
        foreach (int template in all.Keys.ToArray())
        {
            var rows = all[template];
            if (rows.Any(c => c.TenantOwned)) all[template] = rows.Where(c => c.TenantOwned).OrderBy(c => c.Order).ToList();
            else all[template] = rows.Where(c => !c.TenantOwned).OrderBy(c => c.Order).ToList();
        }
        return all;
    }

    private static string DocumentGroupId(SessionDetail detail, IReadOnlyList<DocumentColumn> columns)
    {
        JsonDocument? document = null;
        try
        {
            document = JsonDocument.Parse(detail.RowData);
            var row = document.RootElement;
            var documentColumn = columns.FirstOrDefault(c => c.Role == "document_id") ?? columns.FirstOrDefault();
            if (documentColumn is null && row.ValueKind == JsonValueKind.Object)
            {
                var first = row.EnumerateObject().FirstOrDefault();
                if (first.Name is not null) return DocumentPrefix(detail.TemplateId) + PythonScalar(first.Value).Trim();
            }
            if (documentColumn is not null && row.ValueKind == JsonValueKind.Object
                && row.TryGetProperty(documentColumn.Key, out var value) && value.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined))
            {
                string text = PythonScalar(value);
                if (text.Length > 0) return DocumentPrefix(detail.TemplateId) + text.Trim();
            }
            string representation = document is null ? detail.RowData : PythonRepr(document.RootElement);
            string hash = Convert.ToHexString(System.Security.Cryptography.MD5.HashData(System.Text.Encoding.UTF8.GetBytes(representation))).ToLowerInvariant()[..8];
            return DocumentPrefix(detail.TemplateId) + hash;
        }
        catch (JsonException)
        {
            string hash = Convert.ToHexString(System.Security.Cryptography.MD5.HashData(System.Text.Encoding.UTF8.GetBytes(detail.RowData))).ToLowerInvariant()[..8];
            return DocumentPrefix(detail.TemplateId) + hash;
        }
        finally { document?.Dispose(); }
    }

    private static string DocumentPrefix(int templateId) => templateId.ToString(CultureInfo.InvariantCulture) + "-";
    private static string PythonScalar(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => value.GetString() ?? "",
        JsonValueKind.True => "True", JsonValueKind.False => "False", JsonValueKind.Null => "None",
        _ => value.ToString()
    };
    private static string PythonRepr(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Object => "{" + string.Join(", ", value.EnumerateObject().Select(p => PythonReprString(p.Name) + ": " + PythonRepr(p.Value))) + "}",
        JsonValueKind.Array => "[" + string.Join(", ", value.EnumerateArray().Select(PythonRepr)) + "]",
        JsonValueKind.String => PythonReprString(value.GetString() ?? ""),
        JsonValueKind.True => "True", JsonValueKind.False => "False", JsonValueKind.Null => "None",
        _ => value.GetRawText()
    };
    private static string PythonReprString(string value)
    {
        char quote = value.Contains('\'') && !value.Contains('"') ? '"' : '\'';
        var result = new System.Text.StringBuilder().Append(quote);
        foreach (char c in value)
        {
            if (c == '\\' || c == quote) result.Append('\\').Append(c);
            else if (c == '\n') result.Append("\\n"); else if (c == '\r') result.Append("\\r");
            else if (c == '\t') result.Append("\\t");
            else if (char.IsControl(c)) result.Append("\\x").Append(((int)c).ToString("x2", CultureInfo.InvariantCulture));
            else result.Append(c);
        }
        return result.Append(quote).ToString();
    }

    private static object BuildVerificationLine(SessionDetail detail)
    {
        var procedure = ParseJsonValue(detail.ProcedureData) ?? JsonSerializer.SerializeToElement(new Dictionary<string, object?>());
        string transactionType = procedure.ValueKind == JsonValueKind.Object && procedure.TryGetProperty("transaction_type", out var type)
            ? PythonScalar(type) : "";
        return new
        {
            detail_id = detail.Id, client_data_id = detail.ClientDataId, voucher_id = detail.TransactionRef,
            row_data = ParseJsonValue(detail.RowData), line_items = ParseJsonValue(detail.LineItems) ?? JsonSerializer.SerializeToElement(Array.Empty<object>()),
            procedure_data = procedure, validation_status = detail.ValidationStatus, validation_message = detail.ValidationMessage,
            transaction_type = transactionType, change_detection_status = detail.ChangeStatus,
            old_row_data = ParseJsonValue(detail.OldRowData), version = detail.Version,
            observations = detail.Observations, cleared_status = detail.ClearedStatus, matrix_version_applied = detail.MatrixVersion,
            category_id_applied = detail.CategoryId, category_name_applied = detail.CategoryName
        };
    }

    private static JsonElement FlattenProcedureData(string? json)
    {
        var procedure = ParseJsonValue(json);
        var result = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["purchase_demand"] = false, ["purchase_order"] = false, ["gate_pass"] = false,
            ["store_receiving"] = false, ["vendor_bill"] = false, ["observation"] = "", ["comments"] = ""
        };
        if (procedure is { ValueKind: JsonValueKind.Object } value)
            foreach (string key in result.Keys.ToArray())
                if (value.TryGetProperty(key, out var field)) result[key] = field.Clone();
        return JsonSerializer.SerializeToElement(result);
    }

    private static JsonElement? ParseJsonValue(string? json)
    {
        if (string.IsNullOrWhiteSpace(json)) return null;
        try { using var parsed = JsonDocument.Parse(json); return parsed.RootElement.Clone(); }
        catch (JsonException) { return null; }
    }

    private static PreviewRow PreviewPresentation(Source line, Source[] voucher, string type, string code, string name)
    {
        if (type is not ("cash" or "bank"))
        {
            if (line.Debit > 0)
            {
                var (direction, sign) = type switch
                {
                    "local_vendor" => ("Debit note / adjustment", -1),
                    "import_vendor" => ("Import vendor debit note / adjustment", -1),
                    "export_customer" => ("Export Customer invoice / receivable", 1),
                    "payroll" => ("Payroll charge / salary expense", 1),
                    _ => ("Contractor cost / payable", 1)
                };
                return new(line.Debit * sign, direction, null, null, null, null);
            }
            var (creditDirection, creditSign) = type switch
            {
                "local_vendor" => ("Vendor bill / payable", 1),
                "import_vendor" => ("Import vendor bill / payable", 1),
                "export_customer" => ("Export Customer credit note / adjustment", -1),
                "payroll" => ("Payroll reversal / adjustment", -1),
                _ => ("Contractor credit note / adjustment", -1)
            };
            return new(line.Credit * creditSign, creditDirection, null, null, null, null);
        }
        var treasury = voucher.FirstOrDefault(x => x.AccountCode == code && x.AccountName == name);
        if (treasury is null) return new(line.Amount, null, null, null, null, null);
        bool payment = treasury.Credit > 0;
        var counterpartLines = voucher.Where(x => payment ? x.Debit > 0 && x.Credit == 0 : x.Credit > 0 && x.Debit == 0).ToArray();
        decimal counterpartTotal = counterpartLines.Sum(x => payment ? x.Debit : x.Credit);
        decimal treasuryAmount = payment ? treasury.Credit : treasury.Debit;
        return new((payment ? -1m : 1m) * (payment ? line.Debit : line.Credit), payment ? "Payment" : "Receipt",
            treasuryAmount, counterpartTotal, treasuryAmount - counterpartTotal, Math.Abs(treasuryAmount - counterpartTotal) <= 0.01m);
    }

    private static async Task SeedPriorStatusesAsync(PostgresTransactionContext db, int sessionId, IReadOnlyCollection<int>? onlyClientDataIds, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            UPDATE public.verification_details current SET
                cleared_status=(SELECT prior.cleared_status FROM public.verification_details prior
                    WHERE prior.client_data_id=current.client_data_id AND prior.session_id<>$1 AND prior.cleared_status IS NOT NULL
                    ORDER BY prior.id DESC LIMIT 1),
                matrix_version_applied=(SELECT prior.matrix_version_applied FROM public.verification_details prior
                    WHERE prior.client_data_id=current.client_data_id AND prior.session_id<>$1 AND prior.cleared_status IS NOT NULL
                    ORDER BY prior.id DESC LIMIT 1)
            WHERE current.session_id=$1 AND ($2::integer[] IS NULL OR current.client_data_id=ANY($2))
              AND EXISTS(SELECT 1 FROM public.verification_details prior
                WHERE prior.client_data_id=current.client_data_id AND prior.session_id<>$1 AND prior.cleared_status IS NOT NULL)
            """);
        command.Parameters.AddWithValue(sessionId);
        command.Parameters.AddWithValue(NpgsqlDbType.Array | NpgsqlDbType.Integer,
            onlyClientDataIds is null ? DBNull.Value : onlyClientDataIds.ToArray());
        await command.ExecuteNonQueryAsync(ct);
    }

    private Task AuditAsync(PostgresTransactionContext db, Actor actor, int client, string action, object detail, CancellationToken ct) =>
        audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(),
            new LogActor("authenticated", actor.UserId, actor.Username), client, action, "Verification",
            JsonSerializer.SerializeToElement(detail)), ct);

    private async Task<T> Run<T>(Func<CancellationToken, Task<T>> work, CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct); timeout.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        return await work(timeout.Token);
    }

    private static VerificationRuntimeResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value));
    private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code);
    private static string? Alias(JsonElement row, IEnumerable<string> aliases)
    {
        foreach (var key in aliases)
            if (row.TryGetProperty(key, out var value) && value.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined))
            { string text = value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString(); if (text.Length > 0) return text; }
        return null;
    }
    private static decimal Number(string? value) => decimal.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var amount) ? amount : 0m;
    private static async Task InsertDetailsAsync(PostgresTransactionContext db, int sessionId, IReadOnlyList<Source> rows, CancellationToken ct)
    {
        if (rows.Count == 0) return;
        await using var command = db.CreateCommand("""
            INSERT INTO public.verification_details(session_id,client_data_id,transaction_ref,status,validation_status,range_status)
            SELECT $1,x.client_data_id,x.transaction_ref,'Pending','Pending','active'
            FROM unnest($2::integer[],$3::text[]) AS x(client_data_id,transaction_ref)
            """);
        command.Parameters.AddWithValue(sessionId);
        command.Parameters.AddWithValue(rows.Select(row => row.Id).ToArray());
        command.Parameters.AddWithValue(rows.Select(row => row.Voucher).ToArray());
        await command.ExecuteNonQueryAsync(ct);
    }
    private static bool TryPeriod(string first, string second, out DateOnly start, out DateOnly end)
    {
        bool validStart = DateOnly.TryParseExact(first, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out start);
        bool validEnd = DateOnly.TryParseExact(second, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out end);
        return validStart && validEnd && start <= end;
    }
    private static int? NullableInt(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetInt32(index);
    private static string? NullableString(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetString(index);
}
