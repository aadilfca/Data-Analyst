using System.Diagnostics;
using System.IO.Compression;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.LocalDatabaseBackup;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class LocalDatabaseBackupTests
{
    private static readonly CancellationToken Ct = new CancellationTokenSource(TimeSpan.FromMinutes(5)).Token;

    [Fact]
    public void DisabledByDefault_AndNoProtectedFolderIsReported()
    {
        var folder = Path.Combine(Path.GetTempPath(), "auditplatform-p59-disabled-" + Guid.NewGuid().ToString("N"));
        var env = EnvironmentFor(FindSolutionDirectory());
        var service = new LocalDatabaseBackupService(Options.Create(new LocalDatabaseBackupOptions()),
            Options.Create(new PostgresOptions()), env);
        var availability = service.Availability();
        Assert.Equal("unavailable", availability.Status);
        Assert.Contains(availability.Requirements!, requirement => requirement.Contains("disabled", StringComparison.OrdinalIgnoreCase));
        Assert.False(Directory.Exists(folder));
    }

    [Fact]
    public async Task NonAdministratorCannotStartOrInspectBackups()
    {
        var service = new LocalDatabaseBackupService(Options.Create(new LocalDatabaseBackupOptions()),
            Options.Create(new PostgresOptions()), EnvironmentFor(FindSolutionDirectory()));
        var member = new VerifiedSession(Guid.NewGuid().ToString("N"), 22, "member", "member",
            DateTime.UtcNow.AddHours(1), null, null, null);
        var operation = Guid.NewGuid().ToString("N");
        Assert.Equal("forbidden", (await service.StatusAsync(member, operation, Ct)).Status);
        Assert.Equal("forbidden", (await service.CreateAsync(member, operation, Ct)).Status);
    }

    [Fact]
    public async Task FullDump_IsPackagedCheckedAndRestoresToAnIsolatedDatabase()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var fixture = new PostgresFixture();
        await fixture.InitializeAsync();
        await using var source = await fixture.OpenControlConnectionAsync(Ct);
        var table = await fixture.CreateTableAsync(Ct);
        await using (var insert = new NpgsqlCommand($"INSERT INTO {table}(id,amount) VALUES (1,10.25),(2,10.25),(3,-4.00)", source))
            await insert.ExecuteNonQueryAsync(Ct);
        var expected = await RowsAsync(source, table);
        await using (var sequence = new NpgsqlCommand("CREATE SEQUENCE public.p59_recovery_sequence START 7; SELECT nextval('public.p59_recovery_sequence')", source))
            Assert.Equal(7L, Convert.ToInt64(await sequence.ExecuteScalarAsync(Ct)));
        var expectedSequence = await SequenceAsync(source);
        var expectedTables = await TablesAsync(source);

        var archiveParent = Path.Combine(Path.GetTempPath(), "auditplatform-p59-" + Guid.NewGuid().ToString("N"));
        var fixtureConnection = new NpgsqlConnectionStringBuilder(fixture.ConnectionString);
        var options = new LocalDatabaseBackupOptions
        {
            Enabled = true, ArchiveDirectory = archiveParent, ToolsDirectory = fixture.BinaryDirectory,
            ConnectionString = $"postgresql://{Uri.EscapeDataString(fixtureConnection.Username ?? "")}:{Uri.EscapeDataString(fixtureConnection.Password ?? "")}@127.0.0.1:{fixture.Port}/{fixtureConnection.Database}?sslmode=disable",
            TimeoutSeconds = 120, MinimumFreeBytes = 1, MaximumConcurrentRequests = 1
        };
        var postgres = new PostgresOptions { Enabled = true, ConnectionString = fixture.ConnectionString };
        var service = new LocalDatabaseBackupService(Options.Create(options), Options.Create(postgres), EnvironmentFor(FindSolutionDirectory()));
        var admin = new VerifiedSession(Guid.NewGuid().ToString("N"), 17, "admin", "admin",
            DateTime.UtcNow.AddHours(1), null, null, null);
        string? restoreDatabase = null;
        try
        {
            Assert.Equal("ready", service.Availability().Status);
            var operation = Guid.NewGuid().ToString("N");
            var result = await service.CreateAsync(admin, operation, Ct);
            Assert.Equal("success", result.Status);
            var backup = Assert.IsType<LocalBackupDocument>(result.Backup);
            Assert.Equal("complete", backup.State);
            Assert.False(backup.Running);
            Assert.Equal(4, backup.Progress.CompletedSteps);
            Assert.Matches("^[0-9a-f]{64}$", backup.Sha256!);
            Assert.True(File.Exists(backup.LocalPath));

            var extract = Path.Combine(archiveParent, "recovery-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(extract);
            string dumpPath = Path.Combine(extract, "database.dump");
            using (var zip = ZipFile.OpenRead(backup.LocalPath))
            {
                Assert.Equal(2, zip.Entries.Count);
                var dump = Assert.IsType<ZipArchiveEntry>(zip.GetEntry("database.dump"));
                await using (var input = dump.Open()) await using (var output = File.Create(dumpPath)) await input.CopyToAsync(output, Ct);
                using var manifestStream = zip.GetEntry("manifest.json")!.Open();
                using var manifest = await JsonDocument.ParseAsync(manifestStream, cancellationToken: Ct);
                Assert.Equal(operation, manifest.RootElement.GetProperty("operationId").GetString());
                Assert.Equal(new FileInfo(dumpPath).Length, manifest.RootElement.GetProperty("dumpBytes").GetInt64());
            }

            restoreDatabase = "p59_restore_" + Guid.NewGuid().ToString("N");
            var adminConnection = new NpgsqlConnectionStringBuilder(fixture.ConnectionString) { Database = "postgres" };
            await using (var control = new NpgsqlConnection(adminConnection.ConnectionString))
            {
                await control.OpenAsync(Ct);
                await using var create = new NpgsqlCommand($"CREATE DATABASE \"{restoreDatabase}\"", control);
                await create.ExecuteNonQueryAsync(Ct);
            }
            await RestoreAsync(fixture, restoreDatabase, dumpPath);
            var restoredConnection = new NpgsqlConnectionStringBuilder(fixture.ConnectionString) { Database = restoreDatabase };
            await using var restored = new NpgsqlConnection(restoredConnection.ConnectionString);
            await restored.OpenAsync(Ct);
            Assert.Equal(expected, await RowsAsync(restored, table));
            Assert.Equal(expectedSequence, await SequenceAsync(restored));
            Assert.Equal(expectedTables, await TablesAsync(restored));

            var status = await service.StatusAsync(admin, operation, Ct);
            Assert.Equal("success", status.Status);
            Assert.Equal(backup.Sha256, status.Backup!.Sha256);
            var other = admin with { UserId = admin.UserId + 1 };
            Assert.Equal("not_found", (await service.StatusAsync(other, operation, Ct)).Status);
        }
        finally
        {
            if (restoreDatabase is not null)
            {
                var adminConnection = new NpgsqlConnectionStringBuilder(fixture.ConnectionString) { Database = "postgres" };
                await using var control = new NpgsqlConnection(adminConnection.ConnectionString);
                await control.OpenAsync(Ct);
                await using var drop = new NpgsqlCommand($"DROP DATABASE IF EXISTS \"{restoreDatabase}\" WITH (FORCE)", control);
                await drop.ExecuteNonQueryAsync(Ct);
            }
            var full = Path.GetFullPath(archiveParent); var temp = Path.GetFullPath(Path.GetTempPath());
            if (!full.StartsWith(temp, StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Test cleanup target escaped the temporary directory.");
            if (Directory.Exists(full)) Directory.Delete(full, recursive: true);
        }
    }

    private static async Task<string> RowsAsync(NpgsqlConnection connection, string table)
    {
        await using var command = new NpgsqlCommand($"SELECT string_agg(id::text || ':' || amount::text, ',' ORDER BY id) FROM {table}", connection);
        return (string)(await command.ExecuteScalarAsync(Ct))!;
    }

    private static async Task<string> SequenceAsync(NpgsqlConnection connection)
    {
        await using var command = new NpgsqlCommand("SELECT last_value::text || ':' || is_called::text FROM public.p59_recovery_sequence", connection);
        return (string)(await command.ExecuteScalarAsync(Ct))!;
    }

    private static async Task<string> TablesAsync(NpgsqlConnection connection)
    {
        await using var command = new NpgsqlCommand("SELECT string_agg(tablename,',' ORDER BY tablename) FROM pg_tables WHERE schemaname='public'", connection);
        return (string)(await command.ExecuteScalarAsync(Ct))!;
    }

    private static async Task RestoreAsync(PostgresFixture fixture, string database, string dump)
    {
        var start = new ProcessStartInfo(Path.Combine(fixture.BinaryDirectory, "pg_restore.exe"))
        { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true, RedirectStandardOutput = true };
        foreach (var arg in new[] { "--exit-on-error", "--single-transaction", "--no-password", "--dbname", database, dump }) start.ArgumentList.Add(arg);
        var source = new NpgsqlConnectionStringBuilder(fixture.ConnectionString);
        start.Environment["PGHOST"] = source.Host; start.Environment["PGPORT"] = source.Port.ToString();
        start.Environment["PGDATABASE"] = database; start.Environment["PGUSER"] = source.Username;
        start.Environment["PGPASSWORD"] = source.Password; start.Environment["PGSSLMODE"] = "disable";
        using var process = Process.Start(start)!;
        var stdout = process.StandardOutput.ReadToEndAsync(Ct); var stderr = process.StandardError.ReadToEndAsync(Ct);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(Ct); deadline.CancelAfter(TimeSpan.FromMinutes(2));
        await process.WaitForExitAsync(deadline.Token);
        if (process.ExitCode != 0) throw new InvalidOperationException("Isolated pg_restore failed: " + (await stderr)[..Math.Min((await stderr).Length, 1200)]);
        _ = await stdout;
    }

    private static IWebHostEnvironment EnvironmentFor(string contentRoot) => new TestEnvironment(contentRoot);

    private static string FindSolutionDirectory()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "AuditPlatform.slnx"))) return directory.FullName;
        throw new InvalidOperationException("Cannot locate the isolated migration solution.");
    }

    private sealed class TestEnvironment(string contentRoot) : IWebHostEnvironment
    {
        public string ApplicationName { get; set; } = "AuditPlatform.Api.Tests";
        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
        public string ContentRootPath { get; set; } = contentRoot;
        public string EnvironmentName { get; set; } = "Development";
        public string WebRootPath { get; set; } = contentRoot;
        public IFileProvider WebRootFileProvider { get; set; } = new NullFileProvider();
    }
}
