using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Verification;

/// <summary>Tenant/client-scoped evidence and observation operations for Verification.</summary>
public sealed class VerificationEvidenceService(PostgresTransactions transactions,
    VerificationSessionService sessions, VerificationStatusService status, PostgresAuditWriter audit,
    IOptions<VerificationRuntimeOptions> options)
{
    private const string AccountHeadSystemName = "[System] Account Head Recommendation";
    private const string CorrectnessSystemName = "[System] Voucher Line Correctness";
    private const string AccountHeadPrefix = "__account_head_recommendation__: ";
    private const string CorrectText = "correct";

    public Task<VerificationRuntimeResult> GetDocumentsAsync(VerifiedSession? session, int detailId,
        CancellationToken ct = default) => Run(async token =>
    {
        if (detailId < 1) return Invalid("invalid_verification_detail_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveLineAsync(db, session, detailId, inner, lockLine: false);
            if (scope.Failure is not null) return scope.Failure;
            int[] siblings = await SiblingIdsAsync(db, detailId, scope.Line!.Value.ClientDataId, scope.Owner!.ClientId, inner);
            var documents = new List<object>();
            await using var command = db.CreateCommand("""
                SELECT v.id,v.voucher_line_id,v.client_data_id,v.document_id,v.linked_by,v.linked_at,
                       v.signature_exception,d.name,d.is_active,cd.row_data::text,cd.extract_date::text,
                       cd.extract_amount::text,cd.rate_source
                FROM public.voucher_line_documents v
                JOIN public.client_data cd ON cd.id=v.client_data_id AND cd.client_id=$2
                LEFT JOIN public.audit_rule_documents d ON d.id=v.document_id
                WHERE v.voucher_line_id=ANY($1) AND v.status='active' ORDER BY v.linked_at,v.id
                """);
            command.Parameters.AddWithValue(siblings); command.Parameters.AddWithValue(scope.Owner.ClientId);
            await using var reader = await command.ExecuteReaderAsync(inner);
            while (await reader.ReadAsync(inner))
            {
                using var row = JsonDocument.Parse(reader.GetString(9));
                documents.Add(new { id = reader.GetInt32(0), voucher_line_id = reader.GetInt32(1),
                    client_data_id = reader.GetInt32(2), document_id = reader.GetInt32(3),
                    document_name = reader.IsDBNull(7) ? null : reader.GetString(7),
                    is_active = reader.IsDBNull(8) ? (bool?)null : reader.GetBoolean(8),
                    linked_by = reader.IsDBNull(4) ? null : reader.GetString(4),
                    linked_at = reader.IsDBNull(5) ? null : reader.GetValue(5),
                    signature_exception = !reader.IsDBNull(6) && reader.GetBoolean(6),
                    row_data = row.RootElement.Clone(), extract_date = reader.IsDBNull(10) ? null : reader.GetString(10),
                    extract_amount = DecimalOrNull(reader.IsDBNull(11) ? null : reader.GetString(11)),
                    rate_source = reader.IsDBNull(12) ? null : reader.GetString(12) });
            }
            return Success(new { voucher_line_id = detailId, documents, count = documents.Count });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> AssignDocumentAsync(VerifiedSession? session,
        VerificationDocumentAssignRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.VoucherLineId < 1 || request.ClientDataId < 1 || request.DocumentId < 1)
            return Invalid("invalid_verification_document_assignment");
        try
        {
            return await transactions.ExecuteAsync(async (db, inner) =>
            {
                var scope = await ResolveLineAsync(db, session, request.VoucherLineId, inner, lockLine: true);
                if (scope.Failure is not null) return scope.Failure;
                var owner = scope.Owner!;
                await using (var source = db.CreateCommand("SELECT 1 FROM public.client_data WHERE id=$1 AND client_id=$2 AND is_deleted IS FALSE FOR KEY SHARE"))
                {
                    source.Parameters.AddWithValue(request.ClientDataId); source.Parameters.AddWithValue(owner.ClientId);
                    if (await source.ExecuteScalarAsync(inner) is null) return NotFound("verification_data_bank_row_not_found");
                }
                await using (var document = db.CreateCommand("SELECT 1 FROM public.audit_rule_documents WHERE id=$1 AND is_active IS TRUE AND COALESCE(is_system,FALSE)=FALSE"))
                {
                    document.Parameters.AddWithValue(request.DocumentId);
                    if (await document.ExecuteScalarAsync(inner) is null) return NotFound("verification_document_not_found");
                }
                await using (var existing = db.CreateCommand("""
                    SELECT v.id,v.voucher_line_id,d.transaction_ref FROM public.voucher_line_documents v
                    JOIN public.verification_details d ON d.id=v.voucher_line_id
                    JOIN public.verification_sessions s ON s.id=d.session_id AND s.client_id=$2
                    WHERE v.client_data_id=$1 AND v.status='active' LIMIT 1
                    """))
                {
                    existing.Parameters.AddWithValue(request.ClientDataId); existing.Parameters.AddWithValue(owner.ClientId);
                    await using var reader = await existing.ExecuteReaderAsync(inner);
                    if (await reader.ReadAsync(inner)) return Invalid("verification_evidence_already_assigned");
                }
                int id;
                await using (var insert = db.CreateCommand("""
                    INSERT INTO public.voucher_line_documents(voucher_line_id,client_data_id,document_id,linked_by)
                    VALUES($1,$2,$3,$4) RETURNING id
                    """))
                {
                    insert.Parameters.AddWithValue(request.VoucherLineId); insert.Parameters.AddWithValue(request.ClientDataId);
                    insert.Parameters.AddWithValue(request.DocumentId); insert.Parameters.AddWithValue(owner.Username);
                    id = (int)(await insert.ExecuteScalarAsync(inner))!;
                }
                await WriteAuditAsync(db, owner, "VERIFICATION_EVIDENCE_LINKED", new { assignment_id = id,
                    voucher_line_id = request.VoucherLineId, client_data_id = request.ClientDataId,
                    document_id = request.DocumentId }, inner);
                return await ReadAssignmentAsync(db, owner.ClientId, id, inner);
            }, token);
        }
        catch (PostgresException error) when (error.SqlState == PostgresErrorCodes.UniqueViolation)
        { return Invalid("verification_evidence_already_assigned"); }
    }, ct);

    public Task<VerificationRuntimeResult> UnassignDocumentAsync(VerifiedSession? session, int assignmentId,
        CancellationToken ct = default) => Run(async token =>
    {
        if (assignmentId < 1) return Invalid("invalid_verification_assignment_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var link = await ReadAssignmentScopeAsync(db, assignmentId, inner, forUpdate: false);
            if (link is null) return NotFound("verification_document_link_not_found");
            var scope = await ResolveLineAsync(db, session, link.DetailId, inner, lockLine: true);
            if (scope.Failure is not null) return scope.Failure;
            link = await ReadAssignmentScopeAsync(db, assignmentId, inner, forUpdate: true);
            if (link is null) return NotFound("verification_document_link_not_found");
            if (link.Status != "active") return NotFound("verification_document_link_already_unassigned");
            if (!await AssignmentSourceBelongsToClientAsync(db, assignmentId, scope.Owner!.ClientId, inner))
                return NotFound("verification_document_link_not_found");
            await using (var update = db.CreateCommand("UPDATE public.voucher_line_documents SET status='unassigned',unassigned_at=now() WHERE id=$1 AND status='active'"))
            {
                update.Parameters.AddWithValue(assignmentId);
                if (await update.ExecuteNonQueryAsync(inner) != 1) return NotFound("verification_document_link_already_unassigned");
            }
            await WriteAuditAsync(db, scope.Owner!, "VERIFICATION_EVIDENCE_UNLINKED", new { assignment_id = assignmentId,
                voucher_line_id = link.DetailId, client_data_id = link.ClientDataId,
                document_id = link.DocumentId }, inner);
            return Success(new { id = assignmentId, voucher_line_id = link.DetailId,
                client_data_id = link.ClientDataId, document_id = link.DocumentId, status = "unassigned" });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SetSignatureExceptionAsync(VerifiedSession? session,
        VerificationSignatureExceptionRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.AssignmentId < 1) return Invalid("invalid_verification_assignment_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var link = await ReadAssignmentScopeAsync(db, request.AssignmentId, inner, forUpdate: false);
            if (link is null || link.Status != "active") return NotFound("verification_document_link_not_found");
            var scope = await ResolveLineAsync(db, session, link.DetailId, inner, lockLine: true);
            if (scope.Failure is not null) return scope.Failure;
            link = await ReadAssignmentScopeAsync(db, request.AssignmentId, inner, forUpdate: true);
            if (link is null || link.Status != "active"
                || !await AssignmentSourceBelongsToClientAsync(db, request.AssignmentId, scope.Owner!.ClientId, inner))
                return NotFound("verification_document_link_not_found");
            await using (var update = db.CreateCommand("UPDATE public.voucher_line_documents SET signature_exception=$1 WHERE id=$2 AND status='active'"))
            {
                update.Parameters.AddWithValue(request.SignatureException); update.Parameters.AddWithValue(request.AssignmentId);
                if (await update.ExecuteNonQueryAsync(inner) != 1) return NotFound("verification_document_link_not_found");
            }
            await WriteAuditAsync(db, scope.Owner!, "VERIFICATION_SIGNATURE_EXCEPTION_SET", new {
                assignment_id = request.AssignmentId, signature_exception = request.SignatureException }, inner);
            return Success(new { id = request.AssignmentId, voucher_line_id = link.DetailId,
                signature_exception = request.SignatureException });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> GetProcedureChecksAsync(VerifiedSession? session, int detailId,
        CancellationToken ct = default) => Run(async token =>
    {
        if (detailId < 1) return Invalid("invalid_verification_detail_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveLineAsync(db, session, detailId, inner, lockLine: false);
            if (scope.Failure is not null) return scope.Failure;
            int[] siblings = await SiblingIdsAsync(db, detailId, scope.Line!.Value.ClientDataId, scope.Owner!.ClientId, inner);
            var latest = new Dictionary<(int DocumentId, string Role), ProcedureCheckRow>();
            await using var command = db.CreateCommand("""
                SELECT p.document_id,p.role_name,p.is_checked,p.checked_by,p.checked_at,d.name,d.is_active,p.id
                FROM public.voucher_line_procedure_checks p
                LEFT JOIN public.audit_rule_documents d ON d.id=p.document_id
                WHERE p.voucher_line_id=ANY($1)
                """);
            command.Parameters.AddWithValue(siblings);
            await using var reader = await command.ExecuteReaderAsync(inner);
            while (await reader.ReadAsync(inner))
            {
                var row = new ProcedureCheckRow(reader.GetInt32(0), reader.GetString(1), reader.GetBoolean(2),
                    reader.IsDBNull(3) ? null : reader.GetString(3), reader.IsDBNull(4) ? null : reader.GetValue(4),
                    reader.IsDBNull(5) ? null : reader.GetString(5), reader.IsDBNull(6) ? null : reader.GetBoolean(6), reader.GetInt32(7));
                var key = (row.DocumentId, row.RoleName);
                if (!latest.TryGetValue(key, out var current) || CompareTime(row.CheckedAt, current.CheckedAt, row.Id, current.Id) > 0)
                    latest[key] = row;
            }
            var checks = latest.Values.Select(row => new { document_id = row.DocumentId, role_name = row.RoleName,
                is_checked = row.IsChecked, checked_by = row.CheckedBy, checked_at = row.CheckedAt,
                document_name = row.DocumentName, is_active = row.IsActive }).ToArray();
            return Success(new { voucher_line_id = detailId, checks });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SetProcedureCheckAsync(VerifiedSession? session,
        VerificationProcedureCheckRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.VoucherLineId < 1 || request.DocumentId < 1 || string.IsNullOrWhiteSpace(request.RoleName))
            return Invalid("invalid_verification_procedure_check");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveLineAsync(db, session, request.VoucherLineId, inner, lockLine: true);
            if (scope.Failure is not null) return scope.Failure;
            if (!RulebookHasRole(scope.Owner!.Rulebook, request.DocumentId, request.RoleName))
                return Invalid("verification_procedure_role_not_configured");
            await UpsertProcedureCheckAsync(db, scope.Owner, request.VoucherLineId, request.DocumentId,
                request.RoleName, request.IsChecked, inner);
            await WriteAuditAsync(db, scope.Owner, "VERIFICATION_PROCEDURE_CHECK_SET", new {
                voucher_line_id = request.VoucherLineId, document_id = request.DocumentId,
                role_name = request.RoleName, is_checked = request.IsChecked }, inner);
            await using var command = db.CreateCommand("""
                SELECT voucher_line_id,document_id,role_name,is_checked,checked_by,checked_at
                FROM public.voucher_line_procedure_checks WHERE voucher_line_id=$1 AND document_id=$2 AND role_name=$3
                """);
            command.Parameters.AddWithValue(request.VoucherLineId); command.Parameters.AddWithValue(request.DocumentId);
            command.Parameters.AddWithValue(request.RoleName);
            await using var reader = await command.ExecuteReaderAsync(inner); await reader.ReadAsync(inner);
            return Success(new { voucher_line_id = reader.GetInt32(0), document_id = reader.GetInt32(1),
                role_name = reader.GetString(2), is_checked = reader.GetBoolean(3),
                checked_by = reader.IsDBNull(4) ? null : reader.GetString(4), checked_at = reader.IsDBNull(5) ? null : reader.GetValue(5) });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> GetObservationsAsync(VerifiedSession? session, int detailId,
        CancellationToken ct = default) => Run(async token =>
    {
        if (detailId < 1) return Invalid("invalid_verification_detail_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveLineAsync(db, session, detailId, inner, lockLine: false);
            if (scope.Failure is not null) return scope.Failure;
            int[] siblings = await SiblingIdsAsync(db, detailId, scope.Line!.Value.ClientDataId, scope.Owner!.ClientId, inner);
            var latest = new Dictionary<int, ObservationRow>();
            await using var command = db.CreateCommand("""
                SELECT o.id,o.document_id,o.observation_text,o.synced,o.last_synced_text,o.updated_by,o.updated_at,
                       d.name,d.is_active,d.is_system
                FROM public.voucher_line_observations o LEFT JOIN public.audit_rule_documents d ON d.id=o.document_id
                WHERE o.voucher_line_id=ANY($1)
                """);
            command.Parameters.AddWithValue(siblings);
            await using var reader = await command.ExecuteReaderAsync(inner);
            while (await reader.ReadAsync(inner))
            {
                var row = new ObservationRow(reader.GetInt32(0), reader.GetInt32(1),
                    reader.IsDBNull(2) ? "" : reader.GetString(2), reader.GetBoolean(3),
                    reader.IsDBNull(4) ? null : reader.GetString(4), reader.IsDBNull(5) ? null : reader.GetString(5),
                    reader.IsDBNull(6) ? null : reader.GetValue(6), reader.IsDBNull(7) ? null : reader.GetString(7),
                    reader.IsDBNull(8) ? null : reader.GetBoolean(8), !reader.IsDBNull(9) && reader.GetBoolean(9));
                if (!latest.TryGetValue(row.DocumentId, out var current) || CompareTime(row.UpdatedAt, current.UpdatedAt, row.Id, current.Id) > 0)
                    latest[row.DocumentId] = row;
            }
            var entries = latest.Values.Select(row => new { document_id = row.DocumentId,
                observation_text = row.Text, synced = row.Synced, last_synced_text = row.LastSyncedText,
                updated_by = row.UpdatedBy, updated_at = row.UpdatedAt, document_name = row.DocumentName,
                is_active = row.IsActive }).ToArray();
            return Success(new { voucher_line_id = detailId, entries });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SaveObservationAsync(VerifiedSession? session,
        VerificationObservationSaveRequest request, CancellationToken ct = default) => Run(async token =>
    {
        if (request is null || request.VoucherLineId < 1 || request.DocumentId == 0)
            return Invalid("invalid_verification_observation");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var scope = await ResolveLineAsync(db, session, request.VoucherLineId, inner, lockLine: true);
            if (scope.Failure is not null) return scope.Failure;
            bool isAccountHead = request.DocumentId == -1;
            bool isCorrectness = request.DocumentId == -2;
            int documentId = request.DocumentId;
            if (isAccountHead || isCorrectness)
                documentId = await EnsureSystemDocumentAsync(db, isAccountHead ? AccountHeadSystemName : CorrectnessSystemName, inner);
            else
            {
                await using var document = db.CreateCommand("SELECT name,is_active,is_system FROM public.audit_rule_documents WHERE id=$1");
                document.Parameters.AddWithValue(documentId);
                await using var documentReader = await document.ExecuteReaderAsync(inner);
                if (!await documentReader.ReadAsync(inner)) return NotFound("verification_document_not_found");
                string? name = documentReader.IsDBNull(0) ? null : documentReader.GetString(0);
                bool active = !documentReader.IsDBNull(1) && documentReader.GetBoolean(1);
                bool system = !documentReader.IsDBNull(2) && documentReader.GetBoolean(2);
                isAccountHead = system && name == AccountHeadSystemName;
                isCorrectness = system && name == CorrectnessSystemName;
                if (system && !isAccountHead && !isCorrectness) return NotFound("verification_document_not_found");
                if (!system && !active) return NotFound("verification_document_not_found");
            }
            await UpsertObservationAsync(db, request.VoucherLineId, documentId, request.ObservationText ?? "",
                scope.Owner!.Username, inner);
            int[] siblings = await SiblingIdsAsync(db, request.VoucherLineId, scope.Line!.Value.ClientDataId, scope.Owner.ClientId, inner);
            string? rolledUp = await RebuildObservationRollupAsync(db, scope.Owner.ClientId,
                request.VoucherLineId, siblings, inner);
            IReadOnlyDictionary<int, VerificationLineStatusResult> statuses = isAccountHead || isCorrectness
                ? await status.RecomputeAsync(db, scope.Owner, [request.VoucherLineId], inner)
                : new Dictionary<int, VerificationLineStatusResult>();
            await WriteAuditAsync(db, scope.Owner, "VERIFICATION_OBSERVATION_SAVED", new {
                voucher_line_id = request.VoucherLineId, document_id = request.DocumentId }, inner);
            await using var read = db.CreateCommand("""
                SELECT voucher_line_id,document_id,observation_text,synced,updated_by,updated_at
                FROM public.voucher_line_observations WHERE voucher_line_id=$1 AND document_id=$2
                """);
            read.Parameters.AddWithValue(request.VoucherLineId); read.Parameters.AddWithValue(documentId);
            await using var reader = await read.ExecuteReaderAsync(inner); await reader.ReadAsync(inner);
            var response = new Dictionary<string, object?> { ["voucher_line_id"] = reader.GetInt32(0),
                ["document_id"] = request.DocumentId, ["observation_text"] = reader.IsDBNull(2) ? "" : reader.GetString(2),
                ["synced"] = reader.GetBoolean(3), ["updated_by"] = reader.IsDBNull(4) ? null : reader.GetString(4),
                ["updated_at"] = reader.IsDBNull(5) ? null : reader.GetValue(5), ["voucher_observations"] = rolledUp };
            if (statuses.TryGetValue(request.VoucherLineId, out var lineStatus))
            { response["cleared_status"] = lineStatus.ClearedStatus; response["matrix_version_applied"] = lineStatus.MatrixVersion; }
            return Success(response);
        }, token);
    }, ct);

    internal async Task<VerificationRuntimeResult?> SaveBundleObservationsAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, IReadOnlyList<VerificationObservationInput> observations,
        IReadOnlyList<VerificationAccountHeadNoteInput> accountHeadNotes,
        IReadOnlyList<VerificationLineCorrectnessInput> correctness,
        IReadOnlySet<int> eligibleLineIds, CancellationToken ct)
    {
        if (observations.Any(item => !eligibleLineIds.Contains(item.VoucherLineId) || item.DocumentId < 1)
            || accountHeadNotes.Any(item => !eligibleLineIds.Contains(item.VoucherLineId))
            || correctness.Any(item => !eligibleLineIds.Contains(item.VoucherLineId)))
            return Invalid("invalid_verification_observation");
        foreach (var item in observations)
        {
            await using var document = db.CreateCommand("SELECT 1 FROM public.audit_rule_documents WHERE id=$1 AND is_active IS TRUE AND COALESCE(is_system,FALSE)=FALSE");
            document.Parameters.AddWithValue(item.DocumentId);
            if (await document.ExecuteScalarAsync(ct) is null) return NotFound("verification_document_not_found");
            await UpsertObservationAsync(db, item.VoucherLineId, item.DocumentId, item.ObservationText ?? "", owner.Username, ct);
        }
        int? accountHeadId = accountHeadNotes.Count > 0 ? await EnsureSystemDocumentAsync(db, AccountHeadSystemName, ct) : null;
        int? correctnessId = correctness.Count > 0 ? await EnsureSystemDocumentAsync(db, CorrectnessSystemName, ct) : null;
        foreach (var item in accountHeadNotes)
            await UpsertObservationAsync(db, item.VoucherLineId, accountHeadId!.Value, item.ObservationText ?? "", owner.Username, ct);
        foreach (var item in correctness)
            await UpsertObservationAsync(db, item.VoucherLineId, correctnessId!.Value,
                item.IsCorrect ? CorrectText : "incorrect", owner.Username, ct);
        var affected = observations.Select(x => x.VoucherLineId).Concat(accountHeadNotes.Select(x => x.VoucherLineId))
            .Concat(correctness.Select(x => x.VoucherLineId)).Distinct().ToArray();
        foreach (int detailId in affected)
        {
            var line = eligibleLineIds.Contains(detailId) ? detailId : 0;
            if (line == 0) continue;
            int clientDataId;
            await using (var getLine = db.CreateCommand("SELECT client_data_id FROM public.verification_details WHERE id=$1 AND session_id IN (SELECT id FROM public.verification_sessions WHERE client_id=$2)"))
            {
                getLine.Parameters.AddWithValue(detailId); getLine.Parameters.AddWithValue(owner.ClientId);
                var found = await getLine.ExecuteScalarAsync(ct);
                if (found is not int id) return NotFound("verification_detail_not_found");
                clientDataId = id;
            }
            int[] siblings = await SiblingIdsAsync(db, detailId, clientDataId, owner.ClientId, ct);
            await RebuildObservationRollupAsync(db, owner.ClientId, detailId, siblings, ct);
        }
        return null;
    }

    internal async Task RefreshBundleObservationRollupsAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, IReadOnlyList<int> detailIds, CancellationToken ct)
    {
        foreach (int detailId in detailIds.Distinct())
        {
            int? clientDataId;
            await using (var getLine = db.CreateCommand("SELECT client_data_id FROM public.verification_details WHERE id=$1 AND session_id IN (SELECT id FROM public.verification_sessions WHERE client_id=$2)"))
            {
                getLine.Parameters.AddWithValue(detailId); getLine.Parameters.AddWithValue(owner.ClientId);
                clientDataId = await getLine.ExecuteScalarAsync(ct) as int?;
            }
            if (clientDataId is not int sourceId) continue;
            int[] siblings = await SiblingIdsAsync(db, detailId, sourceId, owner.ClientId, ct);
            await RebuildObservationRollupAsync(db, owner.ClientId, detailId, siblings, ct);
        }
    }

    private sealed record LineScope(VerificationRuntimeResult? Failure, VerificationReviewContext? Owner,
        (int SessionId, int ClientDataId)? Line);
    private sealed record AssignmentScope(int DetailId, int ClientDataId, int DocumentId, string Status);
    private sealed record ProcedureCheckRow(int DocumentId, string RoleName, bool IsChecked, string? CheckedBy,
        object? CheckedAt, string? DocumentName, bool? IsActive, int Id);
    private sealed record ObservationRow(int Id, int DocumentId, string Text, bool Synced, string? LastSyncedText,
        string? UpdatedBy, object? UpdatedAt, string? DocumentName, bool? IsActive, bool IsSystem);

    private async Task<LineScope> ResolveLineAsync(PostgresTransactionContext db, VerifiedSession? session,
        int detailId, CancellationToken ct, bool lockLine)
    {
        int? sessionId;
        await using (var find = db.CreateCommand("SELECT session_id FROM public.verification_details WHERE id=$1"))
        { find.Parameters.AddWithValue(detailId); sessionId = await find.ExecuteScalarAsync(ct) as int?; }
        if (sessionId is null) return new(NotFound("verification_detail_not_found"), null, null);
        var resolved = await sessions.ResolveReviewContextAsync(db, session, sessionId.Value, ct);
        if (resolved.Failure is not null) return new(resolved.Failure, null, null);
        var owner = resolved.Context!;
        string sql = """
            SELECT vd.session_id,vd.client_data_id FROM public.verification_details vd
            JOIN public.verification_sessions vs ON vs.id=vd.session_id AND vs.client_id=$2
            JOIN public.client_data cd ON cd.id=vd.client_data_id AND cd.client_id=vs.client_id
            JOIN public.platform_tenant_client_homes h ON h.client_id=vs.client_id AND h.tenant_id=$3
            WHERE vd.id=$1 AND vd.range_status='active'
            """ + (lockLine ? " FOR UPDATE OF vd" : "");
        await using var line = db.CreateCommand(sql);
        line.Parameters.AddWithValue(detailId); line.Parameters.AddWithValue(owner.ClientId); line.Parameters.AddWithValue(owner.TenantId);
        await using var reader = await line.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct)
            ? new(null, owner, (reader.GetInt32(0), reader.GetInt32(1)))
            : new(NotFound("verification_detail_not_found"), null, null);
    }

    private static async Task<int[]> SiblingIdsAsync(PostgresTransactionContext db, int detailId,
        int clientDataId, int clientId, CancellationToken ct)
    {
        var ids = new List<int>();
        await using var command = db.CreateCommand("""
            SELECT vd.id FROM public.verification_details vd
            JOIN public.verification_sessions vs ON vs.id=vd.session_id AND vs.client_id=$2
            JOIN public.client_data cd ON cd.id=vd.client_data_id AND cd.client_id=vs.client_id
            WHERE vd.client_data_id=$1 ORDER BY vd.id
            """);
        command.Parameters.AddWithValue(clientDataId); command.Parameters.AddWithValue(clientId);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) ids.Add(reader.GetInt32(0));
        return ids.Count == 0 ? [detailId] : ids.ToArray();
    }

    private static async Task<AssignmentScope?> ReadAssignmentScopeAsync(PostgresTransactionContext db,
        int assignmentId, CancellationToken ct, bool forUpdate)
    {
        await using var command = db.CreateCommand("SELECT voucher_line_id,client_data_id,document_id,status FROM public.voucher_line_documents WHERE id=$1" + (forUpdate ? " FOR UPDATE" : ""));
        command.Parameters.AddWithValue(assignmentId); await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? new(reader.GetInt32(0), reader.GetInt32(1), reader.GetInt32(2), reader.GetString(3)) : null;
    }

    private static async Task<bool> AssignmentSourceBelongsToClientAsync(PostgresTransactionContext db,
        int assignmentId, int clientId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT EXISTS(SELECT 1 FROM public.voucher_line_documents v
            JOIN public.client_data c ON c.id=v.client_data_id AND c.client_id=$2 WHERE v.id=$1)
            """);
        command.Parameters.AddWithValue(assignmentId); command.Parameters.AddWithValue(clientId);
        return (bool)(await command.ExecuteScalarAsync(ct) ?? false);
    }

    private static async Task<VerificationRuntimeResult> ReadAssignmentAsync(PostgresTransactionContext db,
        int clientId, int assignmentId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT v.id,v.voucher_line_id,v.client_data_id,v.document_id,v.linked_by,v.linked_at
            FROM public.voucher_line_documents v JOIN public.verification_details vd ON vd.id=v.voucher_line_id
            JOIN public.verification_sessions vs ON vs.id=vd.session_id AND vs.client_id=$2 WHERE v.id=$1
            """);
        command.Parameters.AddWithValue(assignmentId); command.Parameters.AddWithValue(clientId);
        await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct)) return NotFound("verification_document_link_not_found");
        return Success(new { id = reader.GetInt32(0), voucher_line_id = reader.GetInt32(1),
            client_data_id = reader.GetInt32(2), document_id = reader.GetInt32(3),
            linked_by = reader.IsDBNull(4) ? null : reader.GetString(4), linked_at = reader.IsDBNull(5) ? null : reader.GetValue(5) });
    }

    internal static async Task UpsertProcedureCheckAsync(PostgresTransactionContext db, VerificationReviewContext owner,
        int lineId, int documentId, string roleName, bool isChecked, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            INSERT INTO public.voucher_line_procedure_checks(voucher_line_id,document_id,role_name,is_checked,checked_by,checked_at)
            VALUES($1,$2,$3,$4,$5,now()) ON CONFLICT(voucher_line_id,document_id,role_name) DO UPDATE
            SET is_checked=EXCLUDED.is_checked,checked_by=EXCLUDED.checked_by,checked_at=now()
            """);
        command.Parameters.AddWithValue(lineId); command.Parameters.AddWithValue(documentId); command.Parameters.AddWithValue(roleName);
        command.Parameters.AddWithValue(isChecked); command.Parameters.AddWithValue(owner.Username); await command.ExecuteNonQueryAsync(ct);
    }

    internal static bool RulebookHasRole(JsonElement rulebook, int documentId, string roleName)
    {
        if (!rulebook.TryGetProperty("categories", out var categories) || categories.ValueKind != JsonValueKind.Array) return false;
        foreach (var category in categories.EnumerateArray())
            if (category.TryGetProperty("documents", out var documents) && documents.ValueKind == JsonValueKind.Array)
                foreach (var document in documents.EnumerateArray())
                    if (document.TryGetProperty("document_id", out var id) && id.TryGetInt32(out int found) && found == documentId
                        && document.TryGetProperty("authority_roles", out var roles) && roles.ValueKind == JsonValueKind.Array
                        && roles.EnumerateArray().Any(role => role.TryGetProperty("role_name", out var name)
                            && string.Equals(name.GetString(), roleName, StringComparison.Ordinal))) return true;
        return false;
    }

    private static async Task UpsertObservationAsync(PostgresTransactionContext db, int lineId, int documentId,
        string text, string actor, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            INSERT INTO public.voucher_line_observations(voucher_line_id,document_id,observation_text,synced,updated_by,updated_at)
            VALUES($1,$2,$3,FALSE,$4,now()) ON CONFLICT(voucher_line_id,document_id) DO UPDATE
            SET observation_text=EXCLUDED.observation_text,synced=FALSE,updated_by=EXCLUDED.updated_by,updated_at=now()
            """);
        command.Parameters.AddWithValue(lineId); command.Parameters.AddWithValue(documentId);
        command.Parameters.AddWithValue(NpgsqlDbType.Text, text); command.Parameters.AddWithValue(actor);
        await command.ExecuteNonQueryAsync(ct);
    }

    private static async Task<int> EnsureSystemDocumentAsync(PostgresTransactionContext db, string name, CancellationToken ct)
    {
        await using (var gate = db.CreateCommand("SELECT pg_advisory_xact_lock(hashtextextended($1,0))"))
        { gate.Parameters.AddWithValue(name); await gate.ExecuteNonQueryAsync(ct); }
        await using (var find = db.CreateCommand("SELECT id FROM public.audit_rule_documents WHERE name=$1 AND is_system IS TRUE ORDER BY id LIMIT 1"))
        {
            find.Parameters.AddWithValue(name); var value = await find.ExecuteScalarAsync(ct);
            if (value is int id) return id;
        }
        await using var insert = db.CreateCommand("INSERT INTO public.audit_rule_documents(name,is_active,is_system,document_type) VALUES($1,FALSE,TRUE,'linked_template') RETURNING id");
        insert.Parameters.AddWithValue(name); return (int)(await insert.ExecuteScalarAsync(ct))!;
    }

    private static async Task<string?> RebuildObservationRollupAsync(PostgresTransactionContext db,
        int clientId, int detailId, int[] siblings, CancellationToken ct)
    {
        var latest = new Dictionary<int, ObservationRow>();
        var allIds = new List<int>();
        await using (var command = db.CreateCommand("""
            SELECT o.id,o.document_id,o.observation_text,o.updated_at,d.name,d.is_system
            FROM public.voucher_line_observations o LEFT JOIN public.audit_rule_documents d ON d.id=o.document_id
            WHERE o.voucher_line_id=ANY($1)
            """))
        {
            command.Parameters.AddWithValue(siblings); await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                var row = new ObservationRow(reader.GetInt32(0), reader.GetInt32(1), reader.IsDBNull(2) ? "" : reader.GetString(2),
                    false, null, null, reader.IsDBNull(3) ? null : reader.GetValue(3),
                    reader.IsDBNull(4) ? null : reader.GetString(4), null, !reader.IsDBNull(5) && reader.GetBoolean(5));
                allIds.Add(row.Id);
                if (!latest.TryGetValue(row.DocumentId, out var current) || CompareTime(row.UpdatedAt, current.UpdatedAt, row.Id, current.Id) > 0)
                    latest[row.DocumentId] = row;
            }
        }
        var blocks = new List<string>();
        await using (var checks = db.CreateCommand("""
            SELECT c.name FROM public.voucher_check_status s
            JOIN public.audit_rule_voucher_checks c ON c.id=s.check_id
            WHERE s.client_id=$1 AND s.transaction_ref=(SELECT transaction_ref FROM public.verification_details WHERE id=$2)
              AND c.is_active IS TRUE AND s.flagged_in_observations IS TRUE
            ORDER BY c.sort_order,c.id
            """))
        {
            checks.Parameters.AddWithValue(clientId); checks.Parameters.AddWithValue(detailId);
            await using var reader = await checks.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct)) blocks.Add($"{reader.GetString(0)} marked not done");
        }
        foreach (var entry in latest.Values.OrderBy(row => row.DocumentId))
        {
            string text = entry.Text.Trim();
            if (text.Length == 0 || entry.DocumentName == CorrectnessSystemName) continue;
            if (entry.DocumentName == AccountHeadSystemName)
                blocks.Add(text.StartsWith(AccountHeadPrefix, StringComparison.Ordinal)
                    ? $"[Account Head Recommendation]: Review suggested account head: {text[AccountHeadPrefix.Length..].Trim()}"
                    : $"[Account Head Recommendation]: New account head recommended: {text} — no match in Trial Balance");
            else if (entry.IsSystem) blocks.Add($"[Cross-check Shortfall]: {text}");
            else blocks.Add($"[{entry.DocumentName}]: {text}");
        }
        string? result = blocks.Count == 0 ? null : string.Join("\n", blocks);
        await using (var update = db.CreateCommand("UPDATE public.verification_details SET observations=$1 WHERE id=$2"))
        { update.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)result ?? DBNull.Value); update.Parameters.AddWithValue(detailId); await update.ExecuteNonQueryAsync(ct); }
        foreach (int entryId in allIds)
        {
            await using var update = db.CreateCommand("UPDATE public.voucher_line_observations SET synced=TRUE,last_synced_text=observation_text WHERE id=$1");
            update.Parameters.AddWithValue(entryId); await update.ExecuteNonQueryAsync(ct);
        }
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

    private static int CompareTime(object? left, object? right, int leftId, int rightId)
    {
        DateTimeOffset? a = ToTime(left), b = ToTime(right);
        int compare = Nullable.Compare(a, b);
        return compare != 0 ? compare : leftId.CompareTo(rightId);
    }
    private static DateTimeOffset? ToTime(object? value) => value switch
    {
        DateTimeOffset offset => offset, DateTime date => new DateTimeOffset(date), _ => null
    };
    private static decimal? DecimalOrNull(string? value) => decimal.TryParse(value,
        System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var parsed) ? parsed : null;
    private static VerificationRuntimeResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value));
    private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code);
    private static VerificationRuntimeResult NotFound(string code) => new("not_found", Code: code);
}
