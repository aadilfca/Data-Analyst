using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Health;

public sealed class PostgresReadinessCheck(
    PostgresDataSourceFactory factory, IOptions<PostgresOptions> options)
{
    public async Task<string?> CheckAsync(CancellationToken cancellationToken)
    {
        if (factory.DataSource is not { } dataSource)
            return "database_not_configured";

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.ReadinessTimeoutSeconds));

        try
        {
            await using var connection = await dataSource.OpenConnectionAsync(deadline.Token);
            await using var command = new NpgsqlCommand("SELECT 1", connection)
            {
                CommandTimeout = options.Value.ReadinessTimeoutSeconds
            };
            var value = await command.ExecuteScalarAsync(deadline.Token);
            return value is 1 ? null : "database_unavailable";
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return "database_timeout";
        }
        catch (NpgsqlException) when (!cancellationToken.IsCancellationRequested)
        {
            return deadline.IsCancellationRequested ? "database_timeout" : "database_unavailable";
        }
        catch (TimeoutException) when (!cancellationToken.IsCancellationRequested)
        {
            return "database_timeout";
        }
    }
}
