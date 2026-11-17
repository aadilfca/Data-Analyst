using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.TrialBalance;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Reconciliation;

public sealed class ReconciliationSetupService(PostgresTransactions transactions, PostgresAuditWriter audit,
    IOptions<ReconciliationSetupOptions> options)
{
    private sealed record Account(int Id, string Code, string? Name, string? Type, string Classification, int Sort);
    private sealed record CategorySet(int Id, int Client, string Purpose, string Name, bool Active);
    private sealed record CleanCategory(string Key, string Label, string? Parent, int Sort);
    private sealed record CleanAssignment(string Code, string CategoryKey);
    private sealed record CleanBalance(string Code, string Month, double Opening, double Closing);

    public Task<ReconciliationSetupResult> ListGroupsAsync(VerifiedSession? session, int client,
        CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        await TrialBalanceStorageStore.Authorize(db, session, client, false, inner);
        await using var command = db.CreateCommand("""
            SELECT group_label,account_code,updated_at FROM public.account_groups
            WHERE client_id=$1 AND is_active IS TRUE ORDER BY lower(group_label),account_code
            """);
        command.Parameters.AddWithValue(client);
        var groups = new Dictionary<string, (DateTime? Updated, List<string> Codes)>(StringComparer.Ordinal);
        int rowCount = 0;
        await using var reader = await command.ExecuteReaderAsync(inner);
        while (await reader.ReadAsync(inner))
        {
            if (++rowCount > options.Value.MaximumAssignments)
                return new("too_large", Code: "reconciliation_group_capacity");
            string label = reader.GetString(0);
            if (!groups.TryGetValue(label, out var group)) group = (null, []);
            group.Codes.Add(reader.GetString(1));
            if (group.Updated is null && !reader.IsDBNull(2)) group.Updated = reader.GetDateTime(2);
            groups[label] = group;
        }
        return Success(new { client_id = client, groups = groups.OrderBy(pair => pair.Key, StringComparer.OrdinalIgnoreCase)
            .Select(pair => new { group_label = pair.Key, account_codes = pair.Value.Codes, updated_at = pair.Value.Updated }).ToArray() });
    }, token), ct);

    public Task<ReconciliationSetupResult> AccountSuggestionsAsync(VerifiedSession? session, int client,
        string? groupLabel, CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        await TrialBalanceStorageStore.Authorize(db, session, client, false, inner);
        int? upload = await LatestUploadAsync(db, client, inner);
        if (upload is null) return new("not_found", Code: "trial_balance_not_found");
        var accounts = await AccountsAsync(db, upload.Value, inner);
        bool filterGroup = !string.IsNullOrEmpty(groupLabel);
        string? selectedGroup = filterGroup ? CleanLabel(groupLabel) ?? "" : null;
        var selected = await SavedCodesAsync(db, client, selectedGroup, inner);
        return Success(new { client_id = client,
            group_label = filterGroup ? CleanLabel(groupLabel) : null,
            accounts = accounts.Select(account => new
            {
                id = account.Id, account_code = account.Code, account_name = account.Name,
                account_type = account.Type, classification = account.Classification,
                is_leaf = account.Classification == "leaf", suggested_group = SuggestedGroup(account.Type),
                is_selected = selected.Contains(account.Code)
            }).ToArray() });
    }, token), ct);

    public Task<ReconciliationSetupResult> SaveGroupAsync(VerifiedSession? session, int client,
        AccountGroupUpdateRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || client < 1 || request.AccountCodes is null) return Invalid("invalid_account_group");
        string? label = CleanLabel(request.GroupLabel);
        if (label is null || label.Length > 100) return Invalid("invalid_account_group");
        var codes = CleanCodes(request.AccountCodes, options.Value.MaximumAccounts, out bool valid);
        if (!valid) return Invalid("invalid_account_group_codes");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await TrialBalanceStorageStore.Authorize(db, session, client, true, inner);
            await LockConfigurationAsync(db, inner);
            int? upload = await LatestUploadAsync(db, client, inner);
            if (upload is null) return new("conflict", Code: "trial_balance_required");
            var accounts = await AccountsAsync(db, upload.Value, inner);
            var known = accounts.Select(account => account.Code).ToHashSet(StringComparer.Ordinal);
            var unknown = codes.Where(code => !known.Contains(code)).ToArray();
            if (unknown.Length > 0) return InvalidWith("trial_balance_account_not_found", new
            { message = "One or more account codes are not present in the latest parsed Trial Balance", account_codes = unknown });
            label = await StoredGroupLabelAsync(db, client, label, inner);
            await using (var deactivate = db.CreateCommand("""
                UPDATE public.account_groups SET is_active=FALSE,updated_at=now()
                WHERE client_id=$1 AND lower(group_label)=lower($2) AND is_active IS TRUE
                """))
            { deactivate.Parameters.AddWithValue(client); deactivate.Parameters.AddWithValue(label); await deactivate.ExecuteNonQueryAsync(inner); }
            if (codes.Count > 0)
            {
                string json = JsonSerializer.Serialize(codes.Select(code => new { account_code = code }));
                await using var insert = db.CreateCommand("""
                    INSERT INTO public.account_groups(client_id,group_label,account_code,is_active)
                    SELECT $1,$2,c.account_code,TRUE FROM jsonb_to_recordset($3) AS c(account_code text)
                    ON CONFLICT(client_id,group_label,account_code)
                    DO UPDATE SET is_active=TRUE,updated_at=now()
                    """);
                insert.Parameters.AddWithValue(client); insert.Parameters.AddWithValue(label);
                insert.Parameters.AddWithValue(NpgsqlDbType.Jsonb, json); await insert.ExecuteNonQueryAsync(inner);
            }
            await AuditAsync(db, actor, client, "RECONCILIATION_ACCOUNT_GROUP_REPLACED",
                new { group_label = label, account_count = codes.Count, trial_balance_upload_id = upload.Value }, inner);
            return Success(new { client_id = client, group_label = label, account_codes = codes, count = codes.Count });
        }, token);
    }, ct);

    public Task<ReconciliationSetupResult> ListBalancesAsync(VerifiedSession? session, int client,
        string? groupLabel, CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        await TrialBalanceStorageStore.Authorize(db, session, client, false, inner);
        bool filterGroup = !string.IsNullOrEmpty(groupLabel);
        string where = "client_id=$1";
        if (filterGroup) where += " AND lower(group_label)=lower($2)";
        await using var command = db.CreateCommand($"""
            SELECT group_label,account_code,period_month,opening_balance::double precision,
                   actual_closing_balance::double precision,updated_at
            FROM public.report_balance_inputs WHERE {where}
            ORDER BY lower(group_label),account_code,period_month LIMIT {(filterGroup ? "$3" : "$2")}
            """);
        command.Parameters.AddWithValue(client);
        if (filterGroup) command.Parameters.AddWithValue(CleanLabel(groupLabel) ?? "");
        command.Parameters.AddWithValue(options.Value.MaximumBalanceRows + 1);
        var balances = new List<object>();
        await using var reader = await command.ExecuteReaderAsync(inner);
        while (await reader.ReadAsync(inner))
        {
            if (balances.Count >= options.Value.MaximumBalanceRows) return new("too_large", Code: "reconciliation_balance_capacity");
            balances.Add(new { group_label = reader.GetString(0), account_code = reader.GetString(1),
                period_month = reader.GetString(2), opening_balance = reader.GetDouble(3),
                actual_closing_balance = reader.GetDouble(4), updated_at = NullableDateTime(reader, 5) });
        }
        return Success(new { client_id = client, balances });
    }, token), ct);

    public Task<ReconciliationSetupResult> ReplaceBalancesAsync(VerifiedSession? session, int client,
        ReconciliationBalanceInputsUpdateRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || client < 1) return Invalid("invalid_reconciliation_balances");
        string? requestedLabel = CleanLabel(request.GroupLabel);
        if (requestedLabel is null || requestedLabel.Length > 100) return Invalid("invalid_reconciliation_balances");
        if (request.Balances is null || request.Balances.Count > options.Value.MaximumBalanceRows)
            return Invalid("invalid_reconciliation_balances");
        var balances = CleanBalances(request.Balances, out bool valid);
        if (!valid) return Invalid("invalid_reconciliation_balances");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await TrialBalanceStorageStore.Authorize(db, session, client, true, inner);
            await LockConfigurationAsync(db, inner);
            int? upload = await LatestUploadAsync(db, client, inner);
            if (upload is null) return new("conflict", Code: "trial_balance_required");
            var accounts = await AccountsAsync(db, upload.Value, inner);
            var known = accounts.Select(account => account.Code).ToHashSet(StringComparer.Ordinal);
            var unknown = balances.Select(row => row.Code).Where(code => !known.Contains(code)).Distinct(StringComparer.Ordinal).ToArray();
            if (unknown.Length > 0) return InvalidWith("trial_balance_account_not_found", new
            { message = "One or more balance account codes are not present in the latest parsed Trial Balance", account_codes = unknown });
            var groupCodes = await SavedCodesAsync(db, client, requestedLabel, inner);
            if (groupCodes.Count == 0) return new("conflict", Code: "account_group_required");
            var outside = balances.Select(row => row.Code).Where(code => !groupCodes.Contains(code)).Distinct(StringComparer.Ordinal).ToArray();
            if (outside.Length > 0) return InvalidWith("account_not_in_selected_group", new
            { message = "One or more balance account codes are not members of the selected account group", account_codes = outside });
            string label = await StoredGroupLabelAsync(db, client, requestedLabel, inner);
            await using (var delete = db.CreateCommand("""
                DELETE FROM public.report_balance_inputs WHERE client_id=$1 AND lower(group_label)=lower($2)
                """))
            { delete.Parameters.AddWithValue(client); delete.Parameters.AddWithValue(label); await delete.ExecuteNonQueryAsync(inner); }
            foreach (var item in balances)
            {
                await using var insert = db.CreateCommand("""
                    INSERT INTO public.report_balance_inputs
                        (client_id,group_label,account_code,period_month,opening_balance,actual_closing_balance)
                    VALUES($1,$2,$3,$4,$5,$6)
                    """);
                insert.Parameters.AddWithValue(client); insert.Parameters.AddWithValue(label);
                insert.Parameters.AddWithValue(item.Code); insert.Parameters.AddWithValue(item.Month);
                insert.Parameters.AddWithValue(item.Opening); insert.Parameters.AddWithValue(item.Closing);
                await insert.ExecuteNonQueryAsync(inner);
            }
            await AuditAsync(db, actor, client, "RECONCILIATION_BALANCES_REPLACED",
                new { group_label = label, balance_count = balances.Count, trial_balance_upload_id = upload.Value }, inner);
            return Success(new { client_id = client, group_label = label, balances = balances.Select(BalanceJson).ToArray(), count = balances.Count });
        }, token);
    }, ct);

    public Task<ReconciliationSetupResult> ListCategorySetsAsync(VerifiedSession? session, int client,
        CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        await TrialBalanceStorageStore.Authorize(db, session, client, false, inner);
        await using var command = db.CreateCommand("""
            SELECT s.id,s.purpose_key,s.set_name,s.is_active,s.updated_at,
                   COUNT(DISTINCT c.id) FILTER(WHERE c.is_active IS TRUE) AS category_count,
                   COUNT(DISTINCT a.id) AS assignment_count
            FROM public.report_category_sets s
            LEFT JOIN public.report_categories c ON c.category_set_id=s.id
            LEFT JOIN public.report_account_category_assignments a ON a.category_set_id=s.id
            WHERE s.client_id=$1 AND s.is_active IS TRUE
            GROUP BY s.id ORDER BY s.purpose_key LIMIT $2
            """);
        command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(options.Value.MaximumCategories + 1);
        var sets = new List<object>();
        await using var reader = await command.ExecuteReaderAsync(inner);
        while (await reader.ReadAsync(inner))
        {
            if (sets.Count >= options.Value.MaximumCategories) return new("too_large", Code: "report_category_set_capacity");
            sets.Add(new { id = reader.GetInt32(0), purpose_key = reader.GetString(1), set_name = reader.GetString(2),
                is_active = reader.GetBoolean(3), updated_at = reader.GetDateTime(4), category_count = reader.GetInt64(5),
                assignment_count = reader.GetInt64(6) });
        }
        return Success(new { client_id = client, category_sets = sets });
    }, token), ct);

    public Task<ReconciliationSetupResult> CreateCategorySetAsync(VerifiedSession? session, int client,
        ReportCategorySetCreateRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || client < 1) return Invalid("invalid_report_category_set");
        string purpose = NormalizeKey(request.PurposeKey);
        string? name = CleanLabel(request.SetName);
        if (purpose.Length is 0 or > 80 || name is null || name.Length > 120) return Invalid("invalid_report_category_set");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await TrialBalanceStorageStore.Authorize(db, session, client, true, inner);
            await LockConfigurationAsync(db, inner);
            await using (var exists = db.CreateCommand("SELECT 1 FROM public.report_category_sets WHERE client_id=$1 AND purpose_key=$2 LIMIT 1"))
            { exists.Parameters.AddWithValue(client); exists.Parameters.AddWithValue(purpose); if (await exists.ExecuteScalarAsync(inner) is not null) return new("conflict", Code: "report_category_set_exists"); }
            int id;
            await using (var insert = db.CreateCommand("""
                INSERT INTO public.report_category_sets(client_id,purpose_key,set_name,is_active)
                VALUES($1,$2,$3,TRUE) RETURNING id
                """))
            { insert.Parameters.AddWithValue(client); insert.Parameters.AddWithValue(purpose); insert.Parameters.AddWithValue(name); id = (int)(await insert.ExecuteScalarAsync(inner))!; }
            await AuditAsync(db, actor, client, "REPORT_CATEGORY_SET_CREATED", new { report_category_set_id = id, purpose_key = purpose }, inner);
            return Success(new { id, client_id = client, purpose_key = purpose, set_name = name,
                is_active = true, category_count = 0, assignment_count = 0 });
        }, token);
    }, ct);

    public Task<ReconciliationSetupResult> ListCategoriesAsync(VerifiedSession? session, int setId,
        CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        if (setId < 1) return new("not_found", Code: "report_category_set_not_found");
        var set = await ReadCategorySetAsync(db, setId, false, inner);
        if (set is null) return new("not_found", Code: "report_category_set_not_found");
        var authorization = await AuthorizeSetAsync(db, session, set.Client, false, inner);
        if (authorization.Result is not null) return authorization.Result;
        var categories = await ReadCategoriesAsync(db, setId, inner);
        return Success(new { category_set = CategorySetJson(set), categories = categories.ToArray() });
    }, token), ct, obscureScope: true);

    public Task<ReconciliationSetupResult> ReplaceCategoriesAsync(VerifiedSession? session, int setId,
        ReportCategoryDefinitionsUpdateRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (setId < 1 || request?.Categories is null || request.Categories.Count > options.Value.MaximumCategories)
            return Invalid("invalid_report_categories");
        var categories = CleanCategories(request.Categories, options.Value.MaximumCategories, out bool valid, out var missingParents);
        if (missingParents.Length > 0) return InvalidWith("invalid_report_categories", new
        { message = "Parent category does not exist in this replacement", category_keys = missingParents });
        if (!valid) return Invalid("invalid_report_categories");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var initial = await ReadCategorySetAsync(db, setId, false, inner);
            if (initial is null) return new("not_found", Code: "report_category_set_not_found");
            var authorization = await AuthorizeSetAsync(db, session, initial.Client, true, inner);
            if (authorization.Result is not null) return authorization.Result;
            await LockConfigurationAsync(db, inner);
            var set = await ReadCategorySetAsync(db, setId, true, inner);
            if (set is null) return new("not_found", Code: "report_category_set_not_found");
            await using (var deactivate = db.CreateCommand("""
                UPDATE public.report_categories SET is_active=FALSE,updated_at=now()
                WHERE category_set_id=$1 AND is_active IS TRUE
                """))
            { deactivate.Parameters.AddWithValue(setId); await deactivate.ExecuteNonQueryAsync(inner); }
            foreach (var category in categories)
            {
                await using var upsert = db.CreateCommand("""
                    INSERT INTO public.report_categories(category_set_id,category_key,label,parent_category_key,sort_order,is_active)
                    VALUES($1,$2,$3,$4,$5,TRUE)
                    ON CONFLICT(category_set_id,category_key) DO UPDATE SET
                        label=EXCLUDED.label,parent_category_key=EXCLUDED.parent_category_key,
                        sort_order=EXCLUDED.sort_order,is_active=TRUE,updated_at=now()
                    """);
                upsert.Parameters.AddWithValue(setId); upsert.Parameters.AddWithValue(category.Key);
                upsert.Parameters.AddWithValue(category.Label); upsert.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)category.Parent ?? DBNull.Value);
                upsert.Parameters.AddWithValue(category.Sort); await upsert.ExecuteNonQueryAsync(inner);
            }
            await AuditAsync(db, authorization.Actor!, set.Client, "REPORT_CATEGORIES_REPLACED",
                new { report_category_set_id = setId, category_count = categories.Count, purpose_key = set.Purpose }, inner);
            return Success(new { category_set_id = setId, purpose_key = set.Purpose, categories = categories.Select(CategoryJson).ToArray(), count = categories.Count });
        }, token);
    }, ct, obscureScope: true);

    public Task<ReconciliationSetupResult> ListAssignmentsAsync(VerifiedSession? session, int setId,
        CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        if (setId < 1) return new("not_found", Code: "report_category_set_not_found");
        var set = await ReadCategorySetAsync(db, setId, false, inner);
        if (set is null) return new("not_found", Code: "report_category_set_not_found");
        var authorization = await AuthorizeSetAsync(db, session, set.Client, false, inner);
        if (authorization.Result is not null) return authorization.Result;
        await using var command = db.CreateCommand("""
            SELECT a.account_code,c.category_key,c.label,a.updated_at
            FROM public.report_account_category_assignments a
            JOIN public.report_categories c ON c.id=a.category_id
            WHERE a.category_set_id=$1 AND c.is_active IS TRUE ORDER BY a.account_code LIMIT $2
            """);
        command.Parameters.AddWithValue(setId); command.Parameters.AddWithValue(options.Value.MaximumAssignments + 1);
        var assignments = new List<object>();
        await using var reader = await command.ExecuteReaderAsync(inner);
        while (await reader.ReadAsync(inner))
        {
            if (assignments.Count >= options.Value.MaximumAssignments) return new("too_large", Code: "report_assignment_capacity");
            assignments.Add(new { account_code = reader.GetString(0), category_key = reader.GetString(1),
                category_label = reader.GetString(2), updated_at = reader.GetDateTime(3) });
        }
        return Success(new { category_set = CategorySetJson(set), assignments });
    }, token), ct, obscureScope: true);

    public Task<ReconciliationSetupResult> ReplaceAssignmentsAsync(VerifiedSession? session, int setId,
        ReportCategoryAssignmentsUpdateRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (setId < 1 || request?.Assignments is null || request.Assignments.Count > options.Value.MaximumAssignments)
            return Invalid("invalid_report_assignments");
        var assignments = CleanAssignments(request.Assignments, out bool valid);
        if (!valid) return Invalid("invalid_report_assignments");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var initial = await ReadCategorySetAsync(db, setId, false, inner);
            if (initial is null) return new("not_found", Code: "report_category_set_not_found");
            var authorization = await AuthorizeSetAsync(db, session, initial.Client, true, inner);
            if (authorization.Result is not null) return authorization.Result;
            await LockConfigurationAsync(db, inner);
            var set = await ReadCategorySetAsync(db, setId, true, inner);
            if (set is null) return new("not_found", Code: "report_category_set_not_found");
            int? upload = await LatestUploadAsync(db, set.Client, inner);
            if (upload is null) return new("conflict", Code: "trial_balance_required");
            var accounts = await AccountsAsync(db, upload.Value, inner);
            var known = accounts.Select(account => account.Code).ToHashSet(StringComparer.Ordinal);
            var unknown = assignments.Select(row => row.Code).Where(code => !known.Contains(code)).Distinct(StringComparer.Ordinal).ToArray();
            if (unknown.Length > 0) return InvalidWith("trial_balance_account_not_found", new
            { message = "One or more account codes are not present in the latest parsed Trial Balance", account_codes = unknown });
            var activeCategories = await ActiveCategoryIdsAsync(db, setId, inner);
            var missing = assignments.Select(row => row.CategoryKey).Where(key => !activeCategories.ContainsKey(key))
                .Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
            if (missing.Length > 0) return InvalidWith("report_category_not_active", new
            { message = "One or more category keys are not active in this category set", category_keys = missing });
            await using (var delete = db.CreateCommand("DELETE FROM public.report_account_category_assignments WHERE category_set_id=$1"))
            { delete.Parameters.AddWithValue(setId); await delete.ExecuteNonQueryAsync(inner); }
            foreach (var assignment in assignments)
            {
                await using var insert = db.CreateCommand("""
                    INSERT INTO public.report_account_category_assignments(client_id,category_set_id,account_code,category_id)
                    VALUES($1,$2,$3,$4)
                    """);
                insert.Parameters.AddWithValue(set.Client); insert.Parameters.AddWithValue(setId);
                insert.Parameters.AddWithValue(assignment.Code); insert.Parameters.AddWithValue(activeCategories[assignment.CategoryKey]);
                await insert.ExecuteNonQueryAsync(inner);
            }
            await AuditAsync(db, authorization.Actor!, set.Client, "REPORT_CATEGORY_ASSIGNMENTS_REPLACED",
                new { report_category_set_id = setId, assignment_count = assignments.Count, purpose_key = set.Purpose,
                    trial_balance_upload_id = upload.Value }, inner);
            return Success(new { category_set_id = setId, purpose_key = set.Purpose,
                assignments = assignments.Select(AssignmentJson).ToArray(), count = assignments.Count });
        }, token);
    }, ct, obscureScope: true);

    private async Task<(TrialBalanceStorageActor? Actor, ReconciliationSetupResult? Result)> AuthorizeSetAsync(
        PostgresTransactionContext db, VerifiedSession? session, int client, bool writing, CancellationToken ct)
    {
        try { return (await TrialBalanceStorageStore.Authorize(db, session, client, writing, ct), null); }
        catch (TrialBalanceStorageRejected error)
        {
            var rejected = Rejected(error);
            return (null, rejected.Status == "forbidden" ? new("not_found", Code: "report_category_set_not_found") : rejected);
        }
    }

    private Task<ReconciliationSetupResult> Run(Func<CancellationToken, Task<ReconciliationSetupResult>> operation,
        CancellationToken ct, bool obscureScope = false)
    {
        if (!options.Value.Enabled) return Task.FromResult(new ReconciliationSetupResult("disabled", Code: "reconciliation_setup_disabled"));
        if (!options.Value.Valid()) return Task.FromResult(new ReconciliationSetupResult("unavailable", Code: "reconciliation_setup_configuration"));
        return ExecuteAsync();

        async Task<ReconciliationSetupResult> ExecuteAsync()
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
            deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
            try { return await operation(deadline.Token); }
            catch (TrialBalanceStorageRejected error)
            {
                if (obscureScope && error.Status == "forbidden") return new("not_found", Code: "report_category_set_not_found");
                return Rejected(error);
            }
            catch (PostgresException error) when (error.SqlState == PostgresErrorCodes.UniqueViolation)
            { return new("conflict", Code: "reconciliation_setup_duplicate"); }
            catch (PostgresException error) when (error.SqlState is "40001" or "40P01" or "55P03")
            { return new("conflict", Code: "reconciliation_setup_concurrent_change"); }
            catch (PostgresException error) when (error.SqlState is "23503" or "23514" or "42P01" or "42703" or "42501")
            { return new("review", Code: "reconciliation_setup_schema_or_dependency"); }
            catch (PostgresException error) when (error.SqlState == "22003")
            { return new("invalid", Code: "reconciliation_amount_out_of_range"); }
            catch (OperationCanceledException)
            { return new(ct.IsCancellationRequested ? "cancelled" : "timeout", Code: "reconciliation_setup_timeout"); }
            catch (Exception error) when (error is NpgsqlException or TimeoutException or JsonException)
            { return new("unavailable", Code: "reconciliation_setup_unavailable"); }
        }
    }

    private async Task<CategorySet?> ReadCategorySetAsync(PostgresTransactionContext db, int id, bool forUpdate, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,client_id,purpose_key,set_name,is_active FROM public.report_category_sets
            WHERE id=$1 AND is_active IS TRUE
            """ + (forUpdate ? " FOR UPDATE" : ""));
        command.Parameters.AddWithValue(id);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? new(reader.GetInt32(0), reader.GetInt32(1), reader.GetString(2),
            reader.GetString(3), reader.GetBoolean(4)) : null;
    }

    private async Task<List<object>> ReadCategoriesAsync(PostgresTransactionContext db, int setId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,category_key,label,parent_category_key,sort_order,is_active,updated_at
            FROM public.report_categories WHERE category_set_id=$1
            ORDER BY sort_order,lower(label),category_key LIMIT $2
            """);
        command.Parameters.AddWithValue(setId); command.Parameters.AddWithValue(options.Value.MaximumCategories + 1);
        var categories = new List<object>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            if (categories.Count >= options.Value.MaximumCategories) throw new TrialBalanceStorageRejected("too_large", "report_category_capacity");
            categories.Add(new { id = reader.GetInt32(0), category_key = reader.GetString(1), label = reader.GetString(2),
                parent_category_key = NullableString(reader, 3), sort_order = reader.GetInt32(4),
                is_active = reader.GetBoolean(5), updated_at = NullableDateTime(reader, 6) });
        }
        return categories;
    }

    private async Task<Dictionary<string, int>> ActiveCategoryIdsAsync(PostgresTransactionContext db, int setId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,category_key FROM public.report_categories
            WHERE category_set_id=$1 AND is_active IS TRUE ORDER BY id LIMIT $2
            """);
        command.Parameters.AddWithValue(setId); command.Parameters.AddWithValue(options.Value.MaximumCategories + 1);
        var result = new Dictionary<string, int>(StringComparer.Ordinal);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            if (result.Count >= options.Value.MaximumCategories) throw new TrialBalanceStorageRejected("too_large", "report_category_capacity");
            result.Add(NormalizeKey(reader.GetString(1)), reader.GetInt32(0));
        }
        return result;
    }

    private async Task<int?> LatestUploadAsync(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id FROM public.trial_balance_uploads WHERE client_id=$1 AND status='parsed'
            ORDER BY period_end DESC NULLS LAST,uploaded_at DESC,id DESC LIMIT 1
            """);
        command.Parameters.AddWithValue(client); var value = await command.ExecuteScalarAsync(ct);
        return value is int id ? id : null;
    }

    private async Task<List<Account>> AccountsAsync(PostgresTransactionContext db, int upload, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,account_code,account_name,source_account_type,classification,sort_order
            FROM public.trial_balance_accounts WHERE upload_id=$1 ORDER BY sort_order LIMIT $2
            """);
        command.Parameters.AddWithValue(upload); command.Parameters.AddWithValue(options.Value.MaximumAccounts + 1);
        var accounts = new List<Account>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            if (accounts.Count >= options.Value.MaximumAccounts) throw new TrialBalanceStorageRejected("too_large", "trial_balance_account_capacity");
            accounts.Add(new(reader.GetInt32(0), reader.GetString(1), NullableString(reader, 2), NullableString(reader, 3),
                reader.GetString(4), reader.GetInt32(5)));
        }
        return accounts;
    }

    private async Task<HashSet<string>> SavedCodesAsync(PostgresTransactionContext db, int client, string? label, CancellationToken ct)
    {
        string sql = "SELECT account_code FROM public.account_groups WHERE client_id=$1 AND is_active IS TRUE";
        bool filterGroup = label is not null;
        if (filterGroup) sql += " AND lower(group_label)=lower($2)";
        await using var command = db.CreateCommand(sql + $" ORDER BY account_code LIMIT {(filterGroup ? "$3" : "$2")}");
        command.Parameters.AddWithValue(client);
        if (filterGroup) command.Parameters.AddWithValue(label!);
        command.Parameters.AddWithValue(options.Value.MaximumAccounts + 1);
        var codes = new HashSet<string>(StringComparer.Ordinal);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            if (codes.Count >= options.Value.MaximumAccounts) throw new TrialBalanceStorageRejected("too_large", "account_group_capacity");
            codes.Add(reader.GetString(0));
        }
        return codes;
    }

    private static async Task<string> StoredGroupLabelAsync(PostgresTransactionContext db, int client, string requested,
        CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT group_label FROM public.account_groups WHERE client_id=$1 AND lower(group_label)=lower($2)
            ORDER BY is_active DESC,id LIMIT 1
            """);
        command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(requested);
        return (string?)await command.ExecuteScalarAsync(ct) ?? requested;
    }

    private async Task LockConfigurationAsync(PostgresTransactionContext db, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            LOCK TABLE public.account_groups,public.report_account_category_assignments,public.report_balance_inputs,
                public.report_categories,public.report_category_sets,public.trial_balance_accounts,public.trial_balance_uploads
            IN SHARE ROW EXCLUSIVE MODE NOWAIT
            """);
        await command.ExecuteNonQueryAsync(ct);
    }

    private Task<int> AuditAsync(PostgresTransactionContext db, TrialBalanceStorageActor actor, int client,
        string action, object detail, CancellationToken ct) => audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(),
            new LogActor("authenticated", actor.User, actor.Name), client, action, "ReconciliationSetup",
            JsonSerializer.SerializeToElement(detail)), ct);

    private static List<string> CleanCodes(IReadOnlyList<string?> input, int maximum, out bool valid)
    {
        valid = input.Count <= maximum;
        var result = new List<string>(Math.Min(input.Count, maximum));
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (int i = 0; valid && i < input.Count; i++)
        {
            string code = (input[i] ?? "").Trim();
            if (code.Length is 0 or > 120 || !seen.Add(code)) { valid = false; break; }
            result.Add(code);
        }
        return result;
    }

    private List<CleanBalance> CleanBalances(IReadOnlyList<ReconciliationBalanceInputRequest?> input, out bool valid)
    {
        valid = true;
        var result = new List<CleanBalance>(input.Count);
        var seen = new HashSet<(string Code, string Month)>();
        foreach (var item in input)
        {
            if (item is null || item.OpeningBalance is not { } opening || item.ActualClosingBalance is not { } closing
                || !double.IsFinite(opening) || !double.IsFinite(closing)) { valid = false; break; }
            string code = (item.AccountCode ?? "").Trim();
            if (code.Length is 0 or > 120) { valid = false; break; }
            string? month = NormalizeMonth(item.PeriodMonth);
            if (month is null || !seen.Add((code, month))) { valid = false; break; }
            result.Add(new(code, month, opening, closing));
            if (result.Count > options.Value.MaximumBalanceRows) { valid = false; break; }
        }
        return result;
    }

    private static List<CleanCategory> CleanCategories(IReadOnlyList<ReportCategoryDefinitionRequest?> input,
        int maximum, out bool valid, out string[] missingParents)
    {
        valid = input.Count <= maximum;
        var result = new List<CleanCategory>(Math.Min(input.Count, maximum));
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in input)
        {
            if (!valid) break;
            if (item is null) { valid = false; break; }
            string key = NormalizeKey(item.CategoryKey), label = CleanLabel(item.Label) ?? "";
            string? parent = string.IsNullOrWhiteSpace(item.ParentCategoryKey) ? null : NormalizeKey(item.ParentCategoryKey);
            if (key.Length is 0 or > 80 || label.Length is 0 or > 120 || !seen.Add(key)
                || item.SortOrder < 0 || parent == key) { valid = false; break; }
            result.Add(new(key, label, parent, item.SortOrder));
        }
        var keys = result.Select(row => row.Key).ToHashSet(StringComparer.Ordinal);
        missingParents = valid ? result.Select(row => row.Parent).Where(parent => parent is not null && !keys.Contains(parent))
            .Select(parent => parent!).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray() : [];
        if (missingParents.Length > 0) valid = false;
        return result;
    }

    private static List<CleanAssignment> CleanAssignments(IReadOnlyList<ReportAccountCategoryAssignmentRequest?> input,
        out bool valid)
    {
        valid = true;
        var result = new List<CleanAssignment>(input.Count);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in input)
        {
            if (item is null) { valid = false; break; }
            string code = (item.AccountCode ?? "").Trim(), key = NormalizeKey(item.CategoryKey);
            if (code.Length is 0 or > 120 || key.Length is 0 || !seen.Add(code)) { valid = false; break; }
            result.Add(new(code, key));
        }
        return result;
    }

    private static string? CleanLabel(string? value)
    {
        string cleaned = Regex.Replace((value ?? "").Trim(), @"\s+", " ");
        return cleaned.Length == 0 ? null : cleaned;
    }

    private static string NormalizeKey(string? value) => Regex.Replace((value ?? "").Trim().ToLowerInvariant(),
        "[^a-z0-9]+", "_").Trim('_');

    private static string? NormalizeMonth(string? value)
    {
        string raw = (value ?? "").Trim();
        if (raw.Length == 7 && raw[4] == '-' && int.TryParse(raw[..4], NumberStyles.None, CultureInfo.InvariantCulture, out _)
            && int.TryParse(raw[5..], NumberStyles.None, CultureInfo.InvariantCulture, out int month) && month is >= 1 and <= 12)
            return raw;
        if (!Regex.IsMatch(raw, @"^\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?$"))
            return null;
        if (DateTimeOffset.TryParse(raw.Replace("Z", "+00:00", StringComparison.Ordinal), CultureInfo.InvariantCulture,
            DateTimeStyles.AllowWhiteSpaces | DateTimeStyles.AssumeUniversal, out var parsed))
            return parsed.ToString("yyyy-MM", CultureInfo.InvariantCulture);
        return null;
    }

    private static string? SuggestedGroup(string? value) => value?.Trim().ToLowerInvariant() switch
    { "bank" => "Bank", "cash" => "Cash", _ => null };

    private static object BalanceJson(CleanBalance item) => new
    { account_code = item.Code, period_month = item.Month, opening_balance = item.Opening, actual_closing_balance = item.Closing };
    private static object CategoryJson(CleanCategory item) => new
    { category_key = item.Key, label = item.Label, parent_category_key = item.Parent, sort_order = item.Sort };
    private static object AssignmentJson(CleanAssignment item) => new { account_code = item.Code, category_key = item.CategoryKey };
    private static object CategorySetJson(CategorySet set) => new
    { id = set.Id, client_id = set.Client, purpose_key = set.Purpose, set_name = set.Name, is_active = set.Active };
    private static ReconciliationSetupResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value));
    private static ReconciliationSetupResult Invalid(string code) => new("invalid", Code: code);
    private static ReconciliationSetupResult InvalidWith<T>(string code, T detail) => new("invalid", JsonSerializer.SerializeToElement(detail), code);
    private static ReconciliationSetupResult Rejected(TrialBalanceStorageRejected error) => new(error.Status, Code: error.Code);
    private static string? NullableString(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetString(index);
    private static DateTime? NullableDateTime(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetDateTime(index);
}
