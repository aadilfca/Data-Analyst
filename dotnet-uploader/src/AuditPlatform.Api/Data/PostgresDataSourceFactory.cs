using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Data;

// DI owns this singleton and disposes its one pool when the host shuts down.
// Building the data source opens no connections and performs no schema work.
public sealed class PostgresDataSourceFactory : IDisposable, IAsyncDisposable
{
    public NpgsqlDataSource? DataSource { get; }

    public PostgresDataSourceFactory(IOptions<PostgresOptions> options)
    {
        var settings = options.Value;
        if (!settings.Enabled)
            return;

        var connection = new NpgsqlConnectionStringBuilder(settings.ConnectionString)
        {
            Pooling = true,
            MinPoolSize = 0,
            MaxPoolSize = settings.MaximumPoolSize,
            Timeout = settings.ConnectionTimeoutSeconds,
            CommandTimeout = settings.CommandTimeoutSeconds,
            CancellationTimeout = 1000,
            PersistSecurityInfo = false,
            IncludeErrorDetail = false,
            LogParameters = false,
            NoResetOnClose = false,
            Multiplexing = false,
            ApplicationName = "AuditPlatform.Api"
        };

        // No logger/parameter logging is attached to the driver. Public health
        // results never include server messages, SQL, credentials or addresses.
        DataSource = new NpgsqlDataSourceBuilder(connection.ConnectionString).Build();
    }

    public void Dispose() => DataSource?.Dispose();
    public ValueTask DisposeAsync() => DataSource?.DisposeAsync() ?? ValueTask.CompletedTask;
}
