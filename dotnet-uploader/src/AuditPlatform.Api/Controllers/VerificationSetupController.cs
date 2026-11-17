using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Verification;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/account-control-center")]
public sealed class VerificationSetupController(VerificationSetupService service, IOptions<VerificationSetupOptions> options) : ControllerBase
{
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

    private IActionResult? Guard()
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Verification setup is unavailable" });
        if (!options.Value.Valid()) return StatusCode(503, new { detail = "Verification setup is unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        return ModelState.IsValid ? null : BadRequest(new { detail = "Invalid Verification setup request" });
    }

    [HttpGet("accounts/latest")]
    public async Task<IActionResult> LatestAccounts([FromQuery] int client_id, CancellationToken ct)
        => Guard() ?? Result(await service.LatestAccountsAsync(Session, client_id, ct));

    [HttpPut("mapping")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> SaveAssignment([FromBody] WorkstreamAssignmentRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.SaveAssignmentAsync(Session, request, ct));

    [HttpPost("mapping/apply-to-children")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> ApplyToChildren([FromBody] WorkstreamBulkAssignmentRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.ApplyToChildrenAsync(Session, request, ct));

    [HttpGet("sets")]
    public async Task<IActionResult> ListSets([FromQuery] int client_id, CancellationToken ct)
        => Guard() ?? Result(await service.ListSetsAsync(Session, client_id, ct));

    [HttpGet("sets/{account_set_id:int}")]
    public async Task<IActionResult> GetSet(int account_set_id, CancellationToken ct)
        => Guard() ?? Result(await service.GetSetAsync(Session, account_set_id, ct));

    [HttpPost("sets")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> CreateSet([FromBody] AccountSetCreateRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.CreateSetAsync(Session, request, ct));

    [HttpPut("versions/{version_id:int}")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> UpdateDraft(int version_id, [FromBody] AccountSetDraftRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.UpdateDraftAsync(Session, version_id, request, ct));

    [HttpGet("versions/{version_id:int}/preview")]
    public async Task<IActionResult> Preview(int version_id, [FromQuery] int? upload_id, CancellationToken ct)
        => Guard() ?? Result(await service.PreviewAsync(Session, version_id, upload_id, ct));

    [HttpPost("versions/{version_id:int}/publish")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Publish(int version_id, CancellationToken ct)
        => Guard() ?? Result(await service.PublishAsync(Session, version_id, ct));

    [HttpPost("sets/{account_set_id:int}/draft")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> CreateDraft(int account_set_id, CancellationToken ct)
        => Guard() ?? Result(await service.CreateRevisionDraftAsync(Session, account_set_id, ct));

    private IActionResult Result(VerificationSetupResult result)
    {
        if (result.Status == "success") return Ok(result.Value);
        int status = result.Status switch
        {
            "unauthenticated" => 401,
            "forbidden" => 403,
            "not_found" => 404,
            "conflict" or "review" => 409,
            "too_large" => 413,
            "busy" => 429,
            "timeout" => 504,
            "disabled" or "unavailable" => 503,
            _ => 400
        };
        return StatusCode(status, new { detail = result.Code ?? result.Status, status = result.Status });
    }
}
