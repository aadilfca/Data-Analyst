using System.Security.Claims;
using AuditPlatform.Api.Coexistence;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Users;

namespace AuditPlatform.Api.Authentication;

public sealed class SessionAuthenticationHandler(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, AuthenticationService authentication,
        IOptions<AuthenticationOptions> options)
    {
        var routes = context.RequestServices.GetRequiredService<CoexistenceRouteTable>();
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
            || context.Request.Path.StartsWithSegments("/api/reconciliation")
            || context.Request.Path.StartsWithSegments("/api/audit-rules")
            || context.Request.Path.StartsWithSegments("/api/verification")
            || context.Request.Path.StartsWithSegments("/api/import-excel")
            || context.Request.Path.StartsWithSegments("/api/upload/staging")
            || context.Request.Path.StartsWithSegments("/api/upload/file")
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

public sealed class SessionCoexistenceGate(IOptions<AuthenticationOptions> options, IOptions<ApplicationShellOptions>? shell = null,
    TenantUserService? tenantUsers = null) : ICoexistenceAccessGate
{
    public async ValueTask<CoexistenceAccessDecision> AuthorizeAsync(HttpContext context,
        CoexistenceRoute route, CancellationToken cancellationToken)
    {
        if (!options.Value.Enabled) return CoexistenceAccessDecision.Unavailable;
        if (route.Access == CoexistenceAccess.Public) return CoexistenceAccessDecision.Allowed;
        if (context.Items[typeof(VerifiedSession)] is not VerifiedSession session)
            return CoexistenceAccessDecision.Unauthenticated;
        bool selectedClientReport = route.Owner == CoexistenceOwner.DotNet
            && (route.Access is CoexistenceAccess.Authenticated or CoexistenceAccess.Administrator)
            && (route.Path.StartsWith("/api/reconciliation/financial-statements/", StringComparison.Ordinal)
                || route.Path.StartsWith("/api/reconciliation/cashflows/", StringComparison.Ordinal)
                || route.Path.StartsWith("/api/reconciliation/financial-reports/", StringComparison.Ordinal));
        bool selectedClientVerification = route.Owner == CoexistenceOwner.DotNet
            && route.Access == CoexistenceAccess.Authenticated
            && route.Path.StartsWith("/api/verification/", StringComparison.Ordinal);
        bool auditRules = route.Path.StartsWith("/api/audit-rules/", StringComparison.Ordinal)
            && route.Access == CoexistenceAccess.Administrator;
        bool tenantAdmin = auditRules && route.Owner == CoexistenceOwner.DotNet && tenantUsers is not null
            && (await tenantUsers.ResolveSoleTenantAsync(session, cancellationToken)).Status == TenantUserStatus.Success;
        bool legacyAdmin = auditRules && route.Owner == CoexistenceOwner.Legacy && session.Role == "admin";
        // These report routes repeat tenant, selected-client, membership and
        // administrator checks inside their scoped PostgreSQL snapshot/job store.
        // Other bridge routes retain the shell-era block.
        bool workspaceRead = route.Owner == CoexistenceOwner.DotNet && route.Access == CoexistenceAccess.Authenticated
            && (route.Path.StartsWith("/api/dashboard/",StringComparison.Ordinal) || route.Path.StartsWith("/api/error-logs",StringComparison.Ordinal));
        if (shell?.Value.Enabled==true && !workspaceRead && !selectedClientReport && !selectedClientVerification && !tenantAdmin && !legacyAdmin)
            return CoexistenceAccessDecision.Forbidden;
        if (route.Access == CoexistenceAccess.Client)
            return CoexistenceAccessDecision.Forbidden; // Object scope needs a route-specific policy.
        if (route.Access == CoexistenceAccess.Administrator)
        {
            if (auditRules && route.Owner == CoexistenceOwner.DotNet && !tenantAdmin)
                return CoexistenceAccessDecision.Forbidden;
            if (!(auditRules && route.Owner == CoexistenceOwner.DotNet) && !tenantAdmin && session.Role != "admin")
                return CoexistenceAccessDecision.Forbidden;
        }
        return CoexistenceAccessDecision.Allowed;
    }
}
