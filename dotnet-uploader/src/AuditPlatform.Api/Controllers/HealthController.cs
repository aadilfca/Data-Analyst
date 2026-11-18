using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Health;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Produces("application/json")]
public sealed class HealthController(
    IOptions<PlatformOptions> options, IOptions<ApplicationShellOptions> shell,
    IOptions<AuthenticationOptions> authentication, IOptions<UserManagementOptions> users,
    IOptions<ClientManagementOptions> clients, PostgresReadinessCheck postgres) : ControllerBase
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

    // Database health alone must not make a blank C# host look like a working
    // app. The tray and deployment candidate use this stricter readiness gate.
    [HttpGet("/health/app-ready")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]
    public async Task<IActionResult> ApplicationReadiness(CancellationToken cancellationToken)
    {
        var reason = await postgres.CheckAsync(cancellationToken);
        reason ??= !shell.Value.Enabled ? "application_shell_not_configured"
            : !authentication.Value.Enabled ? "authentication_not_configured"
            : !users.Value.Enabled ? "user_management_not_configured"
            : !clients.Value.Enabled ? "client_management_not_configured" : null;
        if (reason is null) return Ok(new { status = "ready" });
        return StatusCode(StatusCodes.Status503ServiceUnavailable, new { status = "not_ready", reason });
    }
}
