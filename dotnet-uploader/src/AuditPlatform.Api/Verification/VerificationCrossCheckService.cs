using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Verification;

/// <summary>Read-only detailed results from the same Cross-check evaluator used by Cleared/Query.</summary>
public sealed class VerificationCrossCheckService(PostgresTransactions transactions, VerificationSessionService sessions,
    VerificationStatusService status, IOptions<VerificationRuntimeOptions> options)
{
    public Task<VerificationRuntimeResult> GetAsync(VerifiedSession? session, int detailId, CancellationToken ct = default) => Run(async token =>
    {
        if (detailId < 1) return Invalid("invalid_verification_detail_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            int? sessionId; await using (var q = db.CreateCommand("SELECT session_id FROM public.verification_details WHERE id=$1")) { q.Parameters.AddWithValue(detailId); sessionId = await q.ExecuteScalarAsync(inner) as int?; }
            if (sessionId is null) return Success(new { voucher_line_id = detailId, category_id = (int?)null, category_name = (string?)null, evaluated = false, reason = "Voucher line not found.", summary = new { passed = 0, failed = 0, shortfall = 0, not_evaluated = 0 }, results = Array.Empty<object>() });
            var resolved = await sessions.ResolveReviewContextAsync(db, session, sessionId.Value, inner); if (resolved.Failure is not null) return resolved.Failure;
            return Success(await status.DetailedCrossChecksAsync(db, detailId, resolved.Context!, inner));
        }, token);
    }, ct);
    private async Task<T> Run<T>(Func<CancellationToken, Task<T>> action, CancellationToken ct) { if (!options.Value.Enabled) return (T)(object)new VerificationRuntimeResult("disabled", Code: "verification_runtime_disabled"); try { return await action(ct); } catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; } catch (PostgresException e) when (e.SqlState == PostgresErrorCodes.SerializationFailure || e.SqlState == PostgresErrorCodes.DeadlockDetected) { return (T)(object)new VerificationRuntimeResult("conflict", Code: "verification_transaction_retry"); } }
    private static VerificationRuntimeResult Success<T>(T value) => new("success", System.Text.Json.JsonSerializer.SerializeToElement(value)); private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code);
}
