using System.Buffers.Binary;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

// Every test run creates its own cluster. This fixture accepts a binary folder,
// never a database URL, and never reads settings from another app.
public sealed class PostgresFixture : IAsyncLifetime
{
    private readonly string marker = Guid.NewGuid().ToString("N");
    private string runDirectory = "";
    private string binaryDirectory = "";
    private bool initialized;
    public string ConnectionString { get; private set; } = "";
    public int Port { get; private set; }

    public async ValueTask InitializeAsync()
    {
        var root = FindSolutionDirectory();
        var tools = Path.Combine(root, ".tools");
        binaryDirectory = Path.GetFullPath(Environment.GetEnvironmentVariable(
            "AUDITPLATFORM_TEST_POSTGRES_BIN")
            ?? Path.Combine(tools, "postgresql-17.11", "pgsql", "bin"));
        if (!binaryDirectory.StartsWith(tools + Path.DirectorySeparatorChar,
                StringComparison.OrdinalIgnoreCase)
            || !File.Exists(Path.Combine(binaryDirectory, Executable("initdb"))))
            throw new InvalidOperationException("Install approved portable PostgreSQL under .tools before testing.");

        runDirectory = Path.Combine(tools, "postgres-tests", "run-" + marker);
        Directory.CreateDirectory(runDirectory);
        await File.WriteAllTextAsync(Path.Combine(runDirectory, "fixture-owner"), marker);
        var passwordFile = Path.Combine(runDirectory, "password.txt");
        var password = Guid.NewGuid().ToString("N");
        await File.WriteAllTextAsync(passwordFile, password);
        Port = ReservePort();

        try
        {
            await RunToolAsync("initdb", "-D", DataDirectory, "-U", "auditplatform_test",
                "--auth=scram-sha-256", "--pwfile=" + passwordFile, "--encoding=UTF8", "--locale=C");
            if (!File.Exists(Path.Combine(DataDirectory, "PG_VERSION")))
                throw new InvalidOperationException("Local initdb did not initialize a cluster; inspect ignored fixture logs.");
            initialized = true;
            await File.AppendAllTextAsync(Path.Combine(DataDirectory, "postgresql.conf"),
                $"\nlisten_addresses = '127.0.0.1'\nport = {Port}\nmax_connections = 12\nshared_buffers = '16MB'\nssl = off\n");
            await RunToolAsync("pg_ctl", "-D", DataDirectory, "-l",
                Path.Combine(runDirectory, "postgres.log"), "-w", "-t", "20", "start");

            var database = "auditplatform_foundation_" + marker;
            var settings = new NpgsqlConnectionStringBuilder
            {
                Host = "127.0.0.1", Port = Port, Database = "postgres",
                Username = "auditplatform_test", Password = password,
                SslMode = SslMode.Disable, Pooling = false, Timeout = 3,
                CommandTimeout = 5, IncludeErrorDetail = false
            };
            await using var admin = new NpgsqlConnection(settings.ConnectionString);
            await admin.OpenAsync();
            // Check the server's actual directory before the fixture's only DDL.
            await using (var verify = new NpgsqlCommand("SHOW data_directory", admin))
            {
                var actual = Path.GetFullPath((string)(await verify.ExecuteScalarAsync())!);
                if (!actual.Equals(Path.GetFullPath(DataDirectory), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Test cluster ownership check failed.");
            }
            // The identifier contains a fixed prefix and generated hex only.
            await using (var create = new NpgsqlCommand($"CREATE DATABASE \"{database}\"", admin))
                await create.ExecuteNonQueryAsync();
            settings.Database = database;
            ConnectionString = settings.ConnectionString;
            RequireSafeTarget(ConnectionString, Port);
        }
        catch
        {
            await DisposeAsync();
            throw;
        }
        finally
        {
            File.Delete(passwordFile);
        }
    }

    public static void RequireSafeTarget(string connectionString, int ownedPort)
    {
        var settings = new NpgsqlConnectionStringBuilder(connectionString);
        var database = settings.Database ?? "";
        if (settings.Host != "127.0.0.1" || settings.Port != ownedPort
            || settings.Username is not ("auditplatform_test" or "auditplatform_user_admin_runtime" or "audit_platform_runtime")
            || !database.StartsWith("auditplatform_foundation_", StringComparison.Ordinal)
            || !Guid.TryParseExact(database["auditplatform_foundation_".Length..], "N", out _))
            throw new InvalidOperationException("Tests require their own loopback fixture database.");
    }

    public PostgresDataSourceFactory CreatePool(int maximumPoolSize = 2,
        int timeoutSeconds = 3, CommitAcknowledgementProxy? proxy = null, string? connectionString = null)
    {
        var target = connectionString ?? ConnectionString;
        RequireSafeTarget(target, Port);
        var connection = new NpgsqlConnectionStringBuilder(target);
        if (proxy is not null)
        {
            if (proxy.UpstreamPort != Port)
                throw new InvalidOperationException("Proxy must belong to this local test cluster.");
            connection.Port = proxy.Port;
            // The one-connection relay parses ordinary PostgreSQL frames,
            // rather than negotiating encryption for this local-only test.
            connection["GSS Encryption Mode"] = "Disable";
        }
        return new(Options.Create(new PostgresOptions
        {
            Enabled = true, ConnectionString = connection.ConnectionString,
            MaximumPoolSize = maximumPoolSize, ConnectionTimeoutSeconds = 2,
            CommandTimeoutSeconds = timeoutSeconds
        }));
    }

    public async Task<string> CreateUserManagementRuntimeConnectionStringAsync(CancellationToken cancellationToken)
    {
        const string role = "auditplatform_user_admin_runtime";
        var password = Guid.NewGuid().ToString("N");
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using (var create = new NpgsqlCommand($"CREATE ROLE {role} LOGIN NOINHERIT PASSWORD '{password}'", connection))
            await create.ExecuteNonQueryAsync(cancellationToken);

        var database = new NpgsqlConnectionStringBuilder(ConnectionString).Database;
        string[] grantStatements =
        [
            $"GRANT CONNECT ON DATABASE {database} TO {role}",
            $"GRANT USAGE ON SCHEMA public TO {role}",
            $"GRANT SELECT (id,username,password_hash,is_active,role,last_login), UPDATE (last_login) ON public.users TO {role}",
            $"GRANT INSERT (username,password_hash,is_active), UPDATE (username,password_hash) ON public.users TO {role}",
            $"GRANT SELECT (token,user_id,expiry,selected_client_id) ON public.sessions TO {role}",
            $"GRANT UPDATE (selected_client_id) ON public.sessions TO {role}",
            $"GRANT SELECT (id,name,tenant_type,is_active) ON public.platform_tenants TO {role}",
            $"GRANT SELECT (tenant_id,user_id,is_active,tenant_role), UPDATE (tenant_role,is_active) ON public.platform_tenant_memberships TO {role}",
            $"GRANT INSERT (tenant_id,user_id,tenant_role) ON public.platform_tenant_memberships TO {role}",
            $"GRANT INSERT (\"user\",action,module,details,event_id,operation_id,actor_user_id,actor_kind,client_id,correlation_id), SELECT (id) ON public.audit_log TO {role}",
            $"DO $$ BEGIN EXECUTE format('GRANT USAGE ON SEQUENCE %s TO {role}', pg_catalog.pg_get_serial_sequence('public.users','id')::regclass); END; $$",
            $"DO $$ BEGIN EXECUTE format('GRANT USAGE ON SEQUENCE %s TO {role}', pg_catalog.pg_get_serial_sequence('public.audit_log','id')::regclass); END; $$"
        ];
        foreach (var statement in grantStatements)
        {
            await using var grant = new NpgsqlCommand(statement, connection);
            await grant.ExecuteNonQueryAsync(cancellationToken);
        }

        var runtime = new NpgsqlConnectionStringBuilder(ConnectionString)
        {
            Username = role,
            Password = password,
            Pooling = false
        };
        return runtime.ConnectionString;
    }

    public async Task<string> CreateDashboardRuntimeConnectionStringAsync(CancellationToken cancellationToken)
    {
        const string role = "audit_platform_runtime";
        var password = Guid.NewGuid().ToString("N");
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using (var create = new NpgsqlCommand($"CREATE ROLE {role} LOGIN NOINHERIT PASSWORD '{password}'", connection))
            await create.ExecuteNonQueryAsync(cancellationToken);

        var database = new NpgsqlConnectionStringBuilder(ConnectionString).Database;
        await using (var connect = new NpgsqlCommand($"GRANT CONNECT ON DATABASE {database} TO {role}", connection))
            await connect.ExecuteNonQueryAsync(cancellationToken);
        await using (var schema = new NpgsqlCommand($"GRANT USAGE ON SCHEMA public TO {role}", connection))
            await schema.ExecuteNonQueryAsync(cancellationToken);

        var runtime = new NpgsqlConnectionStringBuilder(ConnectionString)
        {
            Username = role,
            Password = password,
            Pooling = false
        };
        RequireSafeTarget(runtime.ConnectionString, Port);
        return runtime.ConnectionString;
    }

    public async Task<NpgsqlConnection> OpenControlConnectionAsync(CancellationToken cancellationToken)
    {
        RequireSafeTarget(ConnectionString, Port);
        var connection = new NpgsqlConnection(ConnectionString);
        try
        {
            await connection.OpenAsync(cancellationToken);
            return connection;
        }
        catch
        {
            await connection.DisposeAsync();
            throw;
        }
    }

    public async Task<string> CreateTableAsync(CancellationToken cancellationToken, bool deferredKey = false)
    {
        var name = "\"p04b_" + Guid.NewGuid().ToString("N") + "\"";
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        var keyMode = deferredKey ? " DEFERRABLE INITIALLY DEFERRED" : "";
        await using var command = new NpgsqlCommand(
            $"CREATE TABLE {name} (id integer PRIMARY KEY{keyMode}, amount numeric NOT NULL)", connection);
        await command.ExecuteNonQueryAsync(cancellationToken);
        return name;
    }

    public CommitAcknowledgementProxy CreateCommitProxy()
    {
        RequireSafeTarget(ConnectionString, Port);
        return new(Port);
    }

    public async Task ApplyAuditSchemaAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        // Isolated fixture prerequisite only; the migration itself never builds
        // or replaces the application's canonical source schema.
        await using (var prerequisite = new NpgsqlCommand("""
            CREATE TABLE IF NOT EXISTS public.client_data (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                client_id INTEGER NOT NULL, row_data JSONB NOT NULL DEFAULT '{}'::jsonb)
            """, connection, transaction))
            await prerequisite.ExecuteNonQueryAsync(cancellationToken);
        var sql = await ReadAuditSchemaAsync(cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection, transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }

    public static Task<string> ReadAuditSchemaAsync(CancellationToken cancellationToken) =>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations", "0001_audit_error.sql"), cancellationToken);

    public async Task ApplyTenantFoundationAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        var sql = await ReadTenantFoundationAsync(cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection, transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }

    public static Task<string> ReadTenantFoundationAsync(CancellationToken cancellationToken) =>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations",
            "0003_tenant_foundation.sql"), cancellationToken);

    public async Task ApplyUploadStagingAsync(CancellationToken ct)
    {
        RequireOwnedDirectory();
        await using var connection=await OpenControlConnectionAsync(ct);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var sql=await File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0011_upload_staging.sql"),ct);
        await using var command=new NpgsqlCommand(sql,connection,transaction);
        await command.ExecuteNonQueryAsync(ct); await transaction.CommitAsync(ct);
    }

    public async Task ApplyEngagementFoundationAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        var sql = await ReadEngagementFoundationAsync(cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection, transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }

    public static Task<string> ReadEngagementFoundationAsync(CancellationToken cancellationToken) =>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations",
            "0004_engagement_foundation.sql"), cancellationToken);

    public async Task ApplyTenantUserRolesAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        var sql = await ReadTenantUserRolesAsync(cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection, transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }

    public static Task<string> ReadTenantUserRolesAsync(CancellationToken cancellationToken) =>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations",
            "0005_tenant_user_roles.sql"), cancellationToken);

    public async Task ApplyClientUserAssignmentsAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        var sql = await ReadClientUserAssignmentsAsync(cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    public static Task<string> ReadClientUserAssignmentsAsync(CancellationToken cancellationToken) =>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations",
            "0028_client_user_assignments.sql"), cancellationToken);

    public string OwnedRunDirectory => runDirectory;
    public string BinaryDirectory => binaryDirectory;

    public async Task ApplyUploadJobsAsync(CancellationToken ct)
    {
        RequireOwnedDirectory();await using var connection=await OpenControlConnectionAsync(ct);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var sql=await File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0016_upload_jobs.sql"),ct);
        await using var command=new NpgsqlCommand(sql,connection,transaction);await command.ExecuteNonQueryAsync(ct);await transaction.CommitAsync(ct);
    }

    public async Task ApplyUploadImportReceiptsAsync(CancellationToken ct)
    {
        RequireOwnedDirectory();await using var connection=await OpenControlConnectionAsync(ct);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var sql=await File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0015_upload_import_receipts.sql"),ct);
        await using var command=new NpgsqlCommand(sql,connection,transaction);await command.ExecuteNonQueryAsync(ct);await transaction.CommitAsync(ct);
    }

    public async Task ApplyUploadDerivedFieldsAsync(CancellationToken ct)
    {
        RequireOwnedDirectory();
        await using var connection=await OpenControlConnectionAsync(ct);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var sql=await File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0014_upload_derived_fields.sql"),ct);
        await using var command=new NpgsqlCommand(sql,connection,transaction);
        await command.ExecuteNonQueryAsync(ct);await transaction.CommitAsync(ct);
    }

    public async Task ApplyUploadImportsAsync(CancellationToken ct)
    {
        RequireOwnedDirectory();
        await using var connection=await OpenControlConnectionAsync(ct);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var sql=await File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0013_upload_imports.sql"),ct);
        await using var command=new NpgsqlCommand(sql,connection,transaction);
        await command.ExecuteNonQueryAsync(ct);await transaction.CommitAsync(ct);
    }

    public async Task ApplyUploadValidationPlansAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection=await OpenControlConnectionAsync(cancellationToken);
        await using var transaction=await connection.BeginTransactionAsync(cancellationToken);
        var sql=await ReadUploadValidationPlansAsync(cancellationToken);
        await using var command=new NpgsqlCommand(sql,connection,transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);await transaction.CommitAsync(cancellationToken);
    }
    public static Task<string> ReadUploadValidationPlansAsync(CancellationToken cancellationToken)=>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0012_upload_validation_plans.sql"),cancellationToken);

    public async Task ApplySavedMappingsAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection=await OpenControlConnectionAsync(cancellationToken);
        await using var transaction=await connection.BeginTransactionAsync(cancellationToken);
        await using var command=new NpgsqlCommand(await ReadSavedMappingsAsync(cancellationToken),connection,transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }
    public static Task<string> ReadSavedMappingsAsync(CancellationToken cancellationToken)=>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0010_tenant_saved_mappings.sql"),cancellationToken);

    public async Task ApplyTenantTemplatesAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection=await OpenControlConnectionAsync(cancellationToken);
        await using var transaction=await connection.BeginTransactionAsync(cancellationToken);
        await using var command=new NpgsqlCommand(await ReadTenantTemplatesAsync(cancellationToken),connection,transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }
    public static Task<string> ReadTenantTemplatesAsync(CancellationToken cancellationToken)=>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(),"database","migrations","0009_tenant_templates.sql"),cancellationToken);

    public async Task ApplyTenantColumnsAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        var sql = await ReadTenantColumnsAsync(cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection, transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }
    public static Task<string> ReadTenantColumnsAsync(CancellationToken cancellationToken) =>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations",
            "0008_tenant_columns.sql"), cancellationToken);

    public async Task ApplyTenantSettingsAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        var sql = await ReadTenantSettingsAsync(cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection, transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }

    public static Task<string> ReadTenantSettingsAsync(CancellationToken cancellationToken) =>
        File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations",
            "0007_tenant_settings.sql"), cancellationToken);

    public async Task ApplyReconciliationSchemaAsync(CancellationToken cancellationToken)
    {
        await ApplyAuditSchemaAsync(cancellationToken);
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        // Synthetic client reference fixture; no canonical source rows are
        // supplied or written by the rehearsal tool itself.
        await using (var clients = new NpgsqlCommand("""
            CREATE TABLE IF NOT EXISTS public.clients(id INTEGER PRIMARY KEY);
            INSERT INTO public.clients(id) VALUES (5),(6) ON CONFLICT DO NOTHING
            """, connection, transaction)) await clients.ExecuteNonQueryAsync(cancellationToken);
        var sql = await File.ReadAllTextAsync(Path.Combine(FindSolutionDirectory(), "database", "migrations", "0002_log_reconciliation.sql"), cancellationToken);
        await using var command = new NpgsqlCommand(sql, connection, transaction);
        await command.ExecuteNonQueryAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
    }

    public async Task<NpgsqlConnection> OpenEmptyOwnedDatabaseAsync(CancellationToken cancellationToken)
    {
        RequireOwnedDirectory();
        var name = "auditplatform_foundation_" + Guid.NewGuid().ToString("N");
        await using (var owner = await OpenControlConnectionAsync(cancellationToken))
        {
            await using var create = new NpgsqlCommand($"CREATE DATABASE \"{name}\"", owner);
            await create.ExecuteNonQueryAsync(cancellationToken);
        }
        var settings = new NpgsqlConnectionStringBuilder(ConnectionString) { Database = name };
        RequireSafeTarget(settings.ConnectionString, Port);
        var connection = new NpgsqlConnection(settings.ConnectionString);
        try { await connection.OpenAsync(cancellationToken); return connection; }
        catch { await connection.DisposeAsync(); throw; }
    }

    public async Task<int> CreateSourceRowAsync(int clientId, CancellationToken cancellationToken)
    {
        await using var connection = await OpenControlConnectionAsync(cancellationToken);
        await using var command = new NpgsqlCommand("INSERT INTO public.client_data(client_id) VALUES ($1) RETURNING id", connection);
        command.Parameters.AddWithValue(clientId);
        return (int)(await command.ExecuteScalarAsync(cancellationToken))!;
    }

    // Test-only transport fault: forward a single connection to this fixture,
    // then drop the server's actual COMMIT completion before the client sees it.
    public sealed class CommitAcknowledgementProxy : IAsyncDisposable
    {
        private readonly TcpListener listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource lifetime = new(TimeSpan.FromSeconds(20));
        private readonly TaskCompletionSource dropped = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly Task running;
        private int armed;
        public int UpstreamPort { get; }
        public int Port { get; }
        public Task AcknowledgementDropped => dropped.Task;

        internal CommitAcknowledgementProxy(int upstreamPort)
        {
            UpstreamPort = upstreamPort;
            listener.Start();
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            running = RunAsync();
        }

        public void Arm() => Interlocked.Exchange(ref armed, 1);

        private async Task RunAsync()
        {
            try
            {
                using var client = await listener.AcceptTcpClientAsync(lifetime.Token);
                using var server = new TcpClient();
                await server.ConnectAsync(IPAddress.Loopback, UpstreamPort, lifetime.Token);
                using var forwardLifetime = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                var downstream = client.GetStream();
                var upstream = server.GetStream();
                var forwarding = downstream.CopyToAsync(upstream, forwardLifetime.Token);
                try
                {
                    var header = new byte[5];
                    while (true)
                    {
                        await upstream.ReadExactlyAsync(header, lifetime.Token);
                        var length = BinaryPrimitives.ReadInt32BigEndian(header.AsSpan(1));
                        if (length is < 4 or > 1048576)
                            throw new IOException("Unexpected local test protocol frame.");
                        var payload = new byte[length - 4];
                        await upstream.ReadExactlyAsync(payload, lifetime.Token);
                        if (Volatile.Read(ref armed) == 1 && header[0] == (byte)'C'
                            && payload.AsSpan().SequenceEqual("COMMIT\0"u8))
                        {
                            dropped.TrySetResult();
                            return;
                        }
                        await downstream.WriteAsync(header, lifetime.Token);
                        await downstream.WriteAsync(payload, lifetime.Token);
                    }
                }
                finally
                {
                    await forwardLifetime.CancelAsync();
                    client.Close();
                    server.Close();
                    try { await forwarding; }
                    catch (Exception error) when (error is IOException or OperationCanceledException or SocketException) { }
                }
            }
            catch (Exception error) when (error is IOException or OperationCanceledException or SocketException)
            {
                if (!lifetime.IsCancellationRequested)
                    dropped.TrySetException(error);
            }
        }

        public async ValueTask DisposeAsync()
        {
            await lifetime.CancelAsync();
            listener.Stop();
            await running;
            lifetime.Dispose();
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (!initialized)
            return;
        RequireOwnedDirectory();
        // Only stop the cluster created by this fixture. Keep ignored artifacts
        // for diagnosis; no recursive deletion or existing-database cleanup.
        if (File.Exists(Path.Combine(DataDirectory, "postmaster.pid")))
            await RunToolAsync("pg_ctl", "-D", DataDirectory, "-m", "immediate", "-w", "-t", "20", "stop");
        initialized = false;
    }

    private string DataDirectory => Path.Combine(runDirectory, "data");

    private void RequireOwnedDirectory()
    {
        var expected = Path.Combine(FindSolutionDirectory(), ".tools", "postgres-tests", "run-" + marker);
        if (Path.GetFullPath(runDirectory) != expected
            || File.ReadAllText(Path.Combine(runDirectory, "fixture-owner")) != marker)
            throw new InvalidOperationException("Refusing to operate on an unowned test cluster.");
    }

    private async Task RunToolAsync(string name, params string[] arguments)
    {
        RequireOwnedDirectory();
        // On Windows the detached server can inherit redirected pg_ctl pipe
        // handles. Capture initdb only; pg_ctl's server output goes to -l.
        var capture = name == "initdb";
        var start = new ProcessStartInfo(Path.Combine(binaryDirectory, Executable(name)))
        {
            WorkingDirectory = runDirectory, UseShellExecute = false,
            CreateNoWindow = true, RedirectStandardOutput = capture, RedirectStandardError = capture
        };
        foreach (var argument in arguments)
            start.ArgumentList.Add(argument);
        // Ignore inherited libpq settings; only our explicit local cluster counts.
        foreach (var key in start.Environment.Keys.Where(key => key.StartsWith("PG", StringComparison.Ordinal)).ToArray())
            start.Environment.Remove(key);
        using var process = Process.Start(start)
            ?? throw new InvalidOperationException("Could not start local PostgreSQL test tool.");
        var output = capture ? process.StandardOutput.ReadToEndAsync() : Task.FromResult("");
        var error = capture ? process.StandardError.ReadToEndAsync() : Task.FromResult("");
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(35));
        try
        {
            await process.WaitForExitAsync(deadline.Token);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            throw new TimeoutException("Local PostgreSQL test tool exceeded its time limit.");
        }
        var text = await output + await error;
        await File.WriteAllTextAsync(Path.Combine(runDirectory, name + "-last.log"), text);
        if (process.ExitCode != 0)
            throw new InvalidOperationException($"Local {name} failed; inspect ignored fixture logs.");
    }

    private static int ReservePort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }

    private static string Executable(string name) => OperatingSystem.IsWindows() ? name + ".exe" : name;

    private static string FindSolutionDirectory()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "AuditPlatform.slnx")))
                return directory.FullName;
        throw new InvalidOperationException("Cannot locate the isolated migration solution.");
    }
}
