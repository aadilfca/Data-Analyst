using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.DataIntegrity;
using Microsoft.AspNetCore.Mvc;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Route("api/data-integrity")]
public sealed class DataIntegrityController(DataIntegrityService service) : ControllerBase
{
    [HttpGet("overview")]
    public async Task<IActionResult> Overview([FromQuery(Name = "client_id")] int clientId, CancellationToken ct)
    {
        Response.Headers.CacheControl = "no-store";
        var result = await service.ReadAsync(HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession, clientId, ct);
        return result.Status == "success" ? new JsonResult(result.Value, IntegrityJson.Options)
            : new ObjectResult(new { code = result.Code }) { StatusCode = result.Status switch
            {
                "unauthenticated" => 401, "forbidden" => 403, "invalid" => 400, "busy" => 429,
                "too_large" => 413, "review" => 409, "timeout" => 504, _ => 503
            } };
    }
}
