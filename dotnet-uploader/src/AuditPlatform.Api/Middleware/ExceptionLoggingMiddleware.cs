using System.Diagnostics;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Hosting;
using Microsoft.AspNetCore.Routing;
using Npgsql;

namespace AuditPlatform.Api.Middleware;

public sealed class ExceptionLoggingMiddleware(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, DiagnosticErrorLogger diagnostics,
        ILogger<ExceptionLoggingMiddleware> logger, IHostEnvironment environment)
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
            if (environment.IsDevelopment())
            {
                var (exceptionType, sqlState) = FailureDetails(error);
                var route = context.GetEndpoint() is RouteEndpoint endpoint
                    ? endpoint.RoutePattern.RawText ?? "unmatched" : "unmatched";
                var method = context.Request.Method is "GET" or "HEAD" or "POST" or "PUT" or "PATCH" or "DELETE" or "OPTIONS"
                    ? context.Request.Method : "OTHER";
                logger.LogError(new EventId(5000, "UnhandledRequest"),
                    "C# unhandled request trace_id={TraceId} correlation_id={CorrelationId} method={Method} route={Route} exception_type={ExceptionType} postgres_sqlstate={PostgresSqlState}",
                    Activity.Current?.TraceId.ToString() ?? context.TraceIdentifier, correlation, method, route,
                    exceptionType, sqlState ?? "none");
            }
            DiagnosticLogResult result;
            try
            {
                result = await diagnostics.LogAsync(diagnostics.Capture(error, context, correlation));
            }
            catch (Exception loggingError) when (loggingError is not OutOfMemoryException and not StackOverflowException)
            {
                if (environment.IsDevelopment())
                {
                    var (exceptionType, sqlState) = FailureDetails(loggingError);
                    logger.LogWarning(new EventId(5001, "DiagnosticPersistence"),
                        "C# diagnostic persistence call failed correlation_id={CorrelationId} exception_type={ExceptionType} postgres_sqlstate={PostgresSqlState}",
                        correlation, exceptionType, sqlState ?? "none");
                }
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

    private static (string Type, string? SqlState) FailureDetails(Exception error)
    {
        var current = error;
        for (var depth = 0; depth < 8; depth++)
        {
            if (current is PostgresException postgres)
                return (error.GetType().Name, postgres.SqlState);
            if (current.InnerException is null) break;
            current = current.InnerException;
        }
        return (error.GetType().Name, null);
    }
}
