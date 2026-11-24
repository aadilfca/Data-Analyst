using System.Diagnostics;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Data;

// For one materialized SELECT only. Related reads that need a stable snapshot
// and every mutation must continue through their existing transaction services.
public sealed class PostgresSingleStatementRead(
    PostgresDataSourceFactory factory, IOptions<PostgresOptions> options,
    ILogger<PostgresSingleStatementRead>? logger = null, IHostEnvironment? environment = null)
{
    public async Task<T> ExecuteAsync<T>(string operation,
        Func<PostgresSingleStatementReadContext, CancellationToken, Task<T>> read,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(read);
        cancellationToken.ThrowIfCancellationRequested();
        var source = factory.DataSource ?? throw new InvalidOperationException("PostgreSQL is not configured.");
        var started = Stopwatch.GetTimestamp();
        await using var connection = await source.OpenConnectionAsync(cancellationToken);
        var context = new PostgresSingleStatementReadContext(connection, options.Value.CommandTimeoutSeconds);
        try { return await read(context, cancellationToken); }
        finally
        {
            if (environment?.IsDevelopment() == true)
                logger?.LogInformation("C# PostgreSQL single read timing operation={Operation} elapsed_ms={ElapsedMs:F1}",
                    operation, Stopwatch.GetElapsedTime(started).TotalMilliseconds);
        }
    }
}

public sealed class PostgresSingleStatementReadContext(NpgsqlConnection connection, int commandTimeoutSeconds)
{
    public NpgsqlCommand CreateCommand(string sql) => new(sql, connection) { CommandTimeout = commandTimeoutSeconds };
}
