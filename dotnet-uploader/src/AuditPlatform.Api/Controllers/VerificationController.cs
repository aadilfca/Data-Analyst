using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Verification;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/verification")]
public sealed class VerificationController(VerificationSessionService service, VerificationReviewService review,
    VerificationEvidenceService evidence, VerificationFormService forms, VerificationPhysicalBillService physicalBills,
    VerificationCrossCheckService crossChecks, VerificationLookupService lookups,
    IOptions<VerificationRuntimeOptions> options) : ControllerBase
{
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

    private IActionResult? Guard()
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled || !options.Value.Valid()) return StatusCode(503, new { detail = "Verification is unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        return ModelState.IsValid ? null : BadRequest(new { detail = "Invalid Verification request" });
    }

    [HttpPost("start_session")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> StartSession([FromQuery] int client_id, [FromQuery] string cashbook_name,
        [FromQuery] string period_start, [FromQuery] string period_end, [FromQuery] string? created_by,
        [FromQuery] string session_type = "cash", CancellationToken ct = default)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return Result(await service.StartAsync(Session, new VerificationSessionStartRequest
        { ClientId = client_id, CashbookName = cashbook_name, PeriodStart = period_start, PeriodEnd = period_end,
            CreatedBy = created_by, SessionType = session_type }, ct));
    }

    [HttpGet("cashbooks")]
    public Task<IActionResult> Cashbooks([FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end,
        [FromQuery] string? category_group, CancellationToken ct) => Discover(client_id, "cash", date_start, date_end, category_group, ct);
    [HttpGet("local_vendors")]
    public Task<IActionResult> LocalVendors([FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end, CancellationToken ct)
        => Discover(client_id, "local_vendor", date_start, date_end, null, ct);
    [HttpGet("import_vendors")]
    public Task<IActionResult> ImportVendors([FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end, CancellationToken ct)
        => Discover(client_id, "import_vendor", date_start, date_end, null, ct);
    [HttpGet("export_customers")]
    public Task<IActionResult> ExportCustomers([FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end, CancellationToken ct)
        => Discover(client_id, "export_customer", date_start, date_end, null, ct);
    [HttpGet("payroll_accounts")]
    public Task<IActionResult> PayrollAccounts([FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end, CancellationToken ct)
        => Discover(client_id, "payroll", date_start, date_end, null, ct);
    [HttpGet("contractor_accounts")]
    public Task<IActionResult> ContractorAccounts([FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end, CancellationToken ct)
        => Discover(client_id, "contractor", date_start, date_end, null, ct);

    [HttpGet("latest_cashbook_date")]
    public Task<IActionResult> LatestCashbookDate([FromQuery] int client_id, [FromQuery] string? category_group, CancellationToken ct)
        => LatestDate(client_id, "cash", category_group, ct);
    [HttpGet("latest_local_vendor_date")]
    public Task<IActionResult> LatestLocalVendorDate([FromQuery] int client_id, CancellationToken ct)
        => LatestDate(client_id, "local_vendor", null, ct);
    [HttpGet("latest_import_vendor_date")]
    public Task<IActionResult> LatestImportVendorDate([FromQuery] int client_id, CancellationToken ct)
        => LatestDate(client_id, "import_vendor", null, ct);
    [HttpGet("latest_export_customer_date")]
    public Task<IActionResult> LatestExportCustomerDate([FromQuery] int client_id, CancellationToken ct)
        => LatestDate(client_id, "export_customer", null, ct);
    [HttpGet("latest_payroll_date")]
    public Task<IActionResult> LatestPayrollDate([FromQuery] int client_id, CancellationToken ct)
        => LatestDate(client_id, "payroll", null, ct);
    [HttpGet("latest_contractor_date")]
    public Task<IActionResult> LatestContractorDate([FromQuery] int client_id, CancellationToken ct)
        => LatestDate(client_id, "contractor", null, ct);

    [HttpGet("preview_cashbook")]
    public async Task<IActionResult> Preview([FromQuery] int client_id, [FromQuery] string cashbook_name,
        [FromQuery] string date_start, [FromQuery] string date_end, [FromQuery] int page = 1,
        [FromQuery] int limit = 50, [FromQuery] string session_type = "cash", CancellationToken ct = default)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return Result(await service.PreviewAsync(Session, client_id, cashbook_name, date_start, date_end, session_type, page, limit, ct));
    }

    [HttpGet("next/{session_id:int}")]
    public async Task<IActionResult> Next(int session_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.NextVoucherAsync(Session, session_id, ct)); }

    [HttpGet("voucher_nav/{session_id:int}")]
    public async Task<IActionResult> VoucherNavigation(int session_id, [FromQuery] int current_detail_id,
        [FromQuery] string direction, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.VoucherNavigationAsync(Session, session_id, current_detail_id, direction, ct)); }

    [HttpGet("session/{session_id:int}/index")]
    public async Task<IActionResult> SessionIndex(int session_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.SessionIndexAsync(Session, session_id, ct)); }

    [HttpGet("session/{session_id:int}/progress")]
    public async Task<IActionResult> SessionProgress(int session_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.SessionProgressAsync(Session, session_id, ct)); }

    [HttpGet("session/{session_id:int}/voucher_stats")]
    public async Task<IActionResult> VoucherStats(int session_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.VoucherStatsAsync(Session, session_id, ct)); }

    [HttpGet("stats")]
    public async Task<IActionResult> Stats(CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await lookups.StatsAsync(Session, ct)); }

    [HttpGet("supporting")]
    public async Task<IActionResult> Supporting([FromQuery] int client_id, [FromQuery] string search,
        [FromQuery] int template_id, [FromQuery] int limit = 50, CancellationToken ct = default)
    { var blocked = Guard(); return blocked ?? Result(await lookups.SupportingAsync(Session, client_id, search, template_id, limit, ct)); }

    [HttpGet("audit_guidance")]
    public async Task<IActionResult> AuditGuidance([FromQuery] string transaction_type, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await lookups.AuditGuidanceAsync(Session, transaction_type, ct)); }

    [HttpGet("category-for-account")]
    public async Task<IActionResult> CategoryForAccount([FromQuery] int client_id, [FromQuery] string account_code, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await lookups.CategoryForAccountAsync(Session, client_id, account_code, ct)); }

    [HttpGet("suggest-account-head")]
    public async Task<IActionResult> SuggestAccountHead([FromQuery] int client_id, [FromQuery] string description,
        [FromQuery] string? category, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await lookups.SuggestAccountHeadAsync(Session, client_id, description, category, ct)); }

    [HttpGet("line_status_reason/{voucher_line_id:int}")]
    public async Task<IActionResult> LineStatusReason(int voucher_line_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await lookups.LineStatusReasonAsync(Session, voucher_line_id, ct)); }

    [HttpGet("voucher_checks")]
    public async Task<IActionResult> VoucherChecks([FromQuery] int client_id, [FromQuery] string transaction_ref, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.VoucherChecksAsync(Session, client_id, transaction_ref, ct)); }

    [HttpPost("save_voucher_check")]
    [RequestSizeLimit(65536)]
    public async Task<IActionResult> SaveVoucherCheck([FromBody] VerificationVoucherCheckSaveRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.SaveVoucherCheckAsync(Session, request!, ct)); }

    [HttpPost("save_voucher_checks_batch")]
    [RequestSizeLimit(262144)]
    public async Task<IActionResult> SaveVoucherChecksBatch([FromBody] VerificationVoucherChecksBatchRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.SaveVoucherChecksBatchAsync(Session, request!, ct)); }

    [HttpPost("procedure_checks_batch")]
    [RequestSizeLimit(262144)]
    public async Task<IActionResult> ProcedureChecksBatch([FromBody] VerificationProcedureChecksBatchRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.SaveProcedureChecksBatchAsync(Session, request!, ct)); }

    [HttpGet("document_assignment_status")]
    public async Task<IActionResult> DocumentAssignmentStatus([FromQuery] string client_data_ids, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.DocumentAssignmentStatusAsync(Session, client_data_ids, ct)); }

    [HttpGet("audited_cashbook/{session_id:int}")]
    public async Task<IActionResult> AuditedCashbook(int session_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.AuditedCashbookAsync(Session, session_id, ct)); }

    [HttpGet("session/{session_id:int}/rulebook")]
    public async Task<IActionResult> Rulebook(int session_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.RulebookAsync(Session, session_id, ct)); }

    [HttpPost("session/{session_id:int}/rulebook/rebase")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> Rebase(int session_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await service.RebaseAsync(Session, session_id, ct)); }

    [HttpPost("session/{session_id:int}/reconcile_range")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> ReconcileRange(int session_id, [FromQuery] string period_start,
        [FromQuery] string period_end, CancellationToken ct)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return Result(await service.ReconcileRangeAsync(Session, session_id, period_start, period_end, ct));
    }

    [HttpPost("presence/{action}")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> Presence(string action, [FromBody] VerificationPresenceRequest? request, CancellationToken ct)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        if (action is not ("enter" or "heartbeat" or "leave")) return NotFound();
        if (request is null) return BadRequest(new { detail = "session_id is required" });
        return Result(await service.PresenceAsync(Session, request.SessionId, action, ct));
    }

    [HttpPost("session/close")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> Close([FromBody] VerificationPresenceRequest? request, CancellationToken ct)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return request is null ? BadRequest(new { detail = "session_id is required" })
            : Result(await service.PresenceAsync(Session, request.SessionId, "close", ct));
    }

    [HttpPost("save_line_items")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> SaveLineItems([FromBody] VerificationLineSaveRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.SaveLineItemsAsync(Session, request!, ct)); }

    [HttpPost("save_and_verify_voucher")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> SaveAndVerify([FromBody] VerificationSaveAndVerifyRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.SaveAndVerifyAsync(Session, request!, ct)); }

    [HttpPost("save_and_verify_voucher_bundle")]
    [RequestSizeLimit(4194304)]
    public async Task<IActionResult> SaveBundle([FromBody] VerificationBundleSaveRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.SaveBundleAsync(Session, request!, ct)); }

    [HttpGet("form_entries/{voucher_line_id:int}/{document_id:int}")]
    public async Task<IActionResult> FormEntries(int voucher_line_id,int document_id,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await forms.GetAsync(Session,voucher_line_id,document_id,ct)); }

    [HttpPost("save_form_entries")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> SaveFormEntries([FromBody] VerificationFormSaveRequest? request,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await forms.SaveAsync(Session,request!,ct)); }

    [HttpGet("form_copy_sources/{voucher_line_id:int}/{document_id:int}")]
    public async Task<IActionResult> FormCopySources(int voucher_line_id,int document_id,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await forms.CopySourcesAsync(Session,voucher_line_id,document_id,ct)); }

    [HttpGet("physical_bills/{voucher_line_id:int}")]
    public async Task<IActionResult> PhysicalBills(int voucher_line_id,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await physicalBills.GetAsync(Session,voucher_line_id,ct)); }

    [HttpPost("save_physical_bill")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> SavePhysicalBill([FromBody] VerificationPhysicalBillSaveRequest? request,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await physicalBills.SaveAsync(Session,request!,ct)); }

    [HttpGet("physical_bill_source_preview/{client_data_id:int}")]
    public async Task<IActionResult> PhysicalBillSourcePreview(int client_data_id,[FromQuery]string source_type,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await physicalBills.PreviewAsync(Session,client_data_id,source_type,ct)); }

    [HttpGet("cross_check_results/{voucher_line_id:int}")]
    public async Task<IActionResult> CrossCheckResults(int voucher_line_id,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await crossChecks.GetAsync(Session,voucher_line_id,ct)); }

    [HttpGet("voucher_support_data/{voucher_line_id:int}")]
    public async Task<IActionResult> VoucherSupportData(int voucher_line_id,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await evidence.GetVoucherSupportAsync(Session,voucher_line_id,ct)); }

    [HttpGet("procedure_modal_data/{voucher_line_id:int}")]
    public async Task<IActionResult> ProcedureModalData(int voucher_line_id,CancellationToken ct)
    { var blocked=Guard();return blocked??Result(await evidence.GetProcedureModalAsync(Session,voucher_line_id,crossChecks,ct)); }

    [HttpPost("verify_voucher_checks")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> VerifyVoucherChecks([FromBody] VerificationVerifyVoucherRequest? request, CancellationToken ct)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return request is null ? BadRequest(new { detail = "session_id and current_detail_id are required" })
            : Result(await review.VerifyVoucherAsync(Session, request.SessionId, request.CurrentDetailId, ct));
    }

    [HttpPost("validate")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> Validate([FromBody] VerificationValidateRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.ValidateAsync(Session, request!, ct)); }

    [HttpPost("complete")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> Complete([FromBody] VerificationCompleteRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await review.CompleteAsync(Session, request!, ct)); }

    [HttpGet("voucher_line_documents/{voucher_line_id:int}")]
    public async Task<IActionResult> VoucherLineDocuments(int voucher_line_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.GetDocumentsAsync(Session, voucher_line_id, ct)); }

    [HttpPost("assign_document")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> AssignDocument([FromBody] VerificationDocumentAssignRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.AssignDocumentAsync(Session, request!, ct)); }

    [HttpPost("unassign_document")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> UnassignDocument([FromBody] VerificationUnassignDocumentRequest? request, CancellationToken ct)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return request is null ? BadRequest(new { detail = "assignment_id is required" })
            : Result(await evidence.UnassignDocumentAsync(Session, request.AssignmentId, ct));
    }

    [HttpPost("set_signature_exception")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> SetSignatureException([FromBody] VerificationSignatureExceptionRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.SetSignatureExceptionAsync(Session, request!, ct)); }

    [HttpGet("procedure_checks/{voucher_line_id:int}")]
    public async Task<IActionResult> ProcedureChecks(int voucher_line_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.GetProcedureChecksAsync(Session, voucher_line_id, ct)); }

    [HttpPost("procedure_checks")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> SetProcedureCheck([FromBody] VerificationProcedureCheckRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.SetProcedureCheckAsync(Session, request!, ct)); }

    [HttpGet("line_observations/{voucher_line_id:int}")]
    public async Task<IActionResult> LineObservations(int voucher_line_id, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.GetObservationsAsync(Session, voucher_line_id, ct)); }

    [HttpPost("save_line_observation")]
    [RequestSizeLimit(65536)]
    public async Task<IActionResult> SaveLineObservation([FromBody] VerificationObservationSaveRequest? request, CancellationToken ct)
    { var blocked = Guard(); return blocked ?? Result(await evidence.SaveObservationAsync(Session, request!, ct)); }

    private async Task<IActionResult> Discover(int client, string workstream, string start, string end, string? group, CancellationToken ct)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return Result(await service.DiscoverAsync(Session, client, workstream, start, end, group, ct));
    }

    private async Task<IActionResult> LatestDate(int client, string workstream, string? group, CancellationToken ct)
    {
        var blocked = Guard(); if (blocked is not null) return blocked;
        return Result(await service.LatestDateAsync(Session, client, workstream, group, ct));
    }

    private IActionResult Result(VerificationRuntimeResult result)
    {
        if (result.Status == "success") return Ok(result.Value);
        int status = result.Status switch
        {
            "unauthenticated" => 401, "forbidden" => 403, "not_found" => 404,
            "conflict" => 409, "too_large" => 413, "busy" => 429, "timeout" => 504,
            "unavailable" or "disabled" => 503, _ => 400
        };
        return StatusCode(status, result.Value ?? System.Text.Json.JsonSerializer.SerializeToElement(new { detail = result.Code ?? result.Status,
            status = result.Status, code = result.Code }));
    }
}
