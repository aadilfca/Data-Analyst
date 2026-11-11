using System.ComponentModel.DataAnnotations;
using Npgsql;

namespace AuditPlatform.Api.Configuration;

public sealed class PostgresOptions : IValidatableObject
{
    public const string SectionName = "Postgres";

    public bool Enabled { get; set; }
    public string ConnectionString { get; set; } = "";
    public int MaximumPoolSize { get; set; } = 10;
    public int ConnectionTimeoutSeconds { get; set; } = 5;
    public int CommandTimeoutSeconds { get; set; } = 30;
    public int ReadinessTimeoutSeconds { get; set; } = 3;

    public IEnumerable<ValidationResult> Validate(ValidationContext validationContext)
    {
        // Disabled installations need no credentials and never build a pool.
        if (!Enabled)
            yield break;

        if (MaximumPoolSize is < 1 or > 100)
            yield return new("Postgres maximum pool size must be between 1 and 100.");
        if (ConnectionTimeoutSeconds is < 1 or > 30)
            yield return new("Postgres connection timeout must be between 1 and 30 seconds.");
        if (CommandTimeoutSeconds is < 1 or > 300)
            yield return new("Postgres command timeout must be between 1 and 300 seconds.");
        if (ReadinessTimeoutSeconds is < 1 or > 15)
            yield return new("Postgres readiness timeout must be between 1 and 15 seconds.");

        NpgsqlConnectionStringBuilder? connection = null;
        try
        {
            connection = new(ConnectionString);
        }
        catch (ArgumentException)
        {
            // Parser messages can contain supplied values. Never include them
            // in validation failures, which the host writes to startup logs.
        }

        if (connection is null || string.IsNullOrWhiteSpace(connection.Host)
            || string.IsNullOrWhiteSpace(connection.Database)
            || string.IsNullOrWhiteSpace(connection.Username))
        {
            yield return new("Postgres requires a valid connection string with host, database and username.");
        }
        else if (!IsLoopback(connection.Host) && connection.SslMode != SslMode.VerifyFull)
        {
            yield return new("Remote Postgres connections require SSL Mode=VerifyFull.");
        }
    }

    internal static bool IsLoopback(string host) =>
        host.Equals("localhost", StringComparison.OrdinalIgnoreCase)
        || host == "127.0.0.1" || host == "::1";
}
