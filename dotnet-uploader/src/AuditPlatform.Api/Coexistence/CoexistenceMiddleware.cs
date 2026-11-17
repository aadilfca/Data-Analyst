using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Coexistence;

public sealed class CoexistenceMiddleware(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, IOptions<CoexistenceOptions> settings,
        CoexistenceRouteTable routes, ICoexistenceAccessGate gate, LegacyForwarder forwarder)
    {
        if (!settings.Value.Enabled) { await next(context); return; }
        var path = context.Request.Path.Value ?? "/";
        var route = routes.Find(context.Request.Method, path);
        if (route is null)
        {
            var methods = routes.AllowedMethods(path);
            if (methods is null) { await next(context); return; }
            context.Response.Headers.Allow = methods;
            await LegacyForwarder.RejectAsync(context, 405, "method_not_owned", "not_sent");
            return;
        }
        var raw = context.Features.Get<IHttpRequestFeature>()?.RawTarget;
        // TestServer may omit RawTarget; Kestrel supplies the original wire
        // target. In either case the trusted parsed path must be canonical.
        var rawPath = string.IsNullOrEmpty(raw) ? path : raw.Split('?', 2)[0];
        // Reject alternate encodings/path bases rather than forwarding a
        // differently interpreted route to another HTTP server.
        if (context.Request.PathBase.HasValue || rawPath != path || !CoexistenceRouteTable.MatchesPath(route, path))
        {
            await LegacyForwarder.RejectAsync(context, 400, "ambiguous_path", "not_sent");
            return;
        }
        var decision = await gate.AuthorizeAsync(context, route, context.RequestAborted);
        if (decision != CoexistenceAccessDecision.Allowed)
        {
            var (status, reason) = decision switch
            {
                CoexistenceAccessDecision.Unauthenticated => (401, "authentication_required"),
                CoexistenceAccessDecision.Forbidden => (403, "access_denied"),
                _ => (503, "access_not_ready")
            };
            await LegacyForwarder.RejectAsync(context, status, reason, "not_sent");
            return;
        }
        if (route.Owner == CoexistenceOwner.DotNet) await next(context);
        else await forwarder.ForwardAsync(context, route);
    }
}
