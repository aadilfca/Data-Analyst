using System.Diagnostics;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Routing;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Logging;

public sealed class DiagnosticErrorLogger(
    PostgresTransactions transactions, PostgresDataSourceFactory factory,
    LogSanitizer sanitizer, IOptions<LogPersistenceOptions> options,
    ILogger<DiagnosticErrorLogger> console)
{
    public const string UnknownCode = "ERR-UNK-001";
    public const string GenericMessage = "An unexpected error occurred. Please try again or contact support.";
    public const string GenericAction = "Please report this error using the Report Error button.";

    public DiagnosticError Capture(Exception error, HttpContext context, string correlationId)
    {
        LogActor actor;
        try { actor = LogActor.FromPrincipal(context.User); }
        catch (InvalidOperationException) { actor = LogActor.Anonymous; }
        var scope = context.Items[typeof(AuthorizedLogScope)] as AuthorizedLogScope;
        var descriptor = context.GetEndpoint()?.Metadata.GetMetadata<ControllerActionDescriptor>();
        var endpoint = context.GetEndpoint() is RouteEndpoint route ? route.RoutePattern.RawText : null;
        var method = context.Request.Method is "GET" or "HEAD" or "POST" or "PUT" or "PATCH" or "DELETE" or "OPTIONS"
            ? context.Request.Method : "OTHER";
        var category = error is NpgsqlException or PostgresCommitOutcomeUnknownException ? "Database"
            : error is IOException or TimeoutException ? "Network" : "System";
        // Rebuild symbols without exception messages, argument values, source
        // snippets, paths, headers, cookies, query strings or request bodies.
        var root = error;
        for (var i = 0; i < 8 && root.InnerException is not null; i++) root = root.InnerException;
        var frames = new StackTrace(root, fNeedFileInfo: false).GetFrames() ?? [];
        var trace = string.Join('\n', frames.Reverse().Select(frame => frame.GetMethod())
            .Where(methodInfo => methodInfo is not null)
            .Select(methodInfo => methodInfo!.DeclaringType?.FullName + "." + methodInfo.Name));
        trace += "\n" + root.GetType().Name;
        return new(Guid.NewGuid(), correlationId, actor, scope?.ClientId,
            descriptor?.ControllerName ?? "Request", endpoint is null ? "unmatched" : "/" + endpoint.TrimStart('/'), method, 500,
            category, "Unhandled " + root.GetType().Name + ".", sanitizer.Text(trace, 5000, tail: true), null,
            error is PostgresCommitOutcomeUnknownException, scope?.OperationId);
    }

    public async Task<DiagnosticLogResult> LogAsync(DiagnosticError record, CancellationToken cancellationToken = default)
    {
        var action = record.OutcomeUnknown ? "Check the operation status before retrying; the change may already have completed." : GenericAction;
        var fallback = new DiagnosticLogResult(null, UnknownCode, GenericMessage, action, "unavailable");
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        budget.CancelAfter(TimeSpan.FromSeconds(options.Value.DiagnosticTimeoutSeconds));
        try
        {
            Validate(record);
            if (factory.DataSource is null)
                return Fallback(record, fallback, "not_configured");
            var metadata = sanitizer.Metadata(record.Metadata);
            return await transactions.ExecuteAsync(async (context, token) =>
            {
                int? registryId = null;
                var message = GenericMessage;
                var suggestion = action;
                await using (var registry = context.CreateCommand("""
                    SELECT id,user_message,suggested_action FROM public.error_codes
                    WHERE code=$1 AND is_active IS TRUE
                    """))
                {
                    registry.Parameters.AddWithValue(UnknownCode);
                    await using var reader = await registry.ExecuteReaderAsync(token);
                    if (await reader.ReadAsync(token))
                    {
                        registryId = reader.GetInt32(0);
                        message = SafeRegistryText(reader.GetString(1), GenericMessage, 1000);
                        if (!record.OutcomeUnknown && !reader.IsDBNull(2))
                            suggestion = SafeRegistryText(reader.GetString(2), GenericAction, 500);
                    }
                }
                await using var command = context.CreateCommand("""
                    INSERT INTO public.error_log(error_code_id,error_code,user_id,client_id,module,endpoint,
                        method,status_code,error_category,error_message,stack_trace,input_data,suggested_fix,
                        event_id,operation_id,actor_user_id,actor_kind,correlation_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) RETURNING id
                    """);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Integer, registryId);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, UnknownCode);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, sanitizer.Text(record.Actor.Name, 256));
                PostgresAuditWriter.Add(command, NpgsqlDbType.Integer, record.ClientId);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, sanitizer.Label(record.Module));
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, sanitizer.Text(record.Endpoint, 500));
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, sanitizer.Label(record.Method, 16));
                PostgresAuditWriter.Add(command, NpgsqlDbType.Integer, record.StatusCode);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, sanitizer.Label(record.Category));
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, sanitizer.Text(record.SafeMessage, 1000));
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, record.SafeTrace is null ? null : sanitizer.Text(record.SafeTrace, 5000, tail: true));
                PostgresAuditWriter.Add(command, NpgsqlDbType.Jsonb, metadata);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, suggestion);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Uuid, record.EventId);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Uuid, record.OperationId);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Integer, record.Actor.UserId);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, record.Actor.Kind);
                PostgresAuditWriter.Add(command, NpgsqlDbType.Text, record.CorrelationId);
                var id = (int)(await command.ExecuteScalarAsync(token))!;
                return new DiagnosticLogResult(id, UnknownCode, message, suggestion, "persisted");
            }, budget.Token);
        }
        catch (Exception error) when (error is not OutOfMemoryException and not StackOverflowException)
        {
            return Fallback(record, fallback, error is PostgresCommitOutcomeUnknownException ? "unknown" : "unavailable");
        }
    }

    private void Validate(DiagnosticError record)
    {
        if (record.EventId == Guid.Empty || record.StatusCode is < 100 or > 599 || record.ClientId is <= 0)
            throw new ArgumentException("Diagnostic record identity or scope is invalid.");
        sanitizer.ValidateActor(record.Actor);
        _ = sanitizer.Label(record.CorrelationId, 64);
        _ = sanitizer.Label(record.Module);
        _ = sanitizer.Label(record.Method, 16);
        _ = sanitizer.Label(record.Category);
    }

    private string SafeRegistryText(string value, string fallback, int maximum)
    {
        var safe = sanitizer.Text(value, maximum);
        return string.IsNullOrWhiteSpace(safe) || safe == LogSanitizer.Redacted ? fallback : safe;
    }

    private DiagnosticLogResult Fallback(DiagnosticError record, DiagnosticLogResult result, string status)
    {
        // Fixed metadata only. Never pass the exception object or supplied
        // strings into a logger, including its failure/formatting paths.
        try
        {
            console.LogWarning(new EventId(5001, "DiagnosticPersistence"),
                "Diagnostic persistence {Persistence}; event {EventId}; status {StatusCode}",
                status, record.EventId, record.StatusCode);
        }
        catch (Exception error) when (error is not OutOfMemoryException and not StackOverflowException) { }
        return result with { Persistence = status };
    }
}
