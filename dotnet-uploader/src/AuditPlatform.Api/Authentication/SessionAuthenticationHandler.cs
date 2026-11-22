using System.Security.Claims;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Authentication;

public sealed class SessionAuthenticationHandler(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, AuthenticationService authentication,
        IOptions<AuthenticationOptions> options)
    {
        var shell=context.RequestServices.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled;
        var isProtectedSurface = context.Request.Path.StartsWithSegments("/api/auth")
            || context.Request.Path.StartsWithSegments("/api/data")
            || context.Request.Path.StartsWithSegments("/api/trial-balance")
            || context.Request.Path.StartsWithSegments("/api/account-control-center")
            || context.Request.Path.StartsWithSegments("/api/data-integrity")
            || context.Request.Path.StartsWithSegments("/api/storage-cleanup")
            || context.Request.Path.StartsWithSegments("/api/dashboard")
            || context.Request.Path.StartsWithSegments("/api/error-logs")
            || context.Request.Path.StartsWithSegments("/api/users")
            || context.Request.Path.StartsWithSegments("/api/clients")
            || context.Request.Path.StartsWithSegments("/api/settings")
            || context.Request.Path.StartsWithSegments("/api/columns")
            || context.Request.Path.StartsWithSegments("/api/templates")
            || context.Request.Path.StartsWithSegments("/api/backup")
            || context.Request.Path.StartsWithSegments("/api/database-backup")
            || context.Request.Path.StartsWithSegments("/api/reconciliation")
            || context.Request.Path.StartsWithSegments("/api/audit-rules")
            || context.Request.Path.StartsWithSegments("/api/verification")
            || context.Request.Path.StartsWithSegments("/api/import-excel")
            || context.Request.Path.StartsWithSegments("/api/upload/staging")
            || context.Request.Path.StartsWithSegments("/api/upload/file")
            || context.Request.Path.StartsWithSegments("/api/upload/save-mapping")
            || context.Request.Path.StartsWithSegments("/api/upload/saved-mapping")
            || (shell && (context.Request.Path=="/" || context.Request.Path.Value?.EndsWith(".html",StringComparison.Ordinal)==true));
        if (options.Value.Enabled && isProtectedSurface && context.Request.Cookies.ContainsKey("session_token"))
        {
            var session = await authentication.ResolveAsync(context, context.RequestAborted);
            if (session is not null)
            {
                context.Items[typeof(VerifiedSession)] = session;
                var claims = new List<Claim> { new(ClaimTypes.NameIdentifier, session.UserId.ToString()),
                    new(ClaimTypes.Name, session.Username) };
                if (session.Role is not null) claims.Add(new(ClaimTypes.Role, session.Role));
                context.User = new ClaimsPrincipal(new ClaimsIdentity(claims, "shared-session"));
            }
            else if (context.Request.Cookies["session_token"] is string token
                && AuthenticationService.ValidToken(token)
                && await authentication.IsExpiredAsync(token, context.RequestAborted))
                context.Items["expired-session"] = true;
        }
        await next(context);
    }
}
