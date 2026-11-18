using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Verification;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/verification")]
public sealed class AuditWorkbookController(AuditWorkbookService service,
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

    [HttpGet("session/{session_id:int}/audit_workbook")]
    public async Task<IActionResult> Get(int session_id, CancellationToken ct)
    {
        var blocked = Guard();
        return blocked ?? Result(await service.GetAsync(Session, session_id, ct));
    }

    [HttpGet("export_verification_excel/{session_id:int}")]
    public async Task<IActionResult> Export(int session_id, CancellationToken ct)
    {
        var blocked = Guard();
        if (blocked is not null) return blocked;
        var result = await service.ExportAsync(Session, session_id, ct);
        if (result.Failure is not null) return Result(result.Failure);
        var workbook = result.Download!;
        return File(workbook.Bytes, workbook.ContentType, workbook.Filename);
    }

    private IActionResult Result(VerificationRuntimeResult result)
    {
        if (result.Status == "success") return Ok(result.Value);
        int status = result.Status switch
        {
            "unauthenticated" => 401, "forbidden" => 403, "not_found" => 404,
            "conflict" => 409, "too_large" => 413, "busy" => 429,
            "timeout" => 504, "unavailable" or "disabled" => 503, _ => 400
        };
        return StatusCode(status, result.Value ?? System.Text.Json.JsonSerializer.SerializeToElement(new
        { detail = result.Code ?? result.Status, status = result.Status, code = result.Code }));
    }
}
