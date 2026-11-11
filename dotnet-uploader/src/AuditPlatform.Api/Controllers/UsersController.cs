using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Users;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

// Disabled by default. A single active tenant membership is required until
// the application has an explicit, server-validated tenant picker.
[Route("api/users")]
public sealed class UsersController(TenantUserService users, IOptions<UserManagementOptions> options) : ControllerBase
{
    public sealed record UserInput(string? Username, string? Password, string? Role);
    public sealed record EditInput(string? Username, string? Role);
    public sealed record PasswordInput(string? Password);

    [HttpGet]
    public async Task<IActionResult> List([FromQuery(Name = "include_inactive")] bool includeInactive,
        CancellationToken cancellationToken)
    {
        var selection = await ContextAsync(cancellationToken);
        if (selection.Error is not null) return selection.Error;
        if (!ModelState.IsValid) return InvalidRequest("query", "include_inactive");
        var result = await users.ListAsync(selection.Session, selection.TenantId, includeInactive, cancellationToken);
        if (result.Status != TenantUserStatus.Success) return Error(result.Status);
        Response.Headers.CacheControl = "no-store";
        return Ok(result.Users.Select(user => new
        {
            id = user.Id, username = user.Username, role = user.Role == "admin" ? "admin" : null,
            is_active = user.IsActive, last_login = user.LastLogin?.ToString("O")
        }));
    }

    [HttpPost]
    public async Task<IActionResult> Create([FromBody] UserInput? input, CancellationToken cancellationToken)
    {
        var selection = await ContextAsync(cancellationToken);
        if (selection.Error is not null) return selection.Error;
        if (!ModelState.IsValid || input is null) return InvalidRequest("body");
        if (input.Username is null || input.Password is null)
            return InvalidRequest("body", new[] { input.Username is null ? "username" : null,
                input.Password is null ? "password" : null }.Where(value => value is not null).Select(value => value!).ToArray());
        var role = ConvertRole(input.Role);
        if (role is null) return Error(TenantUserStatus.Invalid);
        var result = await users.CreateAsync(selection.Session, selection.TenantId, input.Username, input.Password, role, cancellationToken);
        return result.Status == TenantUserStatus.Success
            ? Ok(new { message = $"User '{input.Username}' created successfully", user_id = result.UserId }) : Error(result.Status);
    }

    [HttpPut("{userId:int}")]
    public async Task<IActionResult> Update(int userId, [FromBody] EditInput? input, CancellationToken cancellationToken)
    {
        var selection = await ContextAsync(cancellationToken);
        if (selection.Error is not null) return selection.Error;
        if (!ModelState.IsValid || input is null) return InvalidRequest("body");
        if (input.Username is null) return InvalidRequest("body", "username");
        var role = ConvertRole(input.Role);
        if (role is null) return Error(TenantUserStatus.Invalid);
        var result = await users.UpdateAsync(selection.Session, selection.TenantId, userId, input.Username, role, cancellationToken);
        return result.Status == TenantUserStatus.Success ? Ok(new { message = "User updated successfully" }) : Error(result.Status);
    }

    [HttpPost("{userId:int}/deactivate")]
    public Task<IActionResult> Deactivate(int userId, CancellationToken cancellationToken) => SetActive(userId, false, cancellationToken);

    [HttpPost("{userId:int}/reactivate")]
    public Task<IActionResult> Reactivate(int userId, CancellationToken cancellationToken) => SetActive(userId, true, cancellationToken);

    private async Task<IActionResult> SetActive(int userId, bool active, CancellationToken cancellationToken)
    {
        var selection = await ContextAsync(cancellationToken);
        if (selection.Error is not null) return selection.Error;
        var result = await users.SetActiveAsync(selection.Session, selection.TenantId, userId, active, cancellationToken);
        return result.Status == TenantUserStatus.Success
            ? Ok(new { message = active ? "User reactivated successfully" : "User deactivated successfully" }) : Error(result.Status);
    }

    [HttpPut("{userId:int}/reset-password")]
    public async Task<IActionResult> ResetPassword(int userId, [FromBody] PasswordInput? input, CancellationToken cancellationToken)
    {
        var selection = await ContextAsync(cancellationToken);
        if (selection.Error is not null) return selection.Error;
        if (!ModelState.IsValid || input is null) return InvalidRequest("body");
        if (input.Password is null) return InvalidRequest("body", "password");
        var result = await users.ResetPasswordAsync(selection.Session, selection.TenantId, userId, input.Password, cancellationToken);
        return result.Status == TenantUserStatus.Success ? Ok(new { message = "Password reset successfully" }) : Error(result.Status);
    }

    private async Task<(VerifiedSession? Session, int TenantId, IActionResult? Error)> ContextAsync(CancellationToken cancellationToken)
    {
        if (!options.Value.Enabled) return (null, 0, StatusCode(503, new { detail = "User management is unavailable" }));
        if (HttpContext.Items[typeof(VerifiedSession)] is not VerifiedSession session)
            return (null, 0, Unauthorized(new { detail = "Not authenticated" }));
        var selection = await users.ResolveSoleTenantAsync(session, cancellationToken);
        if (selection.Status != TenantUserStatus.Success || selection.TenantId is null)
            return (null, 0, Error(selection.Status));
        return (session, selection.TenantId.Value, null);
    }

    private static string? ConvertRole(string? role) => role switch
    {
        null or "" => "member",
        "admin" => "admin",
        _ => null
    };

    // Preserve the 422/detail-list shape without reflecting raw request bodies,
    // which may contain passwords, into responses or logs.
    private IActionResult InvalidRequest(string location, params string[] fields)
    {
        if (fields.Length == 0) fields = [""];
        return UnprocessableEntity(new { detail = fields.Select(field => new
        {
            type = "missing", loc = field.Length == 0 ? new[] { location } : new[] { location, field },
            msg = "Field required", input = "[redacted]"
        }) });
    }

    private IActionResult Error(TenantUserStatus status) => status switch
    {
        TenantUserStatus.Forbidden => StatusCode(403, new { detail = "Admin access required" }),
        TenantUserStatus.NotFound => NotFound(new { detail = "User not found" }),
        TenantUserStatus.Invalid => BadRequest(new { detail = "Invalid user details" }),
        TenantUserStatus.Conflict => BadRequest(new { detail = "Username unavailable" }),
        TenantUserStatus.SharedIdentity => Conflict(new { detail = "This account belongs to multiple tenants and needs separate administration" }),
        TenantUserStatus.AmbiguousTenant => Conflict(new { detail = "Tenant selection is required" }),
        _ => StatusCode(503, new { detail = "User management is unavailable" })
    };
}
