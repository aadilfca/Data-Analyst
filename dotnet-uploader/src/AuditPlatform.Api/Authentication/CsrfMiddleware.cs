using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Authentication;

// Applied only to explicitly selected operations once P07's gate is wired.
// The comparison uses an operator-configured origin, never a request Host.
public sealed class CsrfMiddleware(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, IOptions<AuthenticationOptions> options)
    {
        if (HttpMethods.IsGet(context.Request.Method) || HttpMethods.IsHead(context.Request.Method))
        {
            await next(context);
            return;
        }

        var origins = context.Request.Headers.Origin;
        var expected = options.Value.PublicOrigin;
        if (expected.Length == 0 || origins.Count != 1
            || !string.Equals(origins[0], expected, StringComparison.Ordinal))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        await next(context);
    }
}
