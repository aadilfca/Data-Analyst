using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Tenancy;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/auth")]
public sealed class TenantSessionController(TenantSessionService tenants, IOptions<ApplicationShellOptions> options) : ControllerBase
{
    public sealed record TenantInput(int? TenantId, string? ContextKey);
    [HttpGet("tenants")]
    public async Task<IActionResult> List(CancellationToken ct)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503);
        var result = await tenants.ReadAsync(HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession,ct);
        return result.Authenticated ? Ok(new { tenants=result.Tenants.Select(t=>new {id=t.Id,name=t.Name,type=t.Type,role=t.Role}),
            selected_tenant_id=result.SelectedTenantId,context_key=result.ContextKey }) : Unauthorized(new {detail="Not authenticated"});
    }
    [HttpPut("select-tenant")]
    public async Task<IActionResult> Select([FromBody] TenantInput? input, CancellationToken ct)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503);
        if (HttpContext.Items[typeof(VerifiedSession)] is not VerifiedSession session) return Unauthorized();
        if (!ModelState.IsValid || input?.TenantId is null || input.ContextKey is null) return UnprocessableEntity();
        return await tenants.SelectAsync(session,input.TenantId.Value,input.ContextKey,ct)
            ? Ok(new { success=true }) : Conflict(new { detail="Workspace access or context changed; reload the workspace list" });
    }
}
