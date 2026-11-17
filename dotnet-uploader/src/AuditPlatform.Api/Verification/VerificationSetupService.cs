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

namespace AuditPlatform.Api.Verification;

public sealed class VerificationSetupService(PostgresTransactions transactions, PostgresAuditWriter audit,
    IOptions<VerificationSetupOptions> options)
{
    private static readonly HashSet<string> Workstreams = new(StringComparer.Ordinal)
        { "cash", "bank", "local_vendor", "import_vendor", "export_customer", "payroll", "contractor" };
    private static readonly HashSet<string> SelectorTypes = new(StringComparer.Ordinal)
        { "include_subtree", "include_exact", "exclude_subtree", "exclude_exact" };

    private sealed record AccountRow(int Id, string Code, string? Name, int Level, string Classification,
        bool Manual, int Sort, string? Parent, bool IsLeaf);
    private sealed record Selector(string Type, string Code, int Sort);
    private sealed record AccountSet(int Id, int Client, string Key, int? ActiveVersion, bool Active, DateTime Created, DateTime Updated);
    private sealed record AccountSetVersion(int Id, int SetId, int Number, int Upload, string Name,
        string? Description, string Status, DateTime Created, DateTime? Published, int Client, string Key, int? ActiveVersion);

    public Task<VerificationSetupResult> LatestAccountsAsync(VerifiedSession? session, int client, CancellationToken ct = default) =>
        Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await TrialBalanceStorageStore.Authorize(db, session, client, false, inner);
            var upload = await LatestUploadAsync(db, client, inner);
            if (upload is null) return new("not_found", Code: "tb_upload_not_found");
            var accounts = await AccountsAsync(db, upload.Value.Id, inner);
            return Success(new { client_id = client, upload = upload.Value.Json, accounts = accounts.Select(AccountJson).ToArray() });
        }, token), ct);

    public Task<VerificationSetupResult> SaveAssignmentAsync(VerifiedSession? session, WorkstreamAssignmentRequest request,
        CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.ClientId < 1 || request.TrialBalanceUploadId is < 1) return Invalid("invalid_assignment");
        string? code = CleanAccountCode(request.AccountCode);
        string? workstream = CleanWorkstream(request.VerificationWorkstream, out bool valid);
        if (!valid || code is null) return Invalid("invalid_assignment");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await TrialBalanceStorageStore.Authorize(db, session, request.ClientId, true, inner);
            await LockConfigurationAsync(db, inner);
            var upload = request.TrialBalanceUploadId is {} requested
                ? await UploadAsync(db, request.ClientId, requested, inner)
                : await LatestUploadAsync(db, request.ClientId, inner);
            if (upload is null) return new("invalid", Code: "tb_upload_not_found");
            var accounts = await AccountsAsync(db, upload.Value.Id, inner);
            var selected = accounts.SingleOrDefault(row => row.Code == code);
            if (selected is null) return Invalid("account_code_not_found");
            if (!selected.IsLeaf) return Invalid("account_leaf_required");
            await UpsertAssignmentsAsync(db, request.ClientId, [code], workstream, actor.Name, inner);
            await AuditAsync(db, actor, request.ClientId, "VERIFICATION_WORKSTREAM_ASSIGNMENT", new
            { account_code = code, verification_workstream = workstream, trial_balance_upload_id = upload.Value.Id }, inner);
            return Success(new { saved = true, account_code = code, assignment = new { verification_workstream = workstream } });
        }, token);
    }, ct);

    public Task<VerificationSetupResult> ApplyToChildrenAsync(VerifiedSession? session, WorkstreamBulkAssignmentRequest request,
        CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.ClientId < 1 || request.TrialBalanceUploadId is < 1) return Invalid("invalid_assignment");
        string? parentCode = CleanAccountCode(request.ParentAccountCode);
        string? workstream = CleanWorkstream(request.VerificationWorkstream, out bool valid);
        if (!valid || parentCode is null) return Invalid("invalid_assignment");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await TrialBalanceStorageStore.Authorize(db, session, request.ClientId, true, inner);
            await LockConfigurationAsync(db, inner);
            var upload = request.TrialBalanceUploadId is {} requested
                ? await UploadAsync(db, request.ClientId, requested, inner)
                : await LatestUploadAsync(db, request.ClientId, inner);
            if (upload is null) return new("invalid", Code: "tb_upload_not_found");
            var accounts = await AccountsAsync(db, upload.Value.Id, inner);
            var parent = accounts.SingleOrDefault(row => row.Code == parentCode);
            if (parent is null) return Invalid("parent_account_not_found");
            var leaves = DescendantLeaves(accounts, parentCode);
            if (leaves.Count == 0) return Invalid("account_leaves_not_found");
            string[] codes = leaves.Select(row => row.Code).ToArray();
            await UpsertAssignmentsAsync(db, request.ClientId, codes, workstream, actor.Name, inner);
            await AuditAsync(db, actor, request.ClientId, "VERIFICATION_WORKSTREAM_BULK_ASSIGNMENT", new
            { parent_account_code = parentCode, verification_workstream = workstream, leaf_count = codes.Length, trial_balance_upload_id = upload.Value.Id }, inner);
            return Success(new { saved = true, parent_account_code = parentCode, leaf_count = codes.Length });
        }, token);
    }, ct);

    public Task<VerificationSetupResult> ListSetsAsync(VerifiedSession? session, int client, CancellationToken ct = default) =>
        Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
        {
            await TrialBalanceStorageStore.Authorize(db, session, client, false, inner);
            await using var command = db.CreateCommand("""
                SELECT s.id,s.client_id,s.set_key,s.active_version_id,s.is_active,s.created_at,s.updated_at,
                       v.display_name,v.status,v.version_number
                FROM public.account_sets s LEFT JOIN public.account_set_versions v ON v.id=s.active_version_id
                WHERE s.client_id=$1 AND s.is_active IS TRUE
                ORDER BY lower(COALESCE(v.display_name,s.set_key)),s.id LIMIT 257
                """);
            command.Parameters.AddWithValue(client);
            var sets = new List<object>();
            await using var reader = await command.ExecuteReaderAsync(inner);
            while (await reader.ReadAsync(inner))
            {
                if (sets.Count >= 256) return new VerificationSetupResult("too_large", Code: "account_set_capacity");
                sets.Add(new { id = reader.GetInt32(0), set_key = reader.GetString(2),
                    active_version_id = NullableInt(reader, 3), display_name = NullableString(reader, 7),
                    active_version_status = NullableString(reader, 8), version_number = NullableInt(reader, 9),
                    updated_at = reader.GetDateTime(6) });
            }
            return Success(new { client_id = client, account_sets = sets });
        }, token), ct);

    public Task<VerificationSetupResult> GetSetAsync(VerifiedSession? session, int setId, CancellationToken ct = default) =>
        Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
        {
            if (setId < 1) return Invalid("account_set_not_found");
            int? client = await SetClientAsync(db, setId, inner);
            if (client is null) return new("not_found", Code: "account_set_not_found");
            var actor = await AuthorizePathAsync(db, session, client.Value, false, inner);
            if (actor.Result is not null) return actor.Result;
            var set = await ReadSetAsync(db, setId, false, inner);
            if (set is null) return new("not_found", Code: "account_set_not_found");
            return Success(await SerialiseSetAsync(db, set, inner));
        }, token), ct, obscureScope: true);

    public Task<VerificationSetupResult> CreateSetAsync(VerifiedSession? session, AccountSetCreateRequest request,
        CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.ClientId < 1) return Invalid("invalid_account_set");
        var identity = CleanIdentity(request.SetKey, request.DisplayName, request.Description);
        if (identity is null) return Invalid("invalid_account_set");
        var selectors = CleanSelectors(request.Selectors ?? [], out bool valid);
        if (!valid) return Invalid("invalid_account_set_selectors");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var actor = await TrialBalanceStorageStore.Authorize(db, session, request.ClientId, true, inner);
            await LockConfigurationAsync(db, inner);
            var upload = await LatestUploadAsync(db, request.ClientId, inner);
            if (upload is null) return new("conflict", Code: "tb_upload_required");
            var selectorCheck = await ValidateSelectorsAsync(db, upload.Value.Id, selectors, false, inner);
            if (!selectorCheck.Valid) return Invalid("account_set_selector_invalid");
            int setId;
            await using (var insert = db.CreateCommand("INSERT INTO public.account_sets(client_id,set_key) VALUES($1,$2) RETURNING id"))
            { insert.Parameters.AddWithValue(request.ClientId); insert.Parameters.AddWithValue(identity.Value.Key); setId = (int)(await insert.ExecuteScalarAsync(inner))!; }
            int versionId;
            await using (var insert = db.CreateCommand("""
                INSERT INTO public.account_set_versions(account_set_id,version_number,source_upload_id,display_name,description,status)
                VALUES($1,1,$2,$3,$4,'draft') RETURNING id
                """))
            {
                insert.Parameters.AddWithValue(setId); insert.Parameters.AddWithValue(upload.Value.Id);
                insert.Parameters.AddWithValue(identity.Value.Name); insert.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)identity.Value.Description ?? DBNull.Value);
                versionId = (int)(await insert.ExecuteScalarAsync(inner))!;
            }
            await ReplaceSelectorsAsync(db, versionId, selectors, inner);
            await AuditAsync(db, actor, request.ClientId, "ACCOUNT_SET_DRAFT_CREATED", new { account_set_id = setId, version_id = versionId, set_key = identity.Value.Key }, inner);
            var set = await ReadSetAsync(db, setId, false, inner);
            return set is null ? new("unavailable", Code: "account_set_readback_failed") : Success(await SerialiseSetAsync(db, set, inner));
        }, token);
    }, ct);

    public Task<VerificationSetupResult> UpdateDraftAsync(VerifiedSession? session, int versionId,
        AccountSetDraftRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || versionId < 1) return Invalid("invalid_account_set_draft");
        var name = CleanText(request.DisplayName, 120, required: true);
        var description = CleanText(request.Description, 500, required: false);
        var selectors = CleanSelectors(request.Selectors ?? [], out bool valid);
        if (name is null || request.Description is { Length: > 500 } || !valid) return Invalid("invalid_account_set_draft");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            int? client = await VersionClientAsync(db, versionId, inner);
            if (client is null) return new("not_found", Code: "account_set_version_not_found");
            var auth = await AuthorizePathAsync(db, session, client.Value, true, inner);
            if (auth.Result is not null) return auth.Result;
            await LockConfigurationAsync(db, inner);
            var version = await ReadVersionAsync(db, versionId, true, inner);
            if (version is null) return new("not_found", Code: "account_set_version_not_found");
            if (version.Status != "draft") return new("conflict", Code: "account_set_published_immutable");
            int uploadId = request.SourceUploadId ?? version.Upload;
            if (uploadId < 1) return Invalid("tb_upload_not_found");
            if (await UploadAsync(db, version.Client, uploadId, inner) is null) return Invalid("tb_upload_not_found");
            var selectorCheck = await ValidateSelectorsAsync(db, uploadId, selectors, false, inner);
            if (!selectorCheck.Valid) return Invalid("account_set_selector_invalid");
            await using (var update = db.CreateCommand("UPDATE public.account_set_versions SET source_upload_id=$1,display_name=$2,description=$3 WHERE id=$4 AND status='draft'"))
            {
                update.Parameters.AddWithValue(uploadId); update.Parameters.AddWithValue(name);
                update.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)description ?? DBNull.Value); update.Parameters.AddWithValue(versionId);
                if (await update.ExecuteNonQueryAsync(inner) != 1) return new("conflict", Code: "account_set_changed");
            }
            await ReplaceSelectorsAsync(db, versionId, selectors, inner);
            await using (var update = db.CreateCommand("UPDATE public.account_sets SET updated_at=now() WHERE id=$1"))
            { update.Parameters.AddWithValue(version.SetId); await update.ExecuteNonQueryAsync(inner); }
            await AuditAsync(db, auth.Actor!, version.Client, "ACCOUNT_SET_DRAFT_UPDATED", new { account_set_id = version.SetId, version_id = versionId, source_upload_id = uploadId }, inner);
            var set = await ReadSetAsync(db, version.SetId, false, inner);
            return set is null ? new("unavailable", Code: "account_set_readback_failed") : Success(await SerialiseSetAsync(db, set, inner));
        }, token);
    }, ct);

    public Task<VerificationSetupResult> PreviewAsync(VerifiedSession? session, int versionId, int? uploadId,
        CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        if (versionId < 1 || uploadId is < 1) return Invalid("invalid_account_set_preview");
        int? client = await VersionClientAsync(db, versionId, inner);
        if (client is null) return new("not_found", Code: "account_set_version_not_found");
        var auth = await AuthorizePathAsync(db, session, client.Value, false, inner);
        if (auth.Result is not null) return auth.Result;
        var version = await ReadVersionAsync(db, versionId, false, inner);
        if (version is null) return new("not_found", Code: "account_set_version_not_found");
        int targetUpload = uploadId ?? version.Upload;
        var upload = await UploadAsync(db, version.Client, targetUpload, inner);
        if (upload is null) return Invalid("tb_upload_not_found");
        var selectors = await ReadSelectorsAsync(db, version.Id, inner);
        var selectorCheck = await ValidateSelectorsAsync(db, targetUpload, selectors, false, inner);
        if (!selectorCheck.Valid) return Invalid("account_set_selector_invalid");
        return Success(new { account_set_id = version.SetId, version_id = version.Id, version_status = version.Status,
            upload = upload.Value.Json, resolved_leaf_accounts = selectorCheck.Resolved, resolved_leaf_count = selectorCheck.Resolved.Count });
    }, token), ct, obscureScope: true);

    public Task<VerificationSetupResult> PublishAsync(VerifiedSession? session, int versionId, CancellationToken ct = default) =>
        Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
        {
            if (versionId < 1) return Invalid("account_set_version_not_found");
            int? client = await VersionClientAsync(db, versionId, inner);
            if (client is null) return new("not_found", Code: "account_set_version_not_found");
            var auth = await AuthorizePathAsync(db, session, client.Value, true, inner);
            if (auth.Result is not null) return auth.Result;
            await LockConfigurationAsync(db, inner);
            var version = await ReadVersionAsync(db, versionId, true, inner);
            if (version is null) return new("not_found", Code: "account_set_version_not_found");
            if (version.Status != "draft") return new("conflict", Code: "account_set_draft_required");
            var selectors = await ReadSelectorsAsync(db, version.Id, inner);
            var selectorCheck = await ValidateSelectorsAsync(db, version.Upload, selectors, true, inner);
            if (!selectorCheck.Valid) return Invalid("account_set_selector_invalid");
            await using (var supersede = db.CreateCommand("UPDATE public.account_set_versions SET status='superseded' WHERE account_set_id=$1 AND status='published'"))
            { supersede.Parameters.AddWithValue(version.SetId); await supersede.ExecuteNonQueryAsync(inner); }
            await using (var publish = db.CreateCommand("UPDATE public.account_set_versions SET status='published',published_at=now() WHERE id=$1 AND status='draft'"))
            { publish.Parameters.AddWithValue(versionId); if (await publish.ExecuteNonQueryAsync(inner) != 1) return new("conflict", Code: "account_set_changed"); }
            await using (var update = db.CreateCommand("UPDATE public.account_sets SET active_version_id=$1,updated_at=now() WHERE id=$2 AND is_active IS TRUE"))
            { update.Parameters.AddWithValue(versionId); update.Parameters.AddWithValue(version.SetId); if (await update.ExecuteNonQueryAsync(inner) != 1) return new("not_found", Code: "account_set_not_found"); }
            await AuditAsync(db, auth.Actor!, version.Client, "ACCOUNT_SET_VERSION_PUBLISHED", new { account_set_id = version.SetId, version_id = versionId }, inner);
            var set = await ReadSetAsync(db, version.SetId, false, inner);
            return set is null ? new("unavailable", Code: "account_set_readback_failed") : Success(await SerialiseSetAsync(db, set, inner));
        }, token), ct, obscureScope: true);

    public Task<VerificationSetupResult> CreateRevisionDraftAsync(VerifiedSession? session, int setId,
        CancellationToken ct = default) => Run(async token => await transactions.ExecuteAsync(async (db, inner) =>
    {
        if (setId < 1) return Invalid("account_set_not_found");
        int? client = await SetClientAsync(db, setId, inner);
        if (client is null) return new("not_found", Code: "account_set_not_found");
        var auth = await AuthorizePathAsync(db, session, client.Value, true, inner);
        if (auth.Result is not null) return auth.Result;
        await LockConfigurationAsync(db, inner);
        var set = await ReadSetAsync(db, setId, true, inner);
        if (set is null) return new("not_found", Code: "account_set_not_found");
        if (set.ActiveVersion is null) return new("conflict", Code: "account_set_publish_first");
        await using (var existing = db.CreateCommand("SELECT id FROM public.account_set_versions WHERE account_set_id=$1 AND status='draft' LIMIT 1"))
        { existing.Parameters.AddWithValue(setId); if (await existing.ExecuteScalarAsync(inner) is not null) return new("conflict", Code: "account_set_draft_exists"); }
        var active = await ReadVersionAsync(db, set.ActiveVersion.Value, false, inner);
        if (active is null) return new("review", Code: "account_set_active_version_invalid");
        var upload = await LatestUploadAsync(db, set.Client, inner);
        if (upload is null) return new("conflict", Code: "tb_upload_required");
        var selectors = await ReadSelectorsAsync(db, active.Id, inner);
        int next;
        await using (var number = db.CreateCommand("SELECT COALESCE(MAX(version_number),0)+1 FROM public.account_set_versions WHERE account_set_id=$1"))
        { number.Parameters.AddWithValue(setId); next = Convert.ToInt32(await number.ExecuteScalarAsync(inner)); }
        int versionId;
        await using (var insert = db.CreateCommand("""
            INSERT INTO public.account_set_versions(account_set_id,version_number,source_upload_id,display_name,description,status)
            VALUES($1,$2,$3,$4,$5,'draft') RETURNING id
            """))
        {
            insert.Parameters.AddWithValue(setId); insert.Parameters.AddWithValue(next); insert.Parameters.AddWithValue(upload.Value.Id);
            insert.Parameters.AddWithValue(active.Name); insert.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)active.Description ?? DBNull.Value);
            versionId = (int)(await insert.ExecuteScalarAsync(inner))!;
        }
        await ReplaceSelectorsAsync(db, versionId, selectors, inner);
        await using (var update = db.CreateCommand("UPDATE public.account_sets SET updated_at=now() WHERE id=$1"))
        { update.Parameters.AddWithValue(setId); await update.ExecuteNonQueryAsync(inner); }
        await AuditAsync(db, auth.Actor!, set.Client, "ACCOUNT_SET_REVISION_DRAFT_CREATED", new { account_set_id = setId, version_id = versionId, source_upload_id = upload.Value.Id }, inner);
        var refreshed = await ReadSetAsync(db, setId, false, inner);
        return refreshed is null ? new("unavailable", Code: "account_set_readback_failed") : Success(await SerialiseSetAsync(db, refreshed, inner));
    }, token), ct, obscureScope: true);

    private async Task<(TrialBalanceStorageActor? Actor, VerificationSetupResult? Result)> AuthorizePathAsync(
        PostgresTransactionContext db, VerifiedSession? session, int client, bool writing, CancellationToken ct)
    {
        try { return (await TrialBalanceStorageStore.Authorize(db, session, client, writing, ct), null); }
        catch (TrialBalanceStorageRejected error)
        {
            var result = RejectedResult(error);
            return (null, result.Status == "forbidden"
                ? new VerificationSetupResult("not_found", Code: "account_set_not_found")
                : result);
        }
    }

    private Task<VerificationSetupResult> Run(Func<CancellationToken, Task<VerificationSetupResult>> operation,
        CancellationToken ct, bool obscureScope = false)
    {
        if (!options.Value.Enabled) return Task.FromResult(new VerificationSetupResult("disabled", Code: "verification_setup_disabled"));
        if (!options.Value.Valid()) return Task.FromResult(new VerificationSetupResult("unavailable", Code: "verification_setup_configuration"));
        return ExecuteAsync();

        async Task<VerificationSetupResult> ExecuteAsync()
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
            deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
            try { return await operation(deadline.Token); }
            catch (TrialBalanceStorageRejected error)
            {
                if (obscureScope && error.Status == "forbidden") return new("not_found", Code: "account_set_not_found");
                return RejectedResult(error);
            }
            catch (PostgresException error) when (error.SqlState == PostgresErrorCodes.UniqueViolation)
            { return new("conflict", Code: "verification_setup_duplicate"); }
            catch (PostgresException error) when (error.SqlState is "40001" or "40P01" or "55P03")
            { return new("conflict", Code: "verification_setup_concurrent_change"); }
            catch (PostgresException error) when (error.SqlState is "23503" or "23514" or "42P01" or "42703" or "42501")
            { return new("review", Code: "verification_setup_schema_or_dependency"); }
            catch (OperationCanceledException)
            { return new(ct.IsCancellationRequested ? "cancelled" : "timeout", Code: "verification_setup_timeout"); }
            catch (Exception error) when (error is NpgsqlException or TimeoutException or JsonException)
            { return new("unavailable", Code: "verification_setup_unavailable"); }
        }
    }

    private static VerificationSetupResult RejectedResult(TrialBalanceStorageRejected error) => new(error.Status, Code: error.Code);
    private static VerificationSetupResult Invalid(string code) => new("invalid", Code: code);
    private static VerificationSetupResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value));

    private async Task<AccountSet?> ReadSetAsync(PostgresTransactionContext db, int id, bool forUpdate, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,client_id,set_key,active_version_id,is_active,created_at,updated_at
            FROM public.account_sets WHERE id=$1 AND is_active IS TRUE
            """ + (forUpdate ? " FOR UPDATE" : ""));
        command.Parameters.AddWithValue(id);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? new(reader.GetInt32(0), reader.GetInt32(1), reader.GetString(2),
            NullableInt(reader, 3), reader.GetBoolean(4), reader.GetDateTime(5), reader.GetDateTime(6)) : null;
    }

    private async Task<AccountSetVersion?> ReadVersionAsync(PostgresTransactionContext db, int id, bool forUpdate, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT v.id,v.account_set_id,v.version_number,v.source_upload_id,v.display_name,v.description,v.status,
                   v.created_at,v.published_at,s.client_id,s.set_key,s.active_version_id
            FROM public.account_set_versions v JOIN public.account_sets s ON s.id=v.account_set_id
            WHERE v.id=$1 AND s.is_active IS TRUE
            """ + (forUpdate ? " FOR UPDATE OF v,s" : ""));
        command.Parameters.AddWithValue(id);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? new(reader.GetInt32(0), reader.GetInt32(1), reader.GetInt32(2), reader.GetInt32(3),
            reader.GetString(4), NullableString(reader, 5), reader.GetString(6), reader.GetDateTime(7),
            reader.IsDBNull(8) ? null : reader.GetDateTime(8), reader.GetInt32(9), reader.GetString(10), NullableInt(reader, 11)) : null;
    }

    private async Task<int?> SetClientAsync(PostgresTransactionContext db, int setId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT client_id FROM public.account_sets WHERE id=$1 AND is_active IS TRUE");
        command.Parameters.AddWithValue(setId); var value = await command.ExecuteScalarAsync(ct); return value is int client ? client : null;
    }

    private async Task<int?> VersionClientAsync(PostgresTransactionContext db, int versionId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT s.client_id FROM public.account_set_versions v JOIN public.account_sets s ON s.id=v.account_set_id
            WHERE v.id=$1 AND s.is_active IS TRUE
            """);
        command.Parameters.AddWithValue(versionId); var value = await command.ExecuteScalarAsync(ct); return value is int client ? client : null;
    }

    private async Task<JsonElement> SerialiseSetAsync(PostgresTransactionContext db, AccountSet set, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,account_set_id,version_number,source_upload_id,display_name,description,status,created_at,published_at
            FROM public.account_set_versions WHERE account_set_id=$1 ORDER BY version_number DESC LIMIT 257
            """);
        command.Parameters.AddWithValue(set.Id);
        var storedVersions = new List<(int Id, int SetId, int Number, int Upload, string Name, string? Description,
            string Status, DateTime Created, DateTime? Published)>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            if (storedVersions.Count >= 256) throw new TrialBalanceStorageRejected("too_large", "account_set_version_capacity");
            storedVersions.Add((reader.GetInt32(0), reader.GetInt32(1), reader.GetInt32(2), reader.GetInt32(3),
                reader.GetString(4), NullableString(reader, 5), reader.GetString(6), reader.GetDateTime(7),
                reader.IsDBNull(8) ? null : reader.GetDateTime(8)));
        }
        await reader.DisposeAsync();
        var versions = new List<object>(storedVersions.Count);
        foreach (var version in storedVersions)
        {
            var selectors = await ReadSelectorsAsync(db, version.Id, ct);
            versions.Add(new { id = version.Id, account_set_id = version.SetId, version_number = version.Number,
                source_upload_id = version.Upload, display_name = version.Name, description = version.Description,
                status = version.Status, created_at = version.Created, published_at = version.Published,
                selectors = selectors.Select(SelectorJson).ToArray() });
        }
        return JsonSerializer.SerializeToElement(new { id = set.Id, client_id = set.Client, set_key = set.Key,
            active_version_id = set.ActiveVersion, is_active = set.Active, created_at = set.Created, updated_at = set.Updated, versions });
    }

    private async Task<List<Selector>> ReadSelectorsAsync(PostgresTransactionContext db, int versionId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT selector_type,account_code,sort_order FROM public.account_set_selectors
            WHERE account_set_version_id=$1 ORDER BY sort_order,id LIMIT $2
            """);
        command.Parameters.AddWithValue(versionId); command.Parameters.AddWithValue(options.Value.MaximumSelectors + 1);
        var result = new List<Selector>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            if (result.Count >= options.Value.MaximumSelectors) throw new TrialBalanceStorageRejected("too_large", "account_set_selector_capacity");
            result.Add(new(reader.GetString(0), reader.GetString(1), reader.GetInt32(2)));
        }
        return result;
    }

    private async Task<(int Id, JsonElement Json)?> LatestUploadAsync(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id FROM public.trial_balance_uploads WHERE client_id=$1 AND status='parsed'
            ORDER BY period_end DESC NULLS LAST,uploaded_at DESC,id DESC LIMIT 1
            """);
        command.Parameters.AddWithValue(client); var value = await command.ExecuteScalarAsync(ct);
        return value is int id ? await UploadAsync(db, client, id, ct) : null;
    }

    private static async Task<(int Id, JsonElement Json)?> UploadAsync(PostgresTransactionContext db, int client, int upload, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,client_id,as_of_date::text,file_name,status,row_count,notes,uploaded_at,
                   period_start::text,period_end::text,period_type
            FROM public.trial_balance_uploads WHERE id=$1 AND client_id=$2 AND status='parsed'
            """);
        command.Parameters.AddWithValue(upload); command.Parameters.AddWithValue(client);
        await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct)) return null;
        var json = JsonSerializer.SerializeToElement(new { id = reader.GetInt32(0), client_id = reader.GetInt32(1),
            as_of_date = NullableString(reader, 2), file_name = NullableString(reader, 3), status = reader.GetString(4),
            row_count = reader.IsDBNull(5) ? 0 : reader.GetInt32(5), notes = NullableString(reader, 6),
            uploaded_at = reader.IsDBNull(7) ? (DateTime?)null : reader.GetDateTime(7), period_start = NullableString(reader, 8),
            period_end = NullableString(reader, 9), period_type = NullableString(reader, 10) });
        return (reader.GetInt32(0), json);
    }

    private async Task<List<AccountRow>> AccountsAsync(PostgresTransactionContext db, int upload, CancellationToken ct)
    {
        var rows = new List<(int Id, string Code, string? Name, string? StoredParent, int Level, string Classification, bool Manual, int Sort)>();
        await using (var command = db.CreateCommand("""
            SELECT id,account_code,account_name,parent_account_code,level,classification,manually_overridden,sort_order
            FROM public.trial_balance_accounts WHERE upload_id=$1 ORDER BY sort_order,account_code,id LIMIT $2
            """))
        {
            command.Parameters.AddWithValue(upload); command.Parameters.AddWithValue(options.Value.MaximumAccounts + 1);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                if (rows.Count >= options.Value.MaximumAccounts) throw new TrialBalanceStorageRejected("too_large", "tb_account_capacity");
                rows.Add((reader.GetInt32(0), reader.GetString(1), NullableString(reader, 2), NullableString(reader, 3),
                    reader.GetInt32(4), reader.GetString(5), !reader.IsDBNull(6) && reader.GetBoolean(6), reader.GetInt32(7)));
            }
        }
        var hierarchy = TrialBalanceValidation.Hierarchy(rows.Select(row => row.Code), ct);
        return rows.Select(row =>
        {
            var tree = hierarchy[row.Code];
            bool leaf = (row.Manual ? row.Classification : tree.Classification) == "leaf";
            return new AccountRow(row.Id, row.Code, row.Name, row.Level, row.Classification, row.Manual, row.Sort, tree.Parent, leaf);
        }).ToList();
    }

    private sealed record SelectorValidation(bool Valid, List<object> Resolved);

    private async Task<SelectorValidation> ValidateSelectorsAsync(PostgresTransactionContext db, int upload, IReadOnlyList<Selector> selectors,
        bool requireResolved, CancellationToken ct)
    {
        var accounts = await AccountsAsync(db, upload, ct);
        var byCode = accounts.ToDictionary(row => row.Code, StringComparer.Ordinal);
        var missing = selectors.Select(selector => selector.Code).Where(code => !byCode.ContainsKey(code)).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        if (missing.Length > 0) return new(false, []);
        var invalidExact = selectors.Where(selector => selector.Type.EndsWith("exact", StringComparison.Ordinal)
                && !byCode[selector.Code].IsLeaf).Select(selector => selector.Code).Distinct(StringComparer.Ordinal).ToArray();
        if (invalidExact.Length > 0) return new(false, []);
        var includes = selectors.Where(selector => selector.Type.StartsWith("include_", StringComparison.Ordinal)).ToArray();
        var excludes = selectors.Where(selector => selector.Type.StartsWith("exclude_", StringComparison.Ordinal)).ToArray();
        var resolved = new List<object>();
        foreach (var account in accounts.Where(row => row.IsLeaf))
        {
            ct.ThrowIfCancellationRequested();
            var matched = includes.Where(selector => Matches(account.Code, selector)).ToArray();
            if (matched.Length == 0 || excludes.Any(selector => Matches(account.Code, selector))) continue;
            resolved.Add(new { id = account.Id, account_code = account.Code, account_name = account.Name,
                parent_account_code = account.Parent, level = account.Level,
                matched_by = matched.Select(selector => new { selector_type = selector.Type, account_code = selector.Code }).ToArray() });
        }
        return new(!requireResolved || resolved.Count > 0, resolved);
    }

    private static bool Matches(string code, Selector selector) => selector.Type.EndsWith("exact", StringComparison.Ordinal)
        ? code == selector.Code : code == selector.Code || code.StartsWith(selector.Code + "-", StringComparison.Ordinal);

    private static List<AccountRow> DescendantLeaves(IReadOnlyList<AccountRow> accounts, string parentCode)
    {
        var byCode = accounts.ToDictionary(row => row.Code, StringComparer.Ordinal);
        var result = new List<AccountRow>();
        foreach (var row in accounts)
        {
            if (!row.IsLeaf) continue;
            if (row.Code == parentCode) { result.Add(row); continue; }
            string? ancestor = row.Parent;
            var visited = new HashSet<string>(StringComparer.Ordinal);
            while (ancestor is not null && visited.Add(ancestor))
            {
                if (ancestor == parentCode) { result.Add(row); break; }
                ancestor = byCode.GetValueOrDefault(ancestor)?.Parent;
            }
        }
        return result;
    }

    private async Task UpsertAssignmentsAsync(PostgresTransactionContext db, int client, string[] codes,
        string? workstream, string actor, CancellationToken ct)
    {
        if (codes.Length == 0) return;
        await using var command = db.CreateCommand("""
            INSERT INTO public.account_control_assignments(client_id,account_code,verification_workstream,updated_by)
            SELECT $1,code,$3,$4 FROM unnest($2::text[]) AS code
            ON CONFLICT(client_id,account_code) DO UPDATE SET
                verification_workstream=EXCLUDED.verification_workstream,updated_by=EXCLUDED.updated_by,updated_at=now()
            """);
        command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(codes);
        command.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)workstream ?? DBNull.Value); command.Parameters.AddWithValue(actor);
        await command.ExecuteNonQueryAsync(ct);
    }

    private async Task ReplaceSelectorsAsync(PostgresTransactionContext db, int version, IReadOnlyList<Selector> selectors, CancellationToken ct)
    {
        await using (var delete = db.CreateCommand("DELETE FROM public.account_set_selectors WHERE account_set_version_id=$1"))
        { delete.Parameters.AddWithValue(version); await delete.ExecuteNonQueryAsync(ct); }
        if (selectors.Count == 0) return;
        string json = JsonSerializer.Serialize(selectors.Select(selector => new { selector_type = selector.Type, account_code = selector.Code, sort_order = selector.Sort }));
        await using var insert = db.CreateCommand("""
            INSERT INTO public.account_set_selectors(account_set_version_id,selector_type,account_code,sort_order)
            SELECT $1,s.selector_type,s.account_code,s.sort_order
            FROM jsonb_to_recordset($2) AS s(selector_type text,account_code text,sort_order integer)
            """);
        insert.Parameters.AddWithValue(version); insert.Parameters.AddWithValue(NpgsqlDbType.Jsonb, json);
        await insert.ExecuteNonQueryAsync(ct);
    }

    private async Task LockConfigurationAsync(PostgresTransactionContext db, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            LOCK TABLE public.account_control_assignments,public.account_set_selectors,public.account_set_versions,
                public.account_sets,public.trial_balance_accounts,public.trial_balance_uploads
            IN SHARE ROW EXCLUSIVE MODE NOWAIT
            """);
        await command.ExecuteNonQueryAsync(ct);
    }

    private Task<int> AuditAsync(PostgresTransactionContext db, TrialBalanceStorageActor actor, int client, string action,
        object detail, CancellationToken ct) => audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(),
            new LogActor("authenticated", actor.User, actor.Name), client, action, "VerificationSetup",
            JsonSerializer.SerializeToElement(detail)), ct);

    private static JsonElement AccountJson(AccountRow row) => JsonSerializer.SerializeToElement(new
    { id = row.Id, account_code = row.Code, account_name = row.Name, parent_account_code = row.Parent, level = row.Level, is_leaf = row.IsLeaf });
    private static JsonElement SelectorJson(Selector selector) => JsonSerializer.SerializeToElement(new
    { selector_type = selector.Type, account_code = selector.Code, sort_order = selector.Sort });
    private static int? NullableInt(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetInt32(index);
    private static string? NullableString(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetString(index);

    private List<Selector> CleanSelectors(IReadOnlyList<AccountSetSelectorRequest> input, out bool valid)
    {
        valid = input.Count <= options.Value.MaximumSelectors;
        var result = new List<Selector>(Math.Min(input.Count, options.Value.MaximumSelectors));
        var seen = new HashSet<(string Type, string Code)>();
        for (int i = 0; valid && i < input.Count; i++)
        {
            var item = input[i];
            if (item is null) { valid = false; break; }
            string type = (item.SelectorType ?? "").Trim().ToLowerInvariant(); string code = CleanAccountCode(item.AccountCode) ?? "";
            int order = item.SortOrder ?? i;
            if (!SelectorTypes.Contains(type) || code.Length == 0 || code.Length > 120 || order < 0 || !seen.Add((type, code)))
            { valid = false; break; }
            result.Add(new(type, code, order));
        }
        return result;
    }

    private static (string Key, string Name, string? Description)? CleanIdentity(string? key, string? name, string? description)
    {
        string normalized = Regex.Replace((key ?? "").Trim().ToLowerInvariant(), "[^a-z0-9]+", "_").Trim('_');
        if (description is { Length: > 500 }) return null;
        string? cleanName = CleanText(name, 120, true); string? cleanDescription = CleanText(description, 500, false);
        return normalized.Length is > 0 and <= 80 && cleanName is not null
            ? (normalized, cleanName, cleanDescription) : null;
    }

    private static string? CleanText(string? value, int maximum, bool required)
    {
        string cleaned = Regex.Replace((value ?? "").Trim(), @"\s+", " ");
        if (required && cleaned.Length == 0 || cleaned.Length > maximum) return null;
        return cleaned.Length == 0 ? null : cleaned;
    }

    private static string? CleanAccountCode(string? value)
    {
        string cleaned = (value ?? "").Trim(); return cleaned.Length is > 0 and <= 120 ? cleaned : null;
    }

    private static string? CleanWorkstream(string? value, out bool valid)
    {
        string cleaned = (value ?? "").Trim().ToLowerInvariant();
        if (cleaned.Length == 0) { valid = true; return null; }
        valid = Workstreams.Contains(cleaned); return valid ? cleaned : null;
    }
}
