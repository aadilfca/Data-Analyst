using System.Data;
using System.Globalization;
using System.Net.Sockets;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Data;

public sealed class PostgresTransactions(
    PostgresDataSourceFactory factory, IOptions<PostgresOptions> options)
{
    // The callback owns SQL only. Await all nested work using this context;
    // it must not commit, roll back, dispose, or replace the supplied resources.
    public async Task<T> ExecuteAsync<T>(
        Func<PostgresTransactionContext, CancellationToken, Task<T>> operation,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(operation);
        cancellationToken.ThrowIfCancellationRequested();
        using var scope = PostgresOperationScope.Enter();
        var source = factory.DataSource
            ?? throw new InvalidOperationException("PostgreSQL is not configured.");
        await using var connection = await source.OpenConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(
            IsolationLevel.ReadCommitted, cancellationToken);
        var context = new PostgresTransactionContext(connection, transaction);
        var commitStarted = false;

        try
        {
            await context.ConfigureAsync(options.Value.CommandTimeoutSeconds, readOnly: false, cancellationToken);
            var result = await operation(context, cancellationToken);
            cancellationToken.ThrowIfCancellationRequested();
            await context.VerifyActiveAsync(readOnly: false, cancellationToken);
            commitStarted = true;
            await transaction.CommitAsync(cancellationToken);
            return result;
        }
        catch (Exception error)
        {
            await PostgresOperationScope.RollbackSafelyAsync(transaction);
            // A server SQL rejection is definite; a missing transport response
            // or cancellation during COMMIT cannot establish whether it applied.
            if (commitStarted && (error is not PostgresException
                || PostgresOperationScope.IsConnectionFailure(error)))
                throw new PostgresCommitOutcomeUnknownException(error);
            throw;
        }
    }
}

public sealed class PostgresCommitOutcomeUnknownException(Exception innerException)
    : Exception("Commit outcome is unknown. Reconcile the operation before retrying; it may already have committed.", innerException);

public sealed class PostgresTransactionContext(NpgsqlConnection connection, NpgsqlTransaction transaction)
{
    // Exposed for approved bulk/COPY/savepoint work. These are borrowed, never
    // independently owned. Commands must use the supplied cancellation token.
    public NpgsqlConnection Connection { get; } = connection;
    public NpgsqlTransaction Transaction { get; } = transaction;

    public NpgsqlCommand CreateCommand(string sql) => new(sql, Connection, Transaction);

    internal async Task ConfigureAsync(int timeoutSeconds, bool readOnly, CancellationToken cancellationToken)
    {
        if (readOnly)
        {
            await using var mode = CreateCommand("SET TRANSACTION READ ONLY");
            await mode.ExecuteNonQueryAsync(cancellationToken);
        }

        // Server-side limits supplement Npgsql's client timeout. SET LOCAL
        // semantics avoid leaking these limits to the next pooled borrower.
        await using var limits = CreateCommand(
            "SELECT set_config('statement_timeout', $1, true), set_config('lock_timeout', $2, true)");
        limits.Parameters.AddWithValue((timeoutSeconds * 1000).ToString(CultureInfo.InvariantCulture));
        limits.Parameters.AddWithValue((Math.Min(timeoutSeconds, 5) * 1000).ToString(CultureInfo.InvariantCulture));
        await limits.ExecuteNonQueryAsync(cancellationToken);
    }

    internal async Task VerifyActiveAsync(bool readOnly, CancellationToken cancellationToken)
    {
        if (Transaction.Connection != Connection)
            throw new InvalidOperationException("The callback ended its supplied transaction.");

        // A callback that swallows a SQL error leaves PostgreSQL's transaction
        // aborted. Detect that before COMMIT can silently become ROLLBACK.
        await using var command = CreateCommand(
            "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')");
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        await reader.ReadAsync(cancellationToken);
        if (readOnly && (reader.GetString(0) != "repeatable read" || reader.GetString(1) != "on"))
            throw new InvalidOperationException("The callback changed its read snapshot settings.");
    }
}

// Scope tracking rejects another root operation in the same async call chain.
// Nested services receive the existing context; independent requests remain free
// to execute concurrently. This does not authorize untrusted SQL callbacks.
internal sealed class PostgresOperationScope : IDisposable
{
    private static readonly AsyncLocal<PostgresOperationScope?> Current = new();

    public static PostgresOperationScope Enter()
    {
        if (Current.Value is not null)
            throw new InvalidOperationException("Reuse the supplied PostgreSQL context instead of starting a nested operation.");
        var scope = new PostgresOperationScope();
        Current.Value = scope;
        return scope;
    }

    public void Dispose() => Current.Value = null;

    internal static bool IsConnectionFailure(Exception error) => error switch
    {
        PostgresException server => server.SqlState.StartsWith("08", StringComparison.Ordinal)
            || server.SqlState is "57P01" or "57P02" or "57P03",
        NpgsqlException { InnerException: IOException or SocketException } => true,
        _ => false
    };

    internal static async Task RollbackSafelyAsync(NpgsqlTransaction transaction)
    {
        // Cleanup must still run when the request's token has been cancelled.
        using var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try
        {
            await transaction.RollbackAsync(cleanup.Token);
        }
        catch (Exception error) when (error is NpgsqlException or InvalidOperationException
            or OperationCanceledException or TimeoutException)
        {
            // Retain the operation's original failure. Npgsql disposes broken
            // connections rather than returning their physical socket to use.
        }
    }
}
