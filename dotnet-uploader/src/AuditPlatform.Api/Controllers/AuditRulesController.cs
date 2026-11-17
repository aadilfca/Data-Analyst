using System.Text.Json;
using AuditPlatform.Api.AuditRules;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/audit-rules")]
public sealed class AuditRulesController(AuditRuleService service, IOptions<AuditRulesOptions> options,
    IOptions<CoexistenceOptions> coexistence, IOptions<ApplicationShellOptions> applicationShell) : ControllerBase
{
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

    [HttpGet("rulebook")]
    public Task<IActionResult> Rulebook(CancellationToken ct) => Run(service.RulebookAsync(Session, ct));

    [HttpGet("changes")]
    public Task<IActionResult> Changes([FromQuery(Name = "since_version")] long sinceVersion = 0, CancellationToken ct = default) =>
        Run(service.ChangesAsync(Session, sinceVersion, ct));

    [HttpGet("cross-checks/{id:int}/matrix")]
    public Task<IActionResult> CrossCheckMatrix(int id, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "cross-checks", id, "matrix", ct: ct));

    [HttpPut("cross-checks/{id:int}/matrix")]
    [RequestSizeLimit(1048576)]
    public Task<IActionResult> SaveCrossCheckMatrix(int id, [FromBody] JsonElement matrix,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "cross-checks", id, "matrix", matrix, ParseVersion(ifMatch), ct));

    [HttpPost("cross-checks/{id:int}/fields")]
    public Task<IActionResult> AddCrossCheckField(int id, [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.MatrixFieldAsync(Session, id, "add", null, null, null, ParseVersion(ifMatch), ct));

    [HttpDelete("cross-check-fields/{fieldId:int}")]
    public Task<IActionResult> DeleteCrossCheckField(int fieldId, [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.MatrixFieldAsync(Session, 0, "delete", fieldId, null, null, ParseVersion(ifMatch), ct));

    [HttpPut("cross-checks/{id:int}/field-order")]
    public Task<IActionResult> ReorderCrossCheckFields(int id, [FromBody] JsonElement order,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.MatrixFieldAsync(Session, id, "reorder", null, null, order, ParseVersion(ifMatch), ct));

    [HttpPut("cross-check-fields/{fieldId:int}/cells/{documentId:int}")]
    public Task<IActionResult> SetCrossCheckCell(int fieldId, int documentId, [FromBody] JsonElement cell,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.MatrixFieldAsync(Session, 0, "cell", fieldId, documentId, cell, ParseVersion(ifMatch), ct));

    [HttpPut("cross-check-fields/{fieldId:int}/condition")]
    public Task<IActionResult> SetCrossCheckCondition(int fieldId, [FromBody] JsonElement condition,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.MatrixFieldAsync(Session, 0, "condition", fieldId, null, condition, ParseVersion(ifMatch), ct));

    [HttpPut("cross-checks/{id:int}/documents")]
    public Task<IActionResult> SetCrossCheckDocuments(int id, [FromBody] JsonElement documents,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.MatrixFieldAsync(Session, id, "documents", null, null, documents, ParseVersion(ifMatch), ct));

    [HttpPut("cross-checks/{id:int}/meta")]
    public Task<IActionResult> SaveCrossCheckMeta(int id, [FromBody] JsonElement meta,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.MatrixFieldAsync(Session, id, "meta", null, null, meta, ParseVersion(ifMatch), ct));

    [HttpGet("categories/{id:int}/validation-rules")]
    public Task<IActionResult> CategoryValidationRules(int id, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "categories", id, "validation_rules_json", ct: ct));

    [HttpPut("categories/{id:int}/validation-rules")]
    [RequestSizeLimit(1048576)]
    public Task<IActionResult> SaveCategoryValidationRules(int id, [FromBody] JsonElement rules,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "categories", id, "validation_rules_json", rules, ParseVersion(ifMatch), ct));

    [HttpGet("categories/{id:int}/documents")]
    public Task<IActionResult> CategoryDocuments(int id, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "categories", id, "document_ids", ct: ct));

    [HttpPut("categories/{id:int}/documents")]
    [RequestSizeLimit(1048576)]
    public Task<IActionResult> SaveCategoryDocuments(int id, [FromBody] JsonElement documentIds,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "categories", id, "document_ids", documentIds, ParseVersion(ifMatch), ct));

    [HttpGet("categories/{id:int}/cross-checks")]
    public Task<IActionResult> CategoryCrossChecks(int id, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "categories", id, "cross_check_ids", ct: ct));

    [HttpPut("categories/{id:int}/cross-checks")]
    [RequestSizeLimit(1048576)]
    public Task<IActionResult> SaveCategoryCrossChecks(int id, [FromBody] JsonElement crossCheckIds,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.PropertyAsync(Session, "categories", id, "cross_check_ids", crossCheckIds, ParseVersion(ifMatch), ct));

    [HttpPut("categories/{categoryId:int}/documents/{documentId:int}/required")]
    public Task<IActionResult> SetCategoryDocumentRequired(int categoryId, int documentId, [FromBody] JsonElement payload,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.CategoryRequiredAsync(Session, categoryId, documentId, payload, ParseVersion(ifMatch), ct));

    [HttpGet("categories/{categoryId:int}/documents/{documentId:int}/assertions")]
    public Task<IActionResult> GetCategoryDocumentAssertions(int categoryId, int documentId, CancellationToken ct) =>
        Run(service.CategoryAssertionTagsAsync(Session, categoryId, "document", documentId, null, null, ct));

    [HttpPut("categories/{categoryId:int}/documents/{documentId:int}/assertions")]
    public Task<IActionResult> SetCategoryDocumentAssertions(int categoryId, int documentId, [FromBody] JsonElement payload,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.CategoryAssertionTagsAsync(Session, categoryId, "document", documentId, payload, ParseVersion(ifMatch), ct));

    [HttpGet("categories/{categoryId:int}/cross-checks/{crossCheckId:int}/assertions")]
    public Task<IActionResult> GetCategoryCrossCheckAssertions(int categoryId, int crossCheckId, CancellationToken ct) =>
        Run(service.CategoryAssertionTagsAsync(Session, categoryId, "cross-check", crossCheckId, null, null, ct));

    [HttpPut("categories/{categoryId:int}/cross-checks/{crossCheckId:int}/assertions")]
    public Task<IActionResult> SetCategoryCrossCheckAssertions(int categoryId, int crossCheckId, [FromBody] JsonElement payload,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.CategoryAssertionTagsAsync(Session, categoryId, "cross-check", crossCheckId, payload, ParseVersion(ifMatch), ct));

    [HttpGet("categories/{id:int}/assertion-gaps")]
    public Task<IActionResult> CategoryAssertionGaps(int id, CancellationToken ct) =>
        Run(service.AssertionGapsAsync(Session, id, ct));

    [HttpPost("voucher-checks/reorder")]
    public Task<IActionResult> ReorderVoucherChecks([FromBody] JsonElement order,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.ReorderVoucherChecksAsync(Session, order, ParseVersion(ifMatch), ct));

    [HttpGet("signature-specimens")]
    public Task<IActionResult> SignatureSpecimens([FromQuery(Name = "client_id")] int clientId,
        [FromQuery(Name = "document_id")] int? documentId,
        [FromQuery(Name = "include_inactive")] bool includeInactive = false, CancellationToken ct = default) =>
        Run(service.SignatureSpecimensAsync(Session, clientId, documentId, includeInactive, ct));

    [HttpGet("signature-specimens/{id:int}")]
    public Task<IActionResult> SignatureSpecimen(int id, CancellationToken ct) =>
        Run(service.SignatureSpecimenAsync(Session, id, ct));

    [HttpPost("signature-specimens")]
    public Task<IActionResult> CreateSignatureSpecimen([FromBody] SignatureSpecimenInput input,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.SaveSignatureSpecimenAsync(Session, null, input, ParseVersion(ifMatch), ct));

    [HttpPut("signature-specimens/{id:int}")]
    public Task<IActionResult> UpdateSignatureSpecimen(int id, [FromBody] SignatureSpecimenInput input,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.SaveSignatureSpecimenAsync(Session, id, input, ParseVersion(ifMatch), ct));

    [HttpDelete("signature-specimens/{id:int}")]
    public Task<IActionResult> DeleteSignatureSpecimen(int id, [FromHeader(Name = "If-Match")] string? ifMatch,
        CancellationToken ct) => Run(service.SetSignatureSpecimenActiveAsync(Session, id, false, ParseVersion(ifMatch), ct));

    [HttpPost("signature-specimens/{id:int}/restore")]
    public Task<IActionResult> RestoreSignatureSpecimen(int id, [FromHeader(Name = "If-Match")] string? ifMatch,
        CancellationToken ct) => Run(service.SetSignatureSpecimenActiveAsync(Session, id, true, ParseVersion(ifMatch), ct));

    [HttpDelete("signature-specimens/{id:int}/hard-delete")]
    public Task<IActionResult> HardDeleteSignatureSpecimen(int id, [FromHeader(Name = "If-Match")] string? ifMatch,
        CancellationToken ct) => Run(service.HardDeleteSignatureSpecimenAsync(Session, id, ParseVersion(ifMatch), ct));

    [HttpGet("voucher-checks/{id:int}/assertions")]
    public Task<IActionResult> GetVoucherCheckAssertions(int id, CancellationToken ct) =>
        Run(service.VoucherCheckAssertionsAsync(Session, id, null, null, ct));

    [HttpPut("voucher-checks/{id:int}/assertions")]
    public Task<IActionResult> SetVoucherCheckAssertions(int id, [FromBody] JsonElement payload,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.VoucherCheckAssertionsAsync(Session, id, payload, ParseVersion(ifMatch), ct));

    [HttpGet("{kind}")]
    public Task<IActionResult> List(string kind, [FromQuery] bool include_inactive = false,
        [FromQuery] int? limit = null, [FromQuery] int offset = 0, CancellationToken ct = default) =>
        Run(service.ListAsync(Session, kind, include_inactive, limit, offset, ct));

    [HttpGet("{kind}/{id:int}")]
    public Task<IActionResult> Get(string kind, int id, CancellationToken ct) => Run(service.GetAsync(Session, kind, id, ct));

    [HttpPost("{kind}")]
    [RequestSizeLimit(1048576)]
    public Task<IActionResult> Create(string kind, [FromBody] JsonElement definition,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.SaveAsync(Session, kind, null, definition, ParseVersion(ifMatch), ct));

    [HttpPut("{kind}/{id:int}")]
    [RequestSizeLimit(1048576)]
    public Task<IActionResult> Update(string kind, int id, [FromBody] JsonElement definition,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.SaveAsync(Session, kind, id, definition, ParseVersion(ifMatch), ct));

    [HttpPut("documents/{id:int}/transaction-level-approval")]
    public Task<IActionResult> SetDocumentTransactionApproval(int id, [FromBody] JsonElement payload,
        [FromHeader(Name = "If-Match")] string? ifMatch, CancellationToken ct) =>
        Run(service.DocumentTransactionApprovalAsync(Session, id, payload, ParseVersion(ifMatch), ct));

    [HttpDelete("{kind}/{id:int}")]
    public Task<IActionResult> Delete(string kind, int id, [FromHeader(Name = "If-Match")] string? ifMatch,
        CancellationToken ct) => Run(service.SetActiveAsync(Session, kind, id, false, ParseVersion(ifMatch), ct));

    [HttpPost("{kind}/{id:int}/restore")]
    public Task<IActionResult> Restore(string kind, int id, [FromHeader(Name = "If-Match")] string? ifMatch,
        CancellationToken ct) => Run(service.SetActiveAsync(Session, kind, id, true, ParseVersion(ifMatch), ct));

    private async Task<IActionResult> Run(Task<AuditRuleResult> operation)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled || !options.Value.RouteOwnershipEnabled || !options.Value.Valid()
            || !coexistence.Value.Enabled || !applicationShell.Value.Enabled)
            return StatusCode(503, new { detail = "Audit Rules are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        var result = await operation;
        if (result.Status == "success")
        {
            if (result.Total is not null) Response.Headers["X-Total-Count"] = result.Total.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);
            if (result.Version is not null) Response.Headers.ETag = $"\"{result.Version.Value}\"";
            return Ok(result.Value);
        }
        int status = result.Status switch
        {
            "unauthenticated" => 401, "forbidden" => 403, "not_found" => 404,
            "conflict" => 409, "too_large" => 413, "busy" => 429, "timeout" => 504,
            "disabled" or "unavailable" => 503, _ => 400
        };
        if (result.Version is not null) Response.Headers.ETag = $"\"{result.Version.Value}\"";
        return StatusCode(status, new { detail = result.Code ?? result.Status, status = result.Status,
            config_version = result.Version });
    }

    private static long? ParseVersion(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        if (value.Trim() == "*") return -1;
        var raw = value.Trim();
        if (raw.StartsWith("W/", StringComparison.Ordinal)) raw = raw[2..];
        raw = raw.Trim('"');
        return long.TryParse(raw, System.Globalization.NumberStyles.None,
            System.Globalization.CultureInfo.InvariantCulture, out var version) && version > 0 ? version : -1;
    }
}
