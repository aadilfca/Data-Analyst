using System.Data;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Data;

public sealed class PostgresReadSnapshot(
    PostgresDataSourceFactory factory, IOptions<PostgresOptions> options)
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
            try
            {
                await using var connection = await source.OpenConnectionAsync(cancellationToken);
                await using var transaction = await connection.BeginTransactionAsync(
                    IsolationLevel.RepeatableRead, cancellationToken);
                var context = new PostgresTransactionContext(connection, transaction);
                try
                {
                    await context.ConfigureAsync(options.Value.CommandTimeoutSeconds, readOnly: true, cancellationToken);
                    var result = await operation(context, cancellationToken);
                    cancellationToken.ThrowIfCancellationRequested();
                    await context.VerifyActiveAsync(readOnly: true, cancellationToken);
                    // Even success ends by rollback; the result is released only
                    // after the complete attempt and its cleanup have succeeded.
                    await transaction.RollbackAsync(cancellationToken);
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
        }
    }
}
