using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Health;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Produces("application/json")]
public sealed class HealthController(
    IOptions<PlatformOptions> options, PostgresReadinessCheck postgres) : ControllerBase
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
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]
    public async Task<IActionResult> Readiness(CancellationToken cancellationToken)
    {
        var reason = await postgres.CheckAsync(cancellationToken);
        if (reason is null)
            return Ok(new { status = "ready" });

        return StatusCode(StatusCodes.Status503ServiceUnavailable, new
        {
            status = "not_ready",
            reason
        });
    }
}
