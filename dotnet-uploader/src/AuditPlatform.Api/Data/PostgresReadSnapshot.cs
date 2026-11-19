using System.Data;
using System.Diagnostics;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Data;

public sealed class PostgresReadSnapshot(
    PostgresDataSourceFactory factory, IOptions<PostgresOptions> options,
    ILogger<PostgresReadSnapshot>? logger = null, IHostEnvironment? environment = null)
{
    // Return fully materialized input, never a live reader, iterator or stream.
    // The callback can run twice; publish nothing and perform no external side
    // effects until this method succeeds. A retry obtains a new snapshot.
    public async Task<T> ExecuteAsync<T>(
        Func<PostgresTransactionContext, CancellationToken, Task<T>> operation,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(operation);
        cancellationToken.ThrowIfCancellationRequested();
        using var scope = PostgresOperationScope.Enter();
        var source = factory.DataSource
            ?? throw new InvalidOperationException("PostgreSQL is not configured.");

        for (var attempt = 0; ; attempt++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var stageStarted = Stopwatch.GetTimestamp();
            var connectionOpenMs = 0d;
            var setupMs = 0d;
            var callbackMs = 0d;
            var verificationMs = 0d;
            var cleanupMs = 0d;
            var outcome = "failed";
            try
            {
                await using var connection = await source.OpenConnectionAsync(cancellationToken);
                connectionOpenMs = Stopwatch.GetElapsedTime(stageStarted).TotalMilliseconds;
                stageStarted = Stopwatch.GetTimestamp();
                await using var transaction = await connection.BeginTransactionAsync(
                    IsolationLevel.RepeatableRead, cancellationToken);
                var context = new PostgresTransactionContext(connection, transaction);
                try
                {
                    await context.ConfigureAsync(options.Value.CommandTimeoutSeconds, readOnly: true, cancellationToken);
                    setupMs = Stopwatch.GetElapsedTime(stageStarted).TotalMilliseconds;
                    stageStarted = Stopwatch.GetTimestamp();
                    var result = await operation(context, cancellationToken);
                    callbackMs = Stopwatch.GetElapsedTime(stageStarted).TotalMilliseconds;
                    cancellationToken.ThrowIfCancellationRequested();
                    stageStarted = Stopwatch.GetTimestamp();
                    await context.VerifyActiveAsync(readOnly: true, cancellationToken);
                    verificationMs = Stopwatch.GetElapsedTime(stageStarted).TotalMilliseconds;
                    // Even success ends by rollback; the result is released only
                    // after the complete attempt and its cleanup have succeeded.
                    stageStarted = Stopwatch.GetTimestamp();
                    await transaction.RollbackAsync(cancellationToken);
                    cleanupMs = Stopwatch.GetElapsedTime(stageStarted).TotalMilliseconds;
                    outcome = "success";
                    return result;
                }
                catch
                {
                    await PostgresOperationScope.RollbackSafelyAsync(transaction);
                    throw;
                }
            }
            catch (Exception error) when (attempt == 0 && !cancellationToken.IsCancellationRequested
                && PostgresOperationScope.IsConnectionFailure(error))
            {
                // Only a recognized broken connection gets one whole-read retry.
                // No retries for SQL errors, lock/command timeouts or cancellation.
            }
            finally
            {
                if (environment?.IsDevelopment() == true)
                    logger?.LogInformation(
                        "C# PostgreSQL read snapshot timing trace_id={TraceId} outcome={Outcome} attempt={Attempt} connection_open_ms={ConnectionOpenMs:F1} setup_ms={SetupMs:F1} callback_ms={CallbackMs:F1} verification_ms={VerificationMs:F1} cleanup_ms={CleanupMs:F1}",
                        Activity.Current?.TraceId.ToString() ?? "none", outcome, attempt + 1,
                        connectionOpenMs, setupMs, callbackMs, verificationMs, cleanupMs);
            }
        }
    }
}
