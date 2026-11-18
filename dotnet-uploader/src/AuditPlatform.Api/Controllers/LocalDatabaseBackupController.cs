using System.Net;
using System.Net.Sockets;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Primitives;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.LocalDatabaseBackup;
using Microsoft.AspNetCore.Mvc;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Route("api/database-backup")]
public sealed class LocalDatabaseBackupController(LocalDatabaseBackupService service) : ControllerBase
{
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    public sealed record CreateRequest([property: JsonPropertyName("operation_id")] string? OperationId);

    [HttpGet("status")]
    public async Task<IActionResult> Status([FromQuery(Name="operation_id")] string? operationId, CancellationToken ct)
    {
        Response.Headers.CacheControl = "no-store";
        if (!IsLocalRequest()) return NotFound();
        var result = await service.StatusAsync(Session, operationId, ct);
        return result.Status switch
        {
            "success" => Ok(new { ready = true, requirements = Array.Empty<string>(), backup = result.Backup }),
            "ready" => Ok(new { ready = true, requirements = Array.Empty<string>() }),
            "unavailable" => Ok(new { ready = false, requirements = result.Requirements ?? [result.Detail ?? "Local backup is unavailable."] }),
            "unauthenticated" => Unauthorized(),
            "forbidden" => Forbid(),
            "not_found" => NotFound(),
            "invalid" => BadRequest(new { detail = result.Detail ?? "Invalid backup reference." }),
            _ => StatusCode(503, new { detail = result.Detail ?? "Local backup status is unavailable." })
        };
    }

    [HttpPost("create")]
    [RequestSizeLimit(4096)]
    public async Task<IActionResult> Create([FromBody] CreateRequest? request, CancellationToken ct)
    {
        Response.Headers.CacheControl = "no-store";
        if (!IsLocalRequest()) return NotFound();
        if (!SameOriginWhenPresent()) return StatusCode(403, new { detail = "Open this backup action from the local application." });
        if (request?.OperationId is not { Length: 32 } operation)
            return BadRequest(new { detail = "Invalid backup reference." });
        var result = await service.CreateAsync(Session, operation, ct);
        return result.Status switch
        {
            "success" => Ok(result.Backup),
            "unauthenticated" => Unauthorized(),
            "forbidden" => Forbid(),
            "busy" or "conflict" => Conflict(new { detail = result.Detail, backup = result.Backup, retry_same = result.RetrySame }),
            "invalid" => BadRequest(new { detail = result.Detail }),
            "not_found" => NotFound(),
            "unavailable" => StatusCode(503, new { detail = result.Detail, backup = result.Backup, retry_same = result.RetrySame }),
            _ => StatusCode(503, new { detail = "Local backup is unavailable." })
        };
    }

    private bool IsLocalRequest()
    {
        var host = Request.Host.Host;
        var loopbackHost = host.Equals("localhost", StringComparison.OrdinalIgnoreCase)
            || IPAddress.TryParse(host, out var parsedHost) && IPAddress.IsLoopback(parsedHost);
        return loopbackHost && Request.HttpContext.Connection.RemoteIpAddress is { } peer && IPAddress.IsLoopback(peer);
    }

    private bool SameOriginWhenPresent()
    {
        var origin = Request.Headers["Origin"];
        if (StringValues.IsNullOrEmpty(origin)) return true;
        return Uri.TryCreate(origin.ToString(), UriKind.Absolute, out var uri)
            && string.IsNullOrEmpty(uri.UserInfo)
            && uri.Scheme == Request.Scheme
            && uri.Host.Equals(Request.Host.Host, StringComparison.OrdinalIgnoreCase)
            && uri.GetLeftPart(UriPartial.Authority).Equals(Request.Scheme + "://" + Request.Host.Value, StringComparison.OrdinalIgnoreCase);
    }
}
