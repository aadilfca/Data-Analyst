using AuditPlatform.Api.Logging;

namespace AuditPlatform.Api.Middleware;

public sealed class ExceptionLoggingMiddleware(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, DiagnosticErrorLogger diagnostics)
    {
        try
        {
            await next(context);
        }
        catch (OperationCanceledException) when (context.RequestAborted.IsCancellationRequested)
        {
            // A disconnected/cancelled request is not a fabricated HTTP 500.
            context.Abort();
        }
        catch (Exception error) when (error is not OutOfMemoryException and not StackOverflowException)
        {
            var correlation = Guid.NewGuid().ToString("N");
            DiagnosticLogResult result;
            try
            {
                result = await diagnostics.LogAsync(diagnostics.Capture(error, context, correlation));
            }
            catch (Exception loggingError) when (loggingError is not OutOfMemoryException and not StackOverflowException)
            {
                result = new(null, DiagnosticErrorLogger.UnknownCode,
                    DiagnosticErrorLogger.GenericMessage, DiagnosticErrorLogger.GenericAction, "unavailable");
            }
            if (context.Response.HasStarted)
            {
                // Headers/body already sent cannot become a second JSON reply.
                // Avoid rethrowing raw diagnostics into another logging layer.
                context.Abort();
                return;
            }
            context.Response.Clear();
            context.Response.StatusCode = StatusCodes.Status500InternalServerError;
            context.Response.Headers.CacheControl = "no-store";
            try
            {
                await context.Response.WriteAsJsonAsync(new
                {
                    success = false,
                    error = new
                    {
                        error_code = result.Code,
                        error_id = result.ErrorId,
                        user_message = result.UserMessage,
                        suggested_action = result.SuggestedAction,
                        technical_details = (object?)null
                    }
                }, context.RequestAborted);
            }
            catch (Exception writeError) when (writeError is OperationCanceledException or IOException)
            {
                context.Abort();
            }
        }
    }
}
