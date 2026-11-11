using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Route("api/auth")]
public sealed class AuthController(PostgresSessionStore sessions, IOptions<AuthenticationOptions> options) : ControllerBase
{
    public sealed record LoginRequest(string Username, string Password);

    [HttpPost("login")]
    public async Task<IActionResult> Login([FromBody] LoginRequest request, CancellationToken cancellationToken)
    {
        if (!options.Value.Enabled) return StatusCode(503);
        var result = await sessions.LoginAsync(request.Username, request.Password, cancellationToken);
        if (result.Result == "invalid") return Unauthorized(new { detail = "Invalid username or password" });
        if (result.Result == "inactive") return StatusCode(403, new { detail = "Account is inactive" });
        Response.Headers.CacheControl = "no-store";
        Response.Cookies.Append("session_token", result.Token!, new CookieOptions
        {
            HttpOnly = true, SameSite = SameSiteMode.Lax, Secure = options.Value.PublicOrigin.StartsWith("https://", StringComparison.Ordinal),
            Path = "/", MaxAge = TimeSpan.FromHours(10)
        });
        return Ok(new { success = true, username = result.Username });
    }

    [HttpPost("logout")]
    public async Task<IActionResult> Logout(CancellationToken cancellationToken)
    {
        if (!options.Value.Enabled) return StatusCode(503);
        var token = Request.Cookies["session_token"];
        if (AuthenticationService.ValidToken(token))
            await sessions.LogoutAsync(token!, cancellationToken);
        Response.Headers.CacheControl = "no-store";
        Response.Cookies.Delete("session_token", new CookieOptions { Path = "/", HttpOnly = true,
            SameSite = SameSiteMode.Lax, Secure = options.Value.PublicOrigin.StartsWith("https://", StringComparison.Ordinal) });
        return Ok(new { success = true });
    }

    [HttpGet("current-client")]
    public IActionResult CurrentClient()
    {
        if (!options.Value.Enabled) return StatusCode(503);
        if (HttpContext.Items[typeof(VerifiedSession)] is not VerifiedSession session)
            return Unauthorized(new { detail = HttpContext.Items.ContainsKey("expired-session")
                ? "Session expired" : "Not authenticated" });
        Response.Headers.CacheControl = "no-store";
        return Ok(new { client_id = session.ClientId, client_name = session.ClientName,
            client_code = session.ClientCode, username = session.Username });
    }
}
