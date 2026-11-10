using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Produces("application/json")]
public sealed class HealthController(IOptions<PlatformOptions> options) : ControllerBase
{
    [HttpGet("/healthz")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult Liveness()
    {
        return Ok(new
        {
            status = "ok",
            service = options.Value.ApplicationName
        });
    }

    [HttpGet("/health/ready")]
    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]
    public IActionResult Readiness()
    {
        // P04 must supply a real dependency check before readiness can pass.
        // The foundation deliberately makes no database connection attempt.
        return StatusCode(StatusCodes.Status503ServiceUnavailable, new
        {
            status = "not_ready",
            reason = "database_not_configured"
        });
    }
}
