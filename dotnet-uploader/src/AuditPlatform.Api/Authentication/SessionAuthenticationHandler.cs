using System.Security.Claims;
using AuditPlatform.Api.Coexistence;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using AuditPlatform.Api.Tenancy;

namespace AuditPlatform.Api.Authentication;

public sealed class SessionAuthenticationHandler(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, AuthenticationService authentication,
        IOptions<AuthenticationOptions> options)
    {
        var routes = context.RequestServices.GetRequiredService<CoexistenceRouteTable>();
        var shell=context.RequestServices.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled;
        var isProtectedSurface = context.Request.Path.StartsWithSegments("/api/auth")
            || context.Request.Path.StartsWithSegments("/api/users")
            || context.Request.Path.StartsWithSegments("/api/clients")
            || context.Request.Path.StartsWithSegments("/api/settings")
            || context.Request.Path.StartsWithSegments("/api/columns")
            || context.Request.Path.StartsWithSegments("/api/templates")
            || context.Request.Path.StartsWithSegments("/api/backup")
            || context.Request.Path.StartsWithSegments("/api/upload/save-mapping")
            || context.Request.Path.StartsWithSegments("/api/upload/saved-mapping")
            || (shell && (context.Request.Path=="/" || context.Request.Path.Value?.EndsWith(".html",StringComparison.Ordinal)==true))
            || routes.Find(context.Request.Method, context.Request.Path.Value ?? "") is not null;
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

public sealed class SessionCoexistenceGate(IOptions<AuthenticationOptions> options, IOptions<ApplicationShellOptions>? shell = null) : ICoexistenceAccessGate
{
    public ValueTask<CoexistenceAccessDecision> AuthorizeAsync(HttpContext context,
        CoexistenceRoute route, CancellationToken cancellationToken)
    {
        if (!options.Value.Enabled) return ValueTask.FromResult(CoexistenceAccessDecision.Unavailable);
        if (route.Access == CoexistenceAccess.Public) return ValueTask.FromResult(CoexistenceAccessDecision.Allowed);
        // Legacy routes do not yet enforce selected-tenant context. A shell
        // session cannot inherit global-role bridge privileges.
        if (shell?.Value.Enabled==true) return ValueTask.FromResult(CoexistenceAccessDecision.Forbidden);
        if (context.Items[typeof(VerifiedSession)] is not VerifiedSession session)
            return ValueTask.FromResult(CoexistenceAccessDecision.Unauthenticated);
        if (route.Access == CoexistenceAccess.Client)
            return ValueTask.FromResult(CoexistenceAccessDecision.Forbidden); // Object scope needs a route-specific policy.
        if (route.Access == CoexistenceAccess.Administrator && session.Role != "admin")
            return ValueTask.FromResult(CoexistenceAccessDecision.Forbidden);
        return ValueTask.FromResult(CoexistenceAccessDecision.Allowed);
    }
}
