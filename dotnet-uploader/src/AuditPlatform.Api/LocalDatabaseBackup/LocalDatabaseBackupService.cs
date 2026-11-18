using System.Diagnostics;
using System.IO.Compression;
using System.ComponentModel;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.LocalDatabaseBackup;

public sealed class LocalDatabaseBackupService(IOptions<LocalDatabaseBackupOptions> options,
    IOptions<PostgresOptions> postgres, IWebHostEnvironment environment, TimeProvider? clock = null)
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    private readonly SemaphoreSlim requests = new(options.Value.MaximumConcurrentRequests);
    private readonly TimeProvider time = clock ?? TimeProvider.System;
    private const string OperationPattern = "^[0-9a-f]{32}$";

    public LocalBackupResult Availability()
    {
        var requirements = new List<string>();
        if (!options.Value.Enabled) requirements.Add("Local full database backup is disabled on this computer.");
        if (!OperatingSystem.IsWindows() || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("RENDER")))
            requirements.Add("Local full database backup is available only on the Windows application computer.");
        if (!postgres.Value.Enabled) requirements.Add("PostgreSQL is not enabled for the application.");
        try { _ = ArchiveRoot(false); }
        catch (BackupRejected error) { requirements.Add(error.Message); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException or DriveNotFoundException)
        { requirements.Add("The protected local backup folder could not be checked."); }
        try { _ = Tools(); }
        catch (BackupRejected error) { requirements.Add(error.Message); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException)
        { requirements.Add("The configured PostgreSQL backup tools could not be checked."); }
        if (postgres.Value.Enabled)
        {
            try { _ = ConnectionEnvironment(); }
            catch (BackupRejected error) { requirements.Add(error.Message); }
            catch (Exception error) when (error is ArgumentException or FormatException)
            { requirements.Add("A valid PostgreSQL backup connection must be configured."); }
        }
        return new(requirements.Count == 0 ? "ready" : "unavailable", Requirements: requirements);
    }

    public async Task<LocalBackupResult> StatusAsync(VerifiedSession? session, string? operationId,
        CancellationToken cancellationToken = default)
    {
        var access = Access(session);
        if (access is not null) return access;
        var availability = Availability();
        if (availability.Status != "ready") return availability;
        if (operationId is null) return availability;
        if (!System.Text.RegularExpressions.Regex.IsMatch(operationId, OperationPattern,
                System.Text.RegularExpressions.RegexOptions.CultureInvariant))
            return new("invalid", Code: "invalid_operation_reference");
        try
        {
            var root = ArchiveRoot(false);
            var directory = OperationDirectory(root, operationId);
            if (!Directory.Exists(directory)) return new("not_found");
            var receipt = await ReadReceiptAsync(directory, operationId, session!.UserId, cancellationToken);
            var probe = TryAcquireLock(root, out _);
            var running = probe is null && receipt.State == "creating";
            probe?.Dispose();
            if (receipt.State == "creating" && !running) receipt = receipt with { State = "incomplete" };
            return new("success", Public(directory, receipt, running));
        }
        catch (BackupRejected error) { return new(error.Status == 404 ? "not_found" : "unavailable", Detail: error.Message, Code: error.Code); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or ArgumentException or NotSupportedException or DriveNotFoundException)
        { return new("unavailable", Detail: "Backup status could not be read. Existing files were retained.", Code: "backup_status_unavailable"); }
    }

    public async Task<LocalBackupResult> CreateAsync(VerifiedSession? session, string operationId,
        CancellationToken cancellationToken = default)
    {
        var access = Access(session);
        if (access is not null) return access;
        if (!System.Text.RegularExpressions.Regex.IsMatch(operationId, OperationPattern,
                System.Text.RegularExpressions.RegexOptions.CultureInvariant))
            return new("invalid", Detail: "Invalid backup reference.", Code: "invalid_operation_reference");
        var availability = Availability();
        if (availability.Status != "ready") return availability;
        if (!await requests.WaitAsync(0, cancellationToken))
            return new("busy", Detail: "A database backup is already running. Check its status before retrying.", RetrySame: true, Code: "backup_busy");
        try
        {
            var root = ArchiveRoot(true);
            using var fileLock = TryAcquireLock(root, out _);
            if (fileLock is null)
                return new("busy", Detail: "A database backup is already running. Check its status before retrying.", RetrySame: true, Code: "backup_busy");
            var directory = OperationDirectory(root, operationId);
            Receipt receipt;
            if (Directory.Exists(directory))
            {
                receipt = await ReadReceiptAsync(directory, operationId, session!.UserId, cancellationToken);
                if (receipt.State != "complete")
                    return new("conflict", Public(directory, receipt, false), Detail: "This backup did not finish. Existing files were retained; start a new backup.", Code: "backup_incomplete");
            }
            else
            {
                if (new DriveInfo(Path.GetPathRoot(root)!).AvailableFreeSpace < options.Value.MinimumFreeBytes)
                    return new("unavailable", Detail: "At least the configured minimum free space is required in the protected backup folder.", Code: "backup_low_disk");
                var tools = Tools();
                var env = ConnectionEnvironment();
                var now = time.GetUtcNow();
                var fileName = $"Database-{now.UtcDateTime:yyyyMMdd'T'HHmmss'Z'}-{operationId}.zip";
                if (OperatingSystem.IsWindows() && Path.Combine(directory, fileName).Length >= 240)
                    return new("unavailable", Detail: "Choose a shorter protected archive folder path before starting a full backup.", Code: "archive_path_too_long");
                directory = CreateOperationDirectory(root, operationId);
                receipt = new(1, operationId, session!.UserId, now, null, "creating", "export", fileName, null, null, null, null);
                await SaveReceiptAsync(directory, receipt, cancellationToken);
                try
                {
                    var partial = Path.Combine(directory, "database.partial");
                    await RunToolAsync(tools.Dump, ["--format=custom", "--no-password", "--lock-wait-timeout=10000", "--file", partial], env, cancellationToken);
                    if (!File.Exists(partial) || new FileInfo(partial).Length < 5) throw new BackupRejected("The backup tool did not produce a complete database archive.", "native_output_invalid");
                    if (new DriveInfo(Path.GetPathRoot(root)!).AvailableFreeSpace < new FileInfo(partial).Length + 64L * 1024 * 1024)
                        throw new BackupRejected("More protected free disk space is required to package and verify the complete database dump.", "backup_packaging_space_required");
                    await using (var source = File.OpenRead(partial))
                    {
                        byte[] signature = new byte[5];
                        if (await source.ReadAsync(signature, cancellationToken) != signature.Length || !signature.AsSpan().SequenceEqual("PGDMP"u8))
                            throw new BackupRejected("The database backup format is invalid.", "native_format_invalid");
                    }
                    receipt = receipt with { Stage = "native_check" };
                    await SaveReceiptAsync(directory, receipt, cancellationToken);
                    var cleanEnv = ProcessEnvironment();
                    var restore = Tools().Restore;
                    await RunToolAsync(restore, ["--file", OperatingSystem.IsWindows() ? "NUL" : "/dev/null", partial], cleanEnv, cancellationToken);
                    receipt = receipt with { Stage = "package" };
                    await SaveReceiptAsync(directory, receipt, cancellationToken);
                    var dumpHash = await HashFileAsync(partial, cancellationToken);
                    var manifest = new Manifest("postgresql-custom", 1, operationId, now, "database.dump",
                        new FileInfo(partial).Length, dumpHash,
                        ["server-global roles", "source workbooks", "Storage object bytes", "environment credentials"]);
                    var target = Path.Combine(directory, fileName);
                    await CreatePackageAsync(target, partial, manifest, cancellationToken);
                    var verifiedHash = await VerifyPackageAsync(target, manifest, cancellationToken);
                    receipt = receipt with { State = "complete", Stage = "complete", VerifiedAt = time.GetUtcNow(),
                        Bytes = new FileInfo(target).Length, Sha256 = verifiedHash };
                    await SaveReceiptAsync(directory, receipt, cancellationToken);
                    try { File.Delete(partial); } catch (IOException) { } catch (UnauthorizedAccessException) { }
                    return new("success", Public(directory, receipt, false));
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    receipt = receipt with { State = "incomplete", Error = "Backup was interrupted. Existing files were retained; start a new backup." , ErrorCode = "backup_interrupted" };
                    await SaveReceiptAsync(directory, receipt, CancellationToken.None);
                    return new("unavailable", Public(directory, receipt, false), Detail: receipt.Error, RetrySame: false, Code: receipt.ErrorCode);
                }
                catch (BackupRejected error)
                {
                    receipt = receipt with { State = "incomplete", Error = error.Message, ErrorCode = error.Code };
                    await SaveReceiptAsync(directory, receipt, CancellationToken.None);
                    return new("unavailable", Public(directory, receipt, false), Detail: error.Message, Code: error.Code);
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidDataException)
                {
                    receipt = receipt with { State = "incomplete", Error = "The local backup could not be completed. Existing database data and files were retained.", ErrorCode = "backup_file_error" };
                    await SaveReceiptAsync(directory, receipt, CancellationToken.None);
                    return new("unavailable", Public(directory, receipt, false), Detail: receipt.Error, Code: receipt.ErrorCode);
                }
            }
            var path = Path.Combine(directory, receipt.FileName);
            if (!File.Exists(path) || receipt.Bytes != new FileInfo(path).Length || receipt.Sha256 != await HashFileAsync(path, cancellationToken))
                return new("conflict", Public(directory, receipt, false), Detail: "The local backup has changed. Existing files were retained; create a new backup.", Code: "backup_integrity_changed");
            return new("success", Public(directory, receipt, false));
        }
        catch (BackupRejected error) { return new(error.Status == 404 ? "not_found" : error.Status == 409 ? "conflict" : "unavailable", Detail: error.Message, Code: error.Code); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or ArgumentException or NotSupportedException or DriveNotFoundException or FormatException)
        { return new("unavailable", Detail: "Local backup storage is unavailable. Existing data and files were retained.", Code: "backup_storage_unavailable"); }
        finally { requests.Release(); }
    }

    private LocalBackupResult? Access(VerifiedSession? session)
    {
        if (session is null) return new("unauthenticated", Code: "not_authenticated");
        if (session.Role != "admin") return new("forbidden", Code: "administrator_required");
        return LocalRequestAllowed ? null : new("not_found", Code: "local_backup_unavailable");
    }

    private static bool LocalRequestAllowed => OperatingSystem.IsWindows()
        && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("RENDER"));

    private string ArchiveRoot(bool create)
    {
        string? configured = !string.IsNullOrWhiteSpace(options.Value.ArchiveDirectory)
            ? options.Value.ArchiveDirectory : Environment.GetEnvironmentVariable("UPLOAD_ARCHIVE_DIR");
        if (string.IsNullOrWhiteSpace(configured) || !Path.IsPathFullyQualified(configured))
            throw new BackupRejected("Configure an absolute protected archive folder outside the application checkout.", "archive_folder_required");
        var parent = Path.GetFullPath(configured);
        EnsureOutsideApplication(parent);
        EnsureNoReparsePoints(parent);
        var volume = Path.GetPathRoot(parent);
        if (volume is null || new DriveInfo(volume).DriveType != DriveType.Fixed)
            throw new BackupRejected("Choose a protected folder on a local fixed disk.", "archive_disk_not_local");
        var root = Path.Combine(parent, "full-database-backups");
        if (create) Directory.CreateDirectory(root);
        EnsureNoReparsePoints(root);
        return root;
    }

    private void EnsureOutsideApplication(string path)
    {
        var content = Path.GetFullPath(environment.ContentRootPath).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var candidate = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (candidate.StartsWith(content, StringComparison.OrdinalIgnoreCase) || content.StartsWith(candidate, StringComparison.OrdinalIgnoreCase))
            throw new BackupRejected("The protected backup folder must be outside the application checkout.", "archive_folder_inside_app");
    }

    private static void EnsureNoReparsePoints(string path)
    {
        var current = new DirectoryInfo(Path.GetFullPath(path));
        for (var item = current; item is not null; item = item.Parent)
        {
            if (item.Exists && (item.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new BackupRejected("The configured backup location contains a link or junction and is not accepted.", "archive_path_link");
        }
    }

    private (string Dump, string Restore) Tools()
    {
        string? directory = !string.IsNullOrWhiteSpace(options.Value.ToolsDirectory)
            ? options.Value.ToolsDirectory : Environment.GetEnvironmentVariable("DATABASE_BACKUP_BIN_DIR");
        string? dump = null, restore = null;
        if (!string.IsNullOrWhiteSpace(directory))
        {
            if (!Path.IsPathFullyQualified(directory)) throw new BackupRejected("Configure an absolute PostgreSQL tools folder.", "tools_folder_invalid");
            dump = Path.Combine(directory, "pg_dump.exe"); restore = Path.Combine(directory, "pg_restore.exe");
        }
        else
        {
            dump = FindOnPath("pg_dump.exe"); restore = FindOnPath("pg_restore.exe");
        }
        if (dump is null || restore is null || !File.Exists(dump) || !File.Exists(restore))
            throw new BackupRejected("PostgreSQL pg_dump.exe and pg_restore.exe must be configured on this computer.", "postgres_tools_missing");
        if (!Path.GetDirectoryName(Path.GetFullPath(dump))!.Equals(Path.GetDirectoryName(Path.GetFullPath(restore)), StringComparison.OrdinalIgnoreCase))
            throw new BackupRejected("Use pg_dump.exe and pg_restore.exe from the same PostgreSQL tools folder.", "postgres_tools_mismatch");
        return (Path.GetFullPath(dump), Path.GetFullPath(restore));
    }

    private static string? FindOnPath(string name) => Environment.GetEnvironmentVariable("PATH")?.Split(Path.PathSeparator)
        .Select(directory => Path.Combine(directory.Trim(), name)).FirstOrDefault(File.Exists);

    private Dictionary<string, string?> ConnectionEnvironment()
    {
        NpgsqlConnectionStringBuilder connection;
        var configured = string.IsNullOrWhiteSpace(options.Value.ConnectionString)
            ? Environment.GetEnvironmentVariable("DATABASE_BACKUP_URL") : options.Value.ConnectionString;
        if (string.IsNullOrWhiteSpace(configured)) configured = postgres.Value.ConnectionString;
        try { connection = ParseConnection(configured); }
        catch (ArgumentException) { throw new BackupRejected("A valid PostgreSQL backup connection must be configured.", "database_connection_invalid"); }
        if (string.IsNullOrWhiteSpace(connection.Host) || string.IsNullOrWhiteSpace(connection.Database)
            || string.IsNullOrWhiteSpace(connection.Username))
            throw new BackupRejected("A complete PostgreSQL backup connection is required.", "database_connection_invalid");
        if (!PostgresOptions.IsLoopback(connection.Host) && connection.SslMode is not (SslMode.Require or SslMode.VerifyCA or SslMode.VerifyFull))
            throw new BackupRejected("Remote database backups require an encrypted PostgreSQL connection.", "database_ssl_required");
        if (connection.Host.EndsWith(".pooler.supabase.com", StringComparison.OrdinalIgnoreCase) && connection.Port == 6543)
            throw new BackupRejected("Configure a direct or session-mode connection for full database backups.", "database_pooler_unsupported");
        var env = ProcessEnvironment();
        env["PGHOST"] = connection.Host; env["PGPORT"] = connection.Port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        env["PGDATABASE"] = connection.Database; env["PGUSER"] = connection.Username;
        if (!string.IsNullOrEmpty(connection.Password)) env["PGPASSWORD"] = connection.Password;
        env["PGSSLMODE"] = connection.SslMode switch { SslMode.Disable => "disable", SslMode.Prefer => "prefer", SslMode.Require => "require", SslMode.VerifyCA => "verify-ca", _ => "verify-full" };
        if (!string.IsNullOrEmpty(connection.RootCertificate)) env["PGSSLROOTCERT"] = connection.RootCertificate;
        if (!string.IsNullOrEmpty(connection.SslCertificate)) env["PGSSLCERT"] = connection.SslCertificate;
        if (!string.IsNullOrEmpty(connection.SslKey)) env["PGSSLKEY"] = connection.SslKey;
        env["PGCONNECT_TIMEOUT"] = "15"; env["PGAPPNAME"] = "internal-audit-local-backup";
        env["PGOPTIONS"] = "-c default_transaction_read_only=on -c statement_timeout=900000 -c lock_timeout=10000";
        return env;
    }

    private static NpgsqlConnectionStringBuilder ParseConnection(string configured)
    {
        if (!configured.StartsWith("postgres://", StringComparison.OrdinalIgnoreCase)
            && !configured.StartsWith("postgresql://", StringComparison.OrdinalIgnoreCase))
            return new NpgsqlConnectionStringBuilder(configured);
        if (!Uri.TryCreate(configured, UriKind.Absolute, out var uri) || uri.UserInfo.Length == 0
            || uri.AbsolutePath.Length < 2 || uri.Fragment.Length != 0)
            throw new ArgumentException("Invalid database URL.");
        var credentials = uri.UserInfo.Split(':', 2);
        if (credentials.Length != 2) throw new ArgumentException("Invalid database URL.");
        var builder = new NpgsqlConnectionStringBuilder
        {
            Host = uri.Host, Port = uri.IsDefaultPort ? 5432 : uri.Port,
            Database = Uri.UnescapeDataString(uri.AbsolutePath[1..]),
            Username = Uri.UnescapeDataString(credentials[0]), Password = Uri.UnescapeDataString(credentials[1])
        };
        foreach (var item in uri.Query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var pair = item.Split('=', 2); var key = Uri.UnescapeDataString(pair[0].Replace('+', ' '));
            var value = Uri.UnescapeDataString(pair.Length == 2 ? pair[1].Replace('+', ' ') : "");
            switch (key.ToLowerInvariant())
            {
                case "sslmode": builder.SslMode = value.ToLowerInvariant() switch
                {
                    "disable" => SslMode.Disable, "prefer" => SslMode.Prefer, "require" => SslMode.Require,
                    "verify-ca" => SslMode.VerifyCA, "verify-full" => SslMode.VerifyFull,
                    _ => throw new ArgumentException("Invalid database URL.")
                }; break;
                case "sslrootcert": builder.RootCertificate = value; break;
                case "sslcert": builder.SslCertificate = value; break;
                case "sslkey": builder.SslKey = value; break;
                case "connect_timeout": if (!int.TryParse(value, out var timeout) || timeout is < 1 or > 60) throw new ArgumentException("Invalid database URL."); builder.Timeout = timeout; break;
                case "application_name": break;
                default: throw new ArgumentException("Unsupported database URL option.");
            }
        }
        return builder;
    }

    private static Dictionary<string, string?> ProcessEnvironment()
    {
        var values = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);
        foreach (System.Collections.DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            var key = (string)entry.Key;
            if (!key.StartsWith("PG", StringComparison.OrdinalIgnoreCase)
                && !key.Equals("DATABASE_URL", StringComparison.OrdinalIgnoreCase)
                && !key.Equals("DATABASE_BACKUP_URL", StringComparison.OrdinalIgnoreCase)
                && !key.StartsWith("DRIVE_", StringComparison.OrdinalIgnoreCase)) values[key] = (string?)entry.Value;
        }
        return values;
    }

    private async Task RunToolAsync(string executable, IReadOnlyList<string> args, Dictionary<string, string?> env,
        CancellationToken cancellationToken)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        var start = new ProcessStartInfo(executable) { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = true };
        foreach (var arg in args) start.ArgumentList.Add(arg);
        start.Environment.Clear();
        foreach (var (key,value) in env) start.Environment[key] = value;
        try
        {
            using var process = new Process { StartInfo = start };
            if (!process.Start()) throw new BackupRejected("The PostgreSQL backup tool could not be started.", "native_start_failed");
            process.StandardInput.Close();
            var stdout = ReadTailAsync(process.StandardOutput, deadline.Token);
            var stderr = ReadTailAsync(process.StandardError, deadline.Token);
            try { await process.WaitForExitAsync(deadline.Token); }
            catch (OperationCanceledException)
            {
                try { process.Kill(entireProcessTree: true); } catch (Exception error) when (error is InvalidOperationException or Win32Exception) { }
                using (var stopDeadline = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
                { try { await process.WaitForExitAsync(stopDeadline.Token); } catch (OperationCanceledException) { } }
                try { await Task.WhenAll(stdout, stderr); } catch (OperationCanceledException) { }
                if (!cancellationToken.IsCancellationRequested)
                    throw new BackupRejected("The PostgreSQL backup tool timed out. Existing database data was not changed; start a new backup.", "native_timeout");
                throw;
            }
            if (process.ExitCode != 0)
            {
                string safe = (await stderr).ToLowerInvariant();
                var error = safe.Contains("permission denied", StringComparison.Ordinal) ? ("database_permission", "PostgreSQL denied read access to a required database object.")
                    : safe.Contains("server version mismatch", StringComparison.Ordinal) ? ("tool_version_mismatch", "The PostgreSQL backup tool version does not support the source database version.")
                    : safe.Contains("no space left", StringComparison.Ordinal) ? ("local_disk_full", "The local archive disk ran out of space.")
                    : safe.Contains("ssl", StringComparison.Ordinal) ? ("database_connection_error", "The database connection reported an SSL or network error during backup.")
                    : ("native_tool_failed", "The PostgreSQL backup tool could not complete. Check its version, connection and access; current database data was not changed.");
                throw new BackupRejected(error.Item2, error.Item1);
            }
            _ = await stdout; _ = await stderr;
        }
        catch (Win32Exception) { throw new BackupRejected("The PostgreSQL backup tool could not be started on this computer.", "native_start_failed"); }
    }

    private static async Task<string> ReadTailAsync(StreamReader reader, CancellationToken token)
    {
        var tail = new StringBuilder(16384); var buffer = new char[4096];
        int read;
        while ((read = await reader.ReadAsync(buffer.AsMemory(), token)) > 0)
        {
            tail.Append(buffer, 0, read);
            if (tail.Length > 16384) tail.Remove(0, tail.Length - 16384);
        }
        return tail.ToString();
    }

    private static FileStream? TryAcquireLock(string root, out FileStream? held)
    {
        held = null;
        try { held = new FileStream(Path.Combine(root, ".backup.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); return held; }
        catch (IOException) { return null; }
    }

    private static string OperationDirectory(string root, string operation)
    {
        var directory = Path.Combine(root, "backup-" + operation);
        if (Directory.Exists(directory) && (File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0)
            throw new BackupRejected("The backup location is unsafe.", "unsafe_backup_path", 409);
        return directory;
    }

    private static string CreateOperationDirectory(string root, string operation)
    {
        var path = OperationDirectory(root, operation);
        Directory.CreateDirectory(path);
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new BackupRejected("The backup location is unsafe.", "unsafe_backup_path", 409);
        return path;
    }

    private static async Task<Receipt> ReadReceiptAsync(string directory, string operation, int owner, CancellationToken token)
    {
        var path = Path.Combine(directory, "receipt.json");
        if (!File.Exists(path) || (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 || new FileInfo(path).Length > 32768)
            throw new BackupRejected("This backup reference is unavailable for this administrator.", "backup_not_found", 404);
        await using var source = File.OpenRead(path);
        var receipt = await JsonSerializer.DeserializeAsync<Receipt>(source, Json, token);
        if (receipt is null || receipt.OperationId != operation || receipt.OwnerId != owner
            || receipt.State is not ("creating" or "incomplete" or "complete")
            || receipt.FileName != $"Database-{receipt.CreatedAt.UtcDateTime:yyyyMMdd'T'HHmmss'Z'}-{operation}.zip"
            || receipt.State == "complete" && (receipt.Bytes is null or < 5 || receipt.Sha256 is not { Length: 64 }))
            throw new BackupRejected("This backup reference is unavailable for this administrator.", "backup_not_found", 404);
        return receipt;
    }

    private static async Task SaveReceiptAsync(string directory, Receipt receipt, CancellationToken token)
    {
        var temp = Path.Combine(directory, ".receipt-" + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            await using (var stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, FileOptions.Asynchronous | FileOptions.WriteThrough))
            { await JsonSerializer.SerializeAsync(stream, receipt, Json, token); await stream.FlushAsync(token); stream.Flush(flushToDisk: true); }
            File.Move(temp, Path.Combine(directory, "receipt.json"), overwrite: true);
        }
        finally { try { File.Delete(temp); } catch (IOException) { } }
    }

    private static async Task CreatePackageAsync(string path, string dump, Manifest manifest, CancellationToken token)
    {
        await using var output = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None, 1024 * 1024, FileOptions.Asynchronous | FileOptions.WriteThrough);
        using (var archive = new ZipArchive(output, ZipArchiveMode.Create, leaveOpen: true))
        {
            var dumpEntry = archive.CreateEntry("database.dump", CompressionLevel.NoCompression);
            await using (var destination = dumpEntry.Open()) await using (var source = File.OpenRead(dump)) await source.CopyToAsync(destination, 1024 * 1024, token);
            var manifestEntry = archive.CreateEntry("manifest.json", CompressionLevel.NoCompression);
            await using var manifestStream = manifestEntry.Open();
            await JsonSerializer.SerializeAsync(manifestStream, manifest, Json, token);
        }
        await output.FlushAsync(token); output.Flush(flushToDisk: true);
    }

    private static async Task<string> VerifyPackageAsync(string path, Manifest manifest, CancellationToken token)
    {
        await using var input = File.OpenRead(path);
        using var archive = new ZipArchive(input, ZipArchiveMode.Read);
        if (archive.Entries.Count != 2 || archive.GetEntry("database.dump") is not { } dump || archive.GetEntry("manifest.json") is not { } metadata)
            throw new BackupRejected("The local backup package is incomplete.", "package_invalid");
        await using var stream = dump.Open(); using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[1024 * 1024]; long count = 0; int read;
        while ((read = await stream.ReadAsync(buffer, token)) > 0) { hash.AppendData(buffer, 0, read); count += read; }
        if (count != manifest.DumpBytes || Convert.ToHexStringLower(hash.GetHashAndReset()) != manifest.DumpSha256)
            throw new BackupRejected("The local database archive failed its full checksum check.", "package_checksum_failed");
        await using var manifestStream = metadata.Open();
        var saved = await JsonSerializer.DeserializeAsync<Manifest>(manifestStream, Json, token);
        if (saved is null || saved.Format != manifest.Format || saved.Version != manifest.Version
            || saved.OperationId != manifest.OperationId || saved.CreatedAt != manifest.CreatedAt
            || saved.DumpFile != manifest.DumpFile || saved.DumpBytes != manifest.DumpBytes
            || saved.DumpSha256 != manifest.DumpSha256 || !saved.NotIncluded.SequenceEqual(manifest.NotIncluded))
            throw new BackupRejected("The local backup manifest did not match its database archive.", "package_manifest_invalid");
        return await HashFileAsync(path, token);
    }

    private static async Task<string> HashFileAsync(string path, CancellationToken token)
    {
        await using var input = File.OpenRead(path); return Convert.ToHexStringLower(await SHA256.HashDataAsync(input, token));
    }

    private static LocalBackupDocument Public(string directory, Receipt receipt, bool running)
    {
        var completed = receipt.State == "complete" ? 4 : receipt.Stage switch { "native_check" => 1, "package" => 2, _ => 0 };
        var label = receipt.Stage switch { "native_check" => "Checking the database file", "package" => "Preparing the dated backup ZIP", "complete" => "Local database backup verified", _ => "Saving database data" };
        long bytes = 0; try { var partial = Path.Combine(directory,"database.partial"); if(File.Exists(partial)) bytes=new FileInfo(partial).Length; else if(File.Exists(Path.Combine(directory,receipt.FileName))) bytes=new FileInfo(Path.Combine(directory,receipt.FileName)).Length; } catch(IOException) { }
        var elapsed = (long)Math.Max(0, ((receipt.VerifiedAt ?? DateTimeOffset.UtcNow) - receipt.CreatedAt).TotalSeconds);
        return new(receipt.OperationId, receipt.CreatedAt, receipt.VerifiedAt, receipt.State, receipt.FileName,
            receipt.Bytes, receipt.Sha256, receipt.Error, receipt.ErrorCode, Path.Combine(directory,receipt.FileName), running,
            new(receipt.Stage, completed, label, bytes, elapsed, label));
    }

    private sealed record Receipt(int Version, string OperationId, int OwnerId, DateTimeOffset CreatedAt,
        DateTimeOffset? VerifiedAt, string State, string Stage, string FileName, long? Bytes, string? Sha256,
        string? Error, string? ErrorCode);
    private sealed record Manifest(string Format, int Version, string OperationId, DateTimeOffset CreatedAt,
        string DumpFile, long DumpBytes, string DumpSha256, string[] NotIncluded);
    private sealed class BackupRejected(string message, string code, int status = 503) : Exception(message)
    { public string Code { get; } = code; public int Status { get; } = status; }
}
