using System.Security.Claims;
using AuditPlatform.Api.Coexistence;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Authentication;

public sealed class SessionAuthenticationHandler(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, AuthenticationService authentication,
        IOptions<AuthenticationOptions> options)
    {
        var routes = context.RequestServices.GetRequiredService<CoexistenceRouteTable>();
        var isProtectedSurface = context.Request.Path.StartsWithSegments("/api/auth")
            || context.Request.Path.StartsWithSegments("/api/users")
            || context.Request.Path.StartsWithSegments("/api/clients")
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

public sealed class SessionCoexistenceGate(IOptions<AuthenticationOptions> options) : ICoexistenceAccessGate
{
    public ValueTask<CoexistenceAccessDecision> AuthorizeAsync(HttpContext context,
        CoexistenceRoute route, CancellationToken cancellationToken)
    {
        if (!options.Value.Enabled) return ValueTask.FromResult(CoexistenceAccessDecision.Unavailable);
        if (route.Access == CoexistenceAccess.Public) return ValueTask.FromResult(CoexistenceAccessDecision.Allowed);
        if (context.Items[typeof(VerifiedSession)] is not VerifiedSession session)
            return ValueTask.FromResult(CoexistenceAccessDecision.Unauthenticated);
        if (route.Access == CoexistenceAccess.Client)
            return ValueTask.FromResult(CoexistenceAccessDecision.Forbidden); // Object scope needs a route-specific policy.
        if (route.Access == CoexistenceAccess.Administrator && session.Role != "admin")
            return ValueTask.FromResult(CoexistenceAccessDecision.Forbidden);
        return ValueTask.FromResult(CoexistenceAccessDecision.Allowed);
    }
}
