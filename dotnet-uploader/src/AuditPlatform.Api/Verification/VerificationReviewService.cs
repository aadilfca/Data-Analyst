using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Verification;

/// <summary>Transactional line review and completion operations for an authorized Verification session.</summary>
public sealed class VerificationReviewService(PostgresTransactions transactions, VerificationSessionService sessions,
    VerificationStatusService status, PostgresAuditWriter audit, IOptions<VerificationRuntimeOptions> options)
{
    private sealed record ReviewLine(int Id, int ClientDataId, string? TransactionRef, string? Status,
        string? ValidationStatus, string RowData, string? ExtractAmount, string? LineItems);

    public Task<VerificationRuntimeResult> SaveLineItemsAsync(VerifiedSession? session,
        VerificationLineSaveRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.DetailId < 1 || !ValidLineItems(request.LineItems)) return Invalid("invalid_verification_line_items");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            int? sessionId = await FindSessionIdAsync(db, request.DetailId, inner);
            if (sessionId is null) return NotFound("verification_detail_not_found");
            var resolved = await sessions.ResolveReviewContextAsync(db, session, sessionId.Value, inner);
            if (resolved.Failure is not null) return resolved.Failure;
            var owner = resolved.Context!;
            var line = await ReadLineForUpdateAsync(db, owner, sessionId.Value, request.DetailId, inner);
            if (line is null) return NotFound("verification_detail_not_found");
            if (line.Status != "Pending") return Invalid("verification_line_not_pending");
            await SaveLineAsync(db, owner, sessionId.Value, line, request.LineItems, request.TransactionType, request.ProcedureData, inner);
            var results = await status.RecomputeAsync(db, owner, [line.Id], inner);
            await WriteAuditAsync(db, owner, "VERIFICATION_LINE_SAVED", new { session_id = sessionId, detail_id = line.Id }, inner);
            var response = new Dictionary<string, object?> { ["message"] = "Line items and procedure data saved successfully." };
            if (results.TryGetValue(line.Id, out var lineStatus)) AddStatus(response, lineStatus);
            return Success(response);
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SaveAndVerifyAsync(VerifiedSession? session,
        VerificationSaveAndVerifyRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.SessionId < 1 || request.DetailId < 1 || request.CurrentDetailId < 1
            || request.DetailId != request.CurrentDetailId || !ValidLineItems(request.LineItems))
            return Invalid("invalid_save_and_verify_request");
        var bundle = new VerificationBundleSaveRequest
        {
            SessionId = request.SessionId, CurrentDetailId = request.CurrentDetailId,
            Lines = [new VerificationBundleLineRequest { DetailId = request.DetailId, LineItems = request.LineItems,
                TransactionType = request.TransactionType, ProcedureData = request.ProcedureData }], Verify = true
        };
        return await SaveBundleCoreAsync(session, bundle, token, useStoredVoucherChecks: true);
    }, ct);

    public Task<VerificationRuntimeResult> SaveBundleAsync(VerifiedSession? session,
        VerificationBundleSaveRequest request, CancellationToken ct = default) => Run(token => SaveBundleCoreAsync(session, request, token), ct);

    public Task<VerificationRuntimeResult> VerifyVoucherAsync(VerifiedSession? session, int sessionId,
        int currentDetailId, CancellationToken ct = default) => Run(async token =>
    {
        if (sessionId < 1 || currentDetailId < 1) return Invalid("invalid_verification_voucher");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var resolved = await sessions.ResolveReviewContextAsync(db, session, sessionId, inner);
            if (resolved.Failure is not null) return resolved.Failure;
            var owner = resolved.Context!;
            var current = await ReadLineForUpdateAsync(db, owner, sessionId, currentDetailId, inner);
            if (current is null) return NotFound("verification_detail_not_found");
            var existingChecks = await ReadVoucherChecksAsync(db, owner.ClientId, current.TransactionRef, owner.Rulebook, inner);
            var checkResult = await SaveVoucherChecksAsync(db, owner, current.TransactionRef, existingChecks, verify: true, inner, persist: false);
            if (checkResult.Failure is not null) return checkResult.Failure;
            if (!string.IsNullOrWhiteSpace(current.TransactionRef))
                await FlagVoucherChecksAsync(db, owner, current.TransactionRef, checkResult.NotDoneIds, checkResult.DoneIds, inner);
            await WriteAuditAsync(db, owner, "VERIFICATION_VOUCHER_VERIFIED", new { session_id = sessionId, detail_id = currentDetailId }, inner);
            return await NavigationResultAsync(db, owner, sessionId, currentDetailId, new Dictionary<int, VerificationLineStatusResult>(),
                checkResult.NotDoneIds.Count, inner);
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> ValidateAsync(VerifiedSession? session,
        VerificationValidateRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.DetailId < 1) return Invalid("invalid_verification_validation");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            int? sessionId = await FindSessionIdAsync(db, request.DetailId, inner);
            if (sessionId is null) return NotFound("verification_detail_not_found");
            var resolved = await sessions.ResolveReviewContextAsync(db, session, sessionId.Value, inner);
            if (resolved.Failure is not null) return resolved.Failure;
            var owner = resolved.Context!;
            var line = await ReadLineForUpdateAsync(db, owner, sessionId.Value, request.DetailId, inner);
            if (line is null) return NotFound("verification_detail_not_found");
            if (line.Status != "Pending") return Invalid("verification_line_not_pending");
            var items = ParseJson(line.LineItems);
            if (items is not { ValueKind: JsonValueKind.Array } array || array.GetArrayLength() == 0)
                return Success(new { validation_status = "Failed", message = "No line items found. Please add line items before validation." });
            decimal total = 0m;
            foreach (var item in array.EnumerateArray())
            {
                if (!item.TryGetProperty("calculated_amount", out var amount) || !TryDecimal(amount, out var value))
                    return Invalid("invalid_verification_line_items");
                total = checked(total + value);
            }
            decimal transactionAmount = decimal.TryParse(line.ExtractAmount, NumberStyles.Float, CultureInfo.InvariantCulture, out var extracted)
                ? extracted : 0m;
            bool passed = Math.Abs(total - transactionAmount) <= 0.01m;
            string validationStatus = passed ? "Passed" : "Failed";
            string message = passed
                ? $"Validation passed: Sum of line items ({total:F2}) matches transaction amount ({transactionAmount:F2})."
                : $"Validation failed: Sum of line items ({total:F2}) does not match transaction amount ({transactionAmount:F2}). Difference: {total - transactionAmount:F2}";
            await using (var update = db.CreateCommand("""
                UPDATE public.verification_details SET validation_status=$1,validation_message=$2
                WHERE id=$3 AND session_id=$4 AND status='Pending' AND range_status='active'
                """))
            {
                update.Parameters.AddWithValue(validationStatus); update.Parameters.AddWithValue(message);
                update.Parameters.AddWithValue(line.Id); update.Parameters.AddWithValue(sessionId.Value);
                if (await update.ExecuteNonQueryAsync(inner) != 1) return Invalid("verification_line_changed");
            }
            await WriteAuditAsync(db, owner, "VERIFICATION_LINE_VALIDATED", new { session_id = sessionId, detail_id = line.Id, validation_status = validationStatus }, inner);
            return Success(new { validation_status = validationStatus, validation_message = message,
                total_line_amount = total, transaction_amount = transactionAmount, passed });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> CompleteAsync(VerifiedSession? session,
        VerificationCompleteRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.DetailId < 1) return Invalid("invalid_verification_completion");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            int? sessionId = await FindSessionIdAsync(db, request.DetailId, inner);
            if (sessionId is null) return NotFound("verification_detail_not_found");
            var resolved = await sessions.ResolveReviewContextAsync(db, session, sessionId.Value, inner);
            if (resolved.Failure is not null) return resolved.Failure;
            var owner = resolved.Context!;
            var line = await ReadLineForUpdateAsync(db, owner, sessionId.Value, request.DetailId, inner);
            if (line is null) return NotFound("verification_detail_not_found");
            if (line.Status != "Pending") return Invalid("verification_line_not_pending");
            if (line.ValidationStatus != "Passed") return Invalid("verification_validation_not_passed");
            await using (var update = db.CreateCommand("""
                UPDATE public.verification_details SET status='Verified',verified_by=$1,verified_at=now()
                WHERE id=$2 AND session_id=$3 AND status='Pending' AND range_status='active'
                """))
            {
                update.Parameters.AddWithValue(owner.Username); update.Parameters.AddWithValue(line.Id); update.Parameters.AddWithValue(sessionId.Value);
                if (await update.ExecuteNonQueryAsync(inner) != 1) return Invalid("verification_line_changed");
            }
            await using (var update = db.CreateCommand("UPDATE public.verification_sessions SET verified_count=verified_count+1 WHERE id=$1 AND client_id=$2"))
            {
                update.Parameters.AddWithValue(sessionId.Value); update.Parameters.AddWithValue(owner.ClientId);
                if (await update.ExecuteNonQueryAsync(inner) != 1) return NotFound("verification_session_not_found");
            }
            await WriteAuditAsync(db, owner, "VERIFICATION_LINE_COMPLETED", new { session_id = sessionId, detail_id = line.Id }, inner);
            return Success(new { message = "Transaction marked as verified.", detail_id = line.Id });
        }, token);
    }, ct);

    private async Task<VerificationRuntimeResult> SaveBundleCoreAsync(VerifiedSession? session,
        VerificationBundleSaveRequest request, CancellationToken ct, bool useStoredVoucherChecks = false)
    {
        if (request is null || request.SessionId < 1 || request.CurrentDetailId < 1 || request.Lines is null || request.Lines.Count == 0
            || request.Lines.Any(line => line is null || line.DetailId < 1 || !ValidLineItems(line.LineItems))
            || request.VoucherChecks is null || request.ProcedureChecks is null
            || request.Lines.Select(line => line.DetailId).Distinct().Count() != request.Lines.Count
            || !request.Lines.Any(line => line.DetailId == request.CurrentDetailId)) return Invalid("invalid_verification_bundle");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var resolved = await sessions.ResolveReviewContextAsync(db, session, request.SessionId, inner);
            if (resolved.Failure is not null) return resolved.Failure;
            var owner = resolved.Context!;
            var lines = await ReadLinesForUpdateAsync(db, owner, request.SessionId, request.Lines.Select(line => line.DetailId).ToArray(), inner);
            if (lines.Count != request.Lines.Count) return NotFound("verification_detail_not_found");
            var current = lines.Single(line => line.Id == request.CurrentDetailId);
            if (current.Status != "Pending") return Invalid("verification_line_not_pending");
            var eligible = lines.Where(line => line.Status == "Pending").ToArray();
            if (request.Verify)
            {
                var submittedChecks = useStoredVoucherChecks
                    ? await ReadVoucherChecksAsync(db, owner.ClientId, current.TransactionRef, owner.Rulebook, inner)
                    : request.VoucherChecks;
                var checks = await SaveVoucherChecksAsync(db, owner, current.TransactionRef,
                    submittedChecks, verify: true, inner);
                if (checks.Failure is not null) return checks.Failure;
                var procedureError = await SaveProcedureChecksAsync(db, owner, request.SessionId,
                    request.ProcedureChecks, eligible.Select(x => x.Id).ToHashSet(), inner);
                if (procedureError is not null) return procedureError;
                foreach (var line in eligible)
                {
                    var payload = request.Lines.Single(item => item.DetailId == line.Id);
                    await SaveLineAsync(db, owner, request.SessionId, line, payload.LineItems,
                        payload.TransactionType, payload.ProcedureData, inner);
                }
                if (!string.IsNullOrWhiteSpace(current.TransactionRef))
                    await FlagVoucherChecksAsync(db, owner, current.TransactionRef, checks.NotDoneIds, checks.DoneIds, inner);
                var statuses = await status.RecomputeAsync(db, owner, eligible.Select(x => x.Id).ToArray(), inner);
                await WriteAuditAsync(db, owner, "VERIFICATION_VOUCHER_SAVE_AND_VERIFY", new { session_id = request.SessionId,
                    current_detail_id = request.CurrentDetailId, saved_line_count = eligible.Length }, inner);
                return await NavigationResultAsync(db, owner, request.SessionId, request.CurrentDetailId,
                    statuses, checks.NotDoneIds.Count, inner);
            }
            var unverifiedChecks = await SaveVoucherChecksAsync(db, owner, current.TransactionRef,
                request.VoucherChecks, verify: false, inner);
            if (unverifiedChecks.Failure is not null) return unverifiedChecks.Failure;
            var checkError = await SaveProcedureChecksAsync(db, owner, request.SessionId,
                request.ProcedureChecks, eligible.Select(x => x.Id).ToHashSet(), inner);
            if (checkError is not null) return checkError;
            foreach (var line in eligible)
            {
                var payload = request.Lines.Single(item => item.DetailId == line.Id);
                await SaveLineAsync(db, owner, request.SessionId, line, payload.LineItems,
                    payload.TransactionType, payload.ProcedureData, inner);
            }
            var lineStatuses = await status.RecomputeAsync(db, owner, eligible.Select(x => x.Id).ToArray(), inner);
            await WriteAuditAsync(db, owner, "VERIFICATION_VOUCHER_SAVED", new { session_id = request.SessionId,
                current_detail_id = request.CurrentDetailId, saved_line_count = eligible.Length }, inner);
            var response = StatusResponse(lineStatuses);
            if (lineStatuses.TryGetValue(request.CurrentDetailId, out var currentStatus))
            {
                response["cleared_status"] = currentStatus.ClearedStatus;
                response["matrix_version_applied"] = currentStatus.MatrixVersion;
            }
            return Success(response);
        }, ct);
    }

    private async Task<VerificationRuntimeResult> NavigationResultAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, int sessionId, int currentDetailId,
        IReadOnlyDictionary<int, VerificationLineStatusResult> statuses, int notDoneCount, CancellationToken ct)
    {
        var result = await VerificationSessionService.VoucherNavigationWithinTransactionAsync(
            db, owner.TenantId, sessionId, currentDetailId, "next", ct);
        if (result is null) return NotFound("verification_detail_not_found");
        result["verified"] = true;
        result["not_done_count"] = notDoneCount;
        AddLineStatuses(result, statuses);
        if (statuses.TryGetValue(currentDetailId, out var current))
        {
            result["cleared_status"] = current.ClearedStatus;
            result["matrix_version_applied"] = current.MatrixVersion;
        }
        return Success(result);
    }

    private static Dictionary<string, object?> StatusResponse(IReadOnlyDictionary<int, VerificationLineStatusResult> statuses)
    {
        var response = new Dictionary<string, object?> { ["saved"] = true };
        AddLineStatuses(response, statuses);
        return response;
    }

    private static void AddLineStatuses(IDictionary<string, object?> response,
        IReadOnlyDictionary<int, VerificationLineStatusResult> statuses)
    {
        response["line_statuses"] = statuses.ToDictionary(pair => pair.Key.ToString(CultureInfo.InvariantCulture),
            pair => pair.Value.ClearedStatus, StringComparer.Ordinal);
    }

    private static void AddStatus(IDictionary<string, object?> response, VerificationLineStatusResult value)
    {
        response["cleared_status"] = value.ClearedStatus;
        response["matrix_version_applied"] = value.MatrixVersion;
        response["category_id_applied"] = value.CategoryId;
        response["category_name_applied"] = value.CategoryName;
    }

    private async Task<ReviewLine?> ReadLineForUpdateAsync(PostgresTransactionContext db, VerificationReviewContext owner,
        int sessionId, int detailId, CancellationToken ct)
    {
        var rows = await ReadLinesForUpdateAsync(db, owner, sessionId, [detailId], ct);
        return rows.SingleOrDefault();
    }

    private static async Task<List<ReviewLine>> ReadLinesForUpdateAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, int sessionId, int[] detailIds, CancellationToken ct)
    {
        var result = new List<ReviewLine>();
        await using var command = db.CreateCommand("""
            SELECT vd.id,vd.client_data_id,vd.transaction_ref,vd.status,vd.validation_status,
                   cd.row_data::text,cd.extract_amount::text,vd.line_items::text
            FROM public.verification_details vd
            JOIN public.verification_sessions vs ON vs.id=vd.session_id AND vs.client_id=$2
            JOIN public.client_data cd ON cd.id=vd.client_data_id AND cd.client_id=vs.client_id
            JOIN public.platform_tenant_client_homes h ON h.client_id=vs.client_id AND h.tenant_id=$3
            WHERE vd.session_id=$1 AND vd.id=ANY($4) AND vd.range_status='active'
            ORDER BY vd.id FOR UPDATE OF vd
            """);
        command.Parameters.AddWithValue(sessionId); command.Parameters.AddWithValue(owner.ClientId);
        command.Parameters.AddWithValue(owner.TenantId); command.Parameters.AddWithValue(detailIds);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) result.Add(new(reader.GetInt32(0), reader.GetInt32(1),
            reader.IsDBNull(2) ? null : reader.GetString(2), reader.IsDBNull(3) ? null : reader.GetString(3),
            reader.IsDBNull(4) ? null : reader.GetString(4), reader.GetString(5),
            reader.IsDBNull(6) ? null : reader.GetString(6), reader.IsDBNull(7) ? null : reader.GetString(7)));
        return result;
    }

    private static async Task<int?> FindSessionIdAsync(PostgresTransactionContext db, int detailId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT session_id FROM public.verification_details WHERE id=$1");
        command.Parameters.AddWithValue(detailId); var value = await command.ExecuteScalarAsync(ct);
        return value is int id ? id : null;
    }

    private static async Task SaveLineAsync(PostgresTransactionContext db, VerificationReviewContext owner,
        int sessionId, ReviewLine line, JsonElement lineItems, string? transactionType,
        JsonElement? procedureData, CancellationToken ct)
    {
        JsonElement? procedure = procedureData;
        if (procedure is { ValueKind: JsonValueKind.Null or JsonValueKind.Undefined }) procedure = null;
        if (transactionType is not null)
        {
            JsonObject body = procedure is { ValueKind: JsonValueKind.Object } json
                ? JsonNode.Parse(json.GetRawText())!.AsObject() : new JsonObject();
            body["transaction_type"] = transactionType;
            procedure = JsonDocument.Parse(body.ToJsonString()).RootElement.Clone();
        }
        await using var command = db.CreateCommand("""
            UPDATE public.verification_details SET line_items=$1::jsonb,
                procedure_data=CASE WHEN $2::jsonb IS NULL THEN procedure_data ELSE $2::jsonb END
            WHERE id=$3 AND session_id=$4 AND status='Pending' AND range_status='active'
            """);
        command.Parameters.AddWithValue(NpgsqlDbType.Jsonb, lineItems.GetRawText());
        command.Parameters.AddWithValue(NpgsqlDbType.Jsonb, (object?)procedure?.GetRawText() ?? DBNull.Value);
        command.Parameters.AddWithValue(line.Id); command.Parameters.AddWithValue(sessionId);
        if (await command.ExecuteNonQueryAsync(ct) != 1) throw new InvalidOperationException("Verification line changed while it was being saved.");
    }

    private sealed record VoucherCheckSave(VerificationRuntimeResult? Failure, HashSet<int> NotDoneIds, HashSet<int> DoneIds);

    private static async Task<VoucherCheckSave> SaveVoucherChecksAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, string? transactionRef, IReadOnlyList<VerificationVoucherCheckInput> inputs,
        bool verify, CancellationToken ct, bool persist = true)
    {
        var active = owner.Rulebook.TryGetProperty("voucher_checks", out var checks) && checks.ValueKind == JsonValueKind.Array
            ? checks.EnumerateArray().Where(check => check.TryGetProperty("id", out var id) && id.TryGetInt32(out _))
                .ToDictionary(check => check.GetProperty("id").GetInt32(), check => check.TryGetProperty("name", out var name) ? name.GetString() ?? "" : "")
            : new Dictionary<int, string>();
        var submitted = new Dictionary<int, string>();
        foreach (var item in inputs)
        {
            if (!active.ContainsKey(item.CheckId) || item.State is not ("untouched" or "done" or "not_done")
                || !submitted.TryAdd(item.CheckId, item.State))
                return new(Invalid("invalid_voucher_check_state"), [], []);
        }
        if (verify)
        {
            var untouched = active.Where(check => submitted.GetValueOrDefault(check.Key, "untouched") == "untouched").Select(check => check.Value).ToArray();
            if (untouched.Length > 0)
                return new(Invalid("voucher_checks_untouched"), [], []);
            if (active.Count > 0 && string.IsNullOrWhiteSpace(transactionRef)) return new(Invalid("voucher_reference_required"), [], []);
        }
        if (inputs.Count > 0 && string.IsNullOrWhiteSpace(transactionRef)) return new(Invalid("voucher_reference_required"), [], []);
        if (persist && !string.IsNullOrWhiteSpace(transactionRef))
        {
            foreach (var item in inputs)
            {
                await using var upsert = db.CreateCommand("""
                    INSERT INTO public.voucher_check_status(client_id,transaction_ref,check_id,state,updated_at)
                    VALUES($1,$2,$3,$4,now())
                    ON CONFLICT(client_id,transaction_ref,check_id) DO UPDATE SET state=EXCLUDED.state,updated_at=now()
                    """);
                upsert.Parameters.AddWithValue(owner.ClientId); upsert.Parameters.AddWithValue(transactionRef);
                upsert.Parameters.AddWithValue(item.CheckId); upsert.Parameters.AddWithValue(item.State);
                await upsert.ExecuteNonQueryAsync(ct);
            }
        }
        var notDone = submitted.Where(pair => pair.Value == "not_done").Select(pair => pair.Key).ToHashSet();
        var done = submitted.Where(pair => pair.Value == "done").Select(pair => pair.Key).ToHashSet();
        return new(null, notDone, done);
    }

    private static async Task FlagVoucherChecksAsync(PostgresTransactionContext db, VerificationReviewContext owner,
        string transactionRef, IReadOnlySet<int> notDone, IReadOnlySet<int> done, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            UPDATE public.voucher_check_status SET flagged_in_observations=(state='not_done')
            WHERE client_id=$1 AND transaction_ref=$2 AND check_id=ANY($3)
            """);
        command.Parameters.AddWithValue(owner.ClientId); command.Parameters.AddWithValue(transactionRef);
        command.Parameters.AddWithValue(notDone.Concat(done).Distinct().ToArray());
        await command.ExecuteNonQueryAsync(ct);
    }

    private static async Task<VerificationRuntimeResult?> SaveProcedureChecksAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, int sessionId, IReadOnlyList<VerificationProcedureCheckInput> inputs,
        IReadOnlySet<int> eligibleLineIds, CancellationToken ct)
    {
        if (inputs.Count == 0) return null;
        foreach (var input in inputs)
        {
            if (!eligibleLineIds.Contains(input.VoucherLineId) || input.DocumentId < 1 || string.IsNullOrWhiteSpace(input.RoleName))
                return Invalid("invalid_procedure_check");
        }
        var roles = new HashSet<(int DocumentId, string Role)>(new DocumentRoleComparer());
        if (owner.Rulebook.TryGetProperty("categories", out var categories) && categories.ValueKind == JsonValueKind.Array)
            foreach (var category in categories.EnumerateArray())
                if (category.TryGetProperty("documents", out var documents) && documents.ValueKind == JsonValueKind.Array)
                    foreach (var document in documents.EnumerateArray())
                        if (document.TryGetProperty("document_id", out var id) && id.TryGetInt32(out int documentId)
                            && document.TryGetProperty("authority_roles", out var authorityRoles) && authorityRoles.ValueKind == JsonValueKind.Array)
                            foreach (var role in authorityRoles.EnumerateArray())
                                if (role.TryGetProperty("role_name", out var roleName) && roleName.GetString() is string name)
                                    roles.Add((documentId, name));
        if (inputs.Any(input => !roles.Contains((input.DocumentId, input.RoleName)))) return Invalid("procedure_role_not_in_rulebook");
        foreach (var input in inputs)
        {
            await using var upsert = db.CreateCommand("""
                INSERT INTO public.voucher_line_procedure_checks(voucher_line_id,document_id,role_name,is_checked,checked_at,checked_by)
                VALUES($1,$2,$3,$4,now(),$5)
                ON CONFLICT(voucher_line_id,document_id,role_name) DO UPDATE
                SET is_checked=EXCLUDED.is_checked,checked_at=now(),checked_by=EXCLUDED.checked_by
                """);
            upsert.Parameters.AddWithValue(input.VoucherLineId); upsert.Parameters.AddWithValue(input.DocumentId);
            upsert.Parameters.AddWithValue(input.RoleName); upsert.Parameters.AddWithValue(input.IsChecked);
            upsert.Parameters.AddWithValue(owner.Username); await upsert.ExecuteNonQueryAsync(ct);
        }
        return null;
    }

    private sealed class DocumentRoleComparer : IEqualityComparer<(int DocumentId, string Role)>
    {
        public bool Equals((int DocumentId, string Role) x, (int DocumentId, string Role) y) =>
            x.DocumentId == y.DocumentId && string.Equals(x.Role, y.Role, StringComparison.Ordinal);
        public int GetHashCode((int DocumentId, string Role) value) => HashCode.Combine(value.DocumentId,
            StringComparer.Ordinal.GetHashCode(value.Role));
    }

    private static async Task<List<VerificationVoucherCheckInput>> ReadVoucherChecksAsync(PostgresTransactionContext db,
        int clientId, string? transactionRef, JsonElement rulebook, CancellationToken ct)
    {
        var existing = new Dictionary<int, string>();
        if (!string.IsNullOrWhiteSpace(transactionRef))
        {
            await using var command = db.CreateCommand("""
                SELECT check_id,state FROM public.voucher_check_status WHERE client_id=$1 AND transaction_ref=$2
                """);
            command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(transactionRef);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct)) existing[reader.GetInt32(0)] = reader.GetString(1);
        }
        var result = new List<VerificationVoucherCheckInput>();
        if (rulebook.TryGetProperty("voucher_checks", out var checks) && checks.ValueKind == JsonValueKind.Array)
            foreach (var check in checks.EnumerateArray())
                if (check.TryGetProperty("id", out var id) && id.TryGetInt32(out int checkId))
                    result.Add(new() { CheckId = checkId, State = existing.GetValueOrDefault(checkId, "untouched") });
        return result;
    }

    private async Task WriteAuditAsync(PostgresTransactionContext db, VerificationReviewContext owner,
        string action, object detail, CancellationToken ct) => await audit.WriteAsync(db,
        new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), new LogActor("authenticated", owner.UserId, owner.Username),
            owner.ClientId, action, "Verification", JsonSerializer.SerializeToElement(detail)), ct);

    private async Task<T> Run<T>(Func<CancellationToken, Task<T>> action, CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        return await action(timeout.Token);
    }

    private static bool ValidLineItems(JsonElement lineItems)
    {
        if (lineItems.ValueKind != JsonValueKind.Array) return false;
        foreach (var item in lineItems.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object) return false;
            foreach (string key in new[] { "vendor", "unit", "qty", "rate", "calculated_amount" })
                if (!item.TryGetProperty(key, out _)) return false;
            foreach (string key in new[] { "qty", "rate", "calculated_amount" })
                if (!item.TryGetProperty(key, out var value) || !TryDecimal(value, out _)) return false;
        }
        return true;
    }

    private static bool TryDecimal(JsonElement value, out decimal result)
    {
        if (value.ValueKind == JsonValueKind.Number) return value.TryGetDecimal(out result);
        if (value.ValueKind == JsonValueKind.String) return decimal.TryParse(value.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out result);
        result = 0m; return false;
    }

    private static JsonElement? ParseJson(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        try { using var document = JsonDocument.Parse(value); return document.RootElement.Clone(); }
        catch (JsonException) { return null; }
    }

    private static VerificationRuntimeResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value));
    private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code);
    private static VerificationRuntimeResult NotFound(string code) => new("not_found", Code: code);
}
