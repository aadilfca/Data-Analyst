using System.Net;
using Npgsql;

namespace AuditPlatform.LogRehearsal;

public sealed class RehearsalTarget : IAsyncDisposable
{
    internal NpgsqlDataSource Source { get; }
    public string OwnerDirectory { get; }
    private readonly string database;

    private RehearsalTarget(NpgsqlDataSource source, string ownerDirectory, string database)
    {
        Source = source;
        OwnerDirectory = ownerDirectory;
        this.database = database;
    }

    public static async Task<RehearsalTarget> OpenAsync(string ownerDirectory, string connectionString,
        CancellationToken cancellationToken = default)
    {
        var owner = ValidateOwner(ownerDirectory);
        var supplied = new NpgsqlConnectionStringBuilder(connectionString);
        const string prefix = "auditplatform_foundation_";
        var name = supplied.Database ?? "";
        if (supplied.Host != "127.0.0.1" || supplied.Username != "auditplatform_test"
            || supplied.Port is < 1024 or > 65535 || !name.StartsWith(prefix, StringComparison.Ordinal)
            || !Guid.TryParseExact(name[prefix.Length..], "N", out _) || string.IsNullOrWhiteSpace(supplied.Password))
            throw new InvalidOperationException("Rehearsal requires a dedicated loopback fixture database.");

        // Copy only the necessary destination/credential values. Never inherit
        // supplied startup commands, passfiles, search paths or logging switches.
        var settings = new NpgsqlConnectionStringBuilder
        {
            Host = "127.0.0.1", Port = supplied.Port, Database = name,
            Username = "auditplatform_test", Password = supplied.Password,
            SslMode = SslMode.Disable, Pooling = true, MinPoolSize = 0, MaxPoolSize = 2,
            Timeout = 2, CommandTimeout = 5, CancellationTimeout = 1000,
            IncludeErrorDetail = false, LogParameters = false, PersistSecurityInfo = false,
            Enlist = false, Timezone = "UTC", ApplicationName = "AuditPlatform.LogRehearsal",
            ClientEncoding = "UTF8", SearchPath = "pg_catalog,public",
            Options = "-c statement_timeout=5000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=10000"
        };
        settings["GSS Encryption Mode"] = "Disable";
        var result = new RehearsalTarget(new NpgsqlDataSourceBuilder(settings.ConnectionString).Build(), owner, name);
        try
        {
            await using var connection = await result.Source.OpenConnectionAsync(cancellationToken);
            await result.VerifyAsync(connection, null, cancellationToken);
            return result;
        }
        catch { await result.DisposeAsync(); throw; }
    }

    internal async Task VerifyAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction, CancellationToken token)
    {
        _ = ValidateOwner(OwnerDirectory);
        await using var command = new NpgsqlCommand("""
            SELECT current_database(),inet_server_addr()::text,current_user,current_setting('data_directory')
            """, connection, transaction);
        await using var reader = await command.ExecuteReaderAsync(token);
        if (!await reader.ReadAsync(token) || reader.GetString(0) != database
            || !IPAddress.IsLoopback(IPAddress.Parse(reader.GetString(1).Split('/')[0]))
            || reader.GetString(2) != "auditplatform_test"
            || !Path.GetFullPath(reader.GetString(3)).Equals(Path.Combine(OwnerDirectory, "data"), StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Rehearsal server ownership verification failed.");
    }

    public string RequireOwnedInput(string path)
    {
        _ = ValidateOwner(OwnerDirectory);
        var full = Path.GetFullPath(path);
        if (!full.StartsWith(OwnerDirectory + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
            || !full.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Input must be a synthetic JSON fixture in the owned run directory.");
        RejectLinks(full);
        return full;
    }

    public static string SolutionDirectory()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "AuditPlatform.slnx"))) return directory.FullName;
        throw new InvalidOperationException("The local migration solution is required.");
    }

    private static string ValidateOwner(string path)
    {
        var owner = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar);
        var parent = Path.Combine(SolutionDirectory(), ".tools", "postgres-tests");
        var name = Path.GetFileName(owner);
        if (Path.GetDirectoryName(owner) != parent || !name.StartsWith("run-", StringComparison.Ordinal)
            || !Guid.TryParseExact(name[4..], "N", out _))
            throw new InvalidOperationException("A marker-owned local fixture directory is required.");
        RejectLinks(owner);
        RejectLinks(Path.Combine(owner, "data"));
        var marker = Path.Combine(owner, "fixture-owner");
        RejectLinks(marker);
        if (File.ReadAllText(marker).Trim() != name[4..])
            throw new InvalidOperationException("Fixture ownership marker does not match.");
        return owner;
    }

    private static void RejectLinks(string path)
    {
        var current = new DirectoryInfo(path);
        if (File.Exists(path))
        {
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("Linked fixture paths are not permitted.");
            current = current.Parent!;
        }
        for (; current is not null; current = current.Parent)
            if (current.Exists && (current.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("Linked fixture paths are not permitted.");
    }

    public ValueTask DisposeAsync() => Source.DisposeAsync();
}
