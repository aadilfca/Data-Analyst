using System.Data;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace AuditPlatform.FirstRunSetup;

public static class Program
{
    private const string ProjectRef = "klljugkujxxmtbmbeewn";
    private const string DirectHost = "db.klljugkujxxmtbmbeewn.supabase.co";
    private const string PoolerUser = "postgres.klljugkujxxmtbmbeewn";
    private const string DefaultCertificate = @"C:\Users\LENOVO\Downloads\prod-ca-2021 (4).crt";

    public static async Task<int> Main()
    {
        try
        {
            PrintHeader();
            var target = ReadTarget();
            var setup = ReadInitialSetup();
            string passwordHash;
            try { passwordHash = BCrypt.Net.BCrypt.HashPassword(new string(setup.Password)); }
            finally { Array.Clear(setup.Password); }
            var connectionString = ReadOwnerConnection(target);

            await using var connection = new NpgsqlConnection(connectionString);
            await connection.OpenAsync();
            await VerifyConnectionTargetAsync(connection);
            var initialInventory = await ReadEmptySchemaInventoryAsync(connection, null);
            PrintPreview(target, setup, initialInventory);

            var confirmation = ReadRequired("Type INITIALIZE " + ProjectRef + " to create these three setup records");
            if (!string.Equals(confirmation, "INITIALIZE " + ProjectRef, StringComparison.Ordinal))
            {
                Console.WriteLine("Cancelled. No database rows were written.");
                return 0;
            }

            await ApplyAsync(connection, setup, passwordHash);
            Console.WriteLine("Setup completed. No client transactions or reusable definitions were copied.");
            return 0;
        }
        catch (OperationCanceledException)
        {
            Console.WriteLine("Cancelled. No database rows were written.");
            return 0;
        }
        catch (Exception error)
        {
            var response = new
            {
                status = "rejected",
                error = error.GetType().Name,
                sqlstate = (error as PostgresException)?.SqlState,
                reason = error is SetupValidationException or FileNotFoundException
                    ? error.Message
                    : "The setup did not report success. Check the protected database state before trying again. No automatic retry was made."
            };
            Console.WriteLine(JsonSerializer.Serialize(response));
            return 1;
        }
    }

    private static void PrintHeader()
    {
        Console.WriteLine("C# Supabase first-run setup");
        Console.WriteLine("Creates one administrator, one workspace, and one blank client card.");
        Console.WriteLine("This tool never creates tables, grants permissions, or copies existing records.");
        Console.WriteLine("Owner credentials are for this controlled setup only; the app must use its restricted runtime login.");
        Console.WriteLine();
    }

    private static TargetSettings ReadTarget()
    {
        var host = ReadRequired("Supabase direct host or Session Pooler host").Trim().ToLowerInvariant();
        if (host != DirectHost && !(host.EndsWith(".pooler.supabase.com", StringComparison.Ordinal)
                                    && host.Length > ".pooler.supabase.com".Length))
            throw new SetupValidationException("Host must be this project's direct endpoint or a Supabase pooler endpoint.");

        var portText = ReadRequired("Database port (Direct or Session Pooler uses 5432)");
        if (!int.TryParse(portText, out var port) || port != 5432)
            throw new SetupValidationException("Use port 5432 for the Direct or Session Pooler connection.");

        var username = ReadRequired("Database owner username").Trim();
        var direct = host == DirectHost && username == "postgres";
        var pooler = host.EndsWith(".pooler.supabase.com", StringComparison.Ordinal)
                     && username == PoolerUser;
        if (!direct && !pooler)
            throw new SetupValidationException("The username does not match the approved project connection format.");

        var certificate = ReadOptional("Supabase Root 2021 CA file", DefaultCertificate);
        certificate = Path.GetFullPath(certificate);
        if (!File.Exists(certificate)) throw new FileNotFoundException("The CA certificate file was not found.", certificate);
        return new TargetSettings(host, port, username, certificate);
    }

    private static string ReadOwnerConnection(TargetSettings target)
    {
        var password = ReadSecret("Database owner password");
        try
        {
            if (password.Length < 1) throw new SetupValidationException("The database owner password is required.");
            var builder = new NpgsqlConnectionStringBuilder
            {
                Host = target.Host,
                Port = target.Port,
                Database = "postgres",
                Username = target.Username,
                Password = new string(password),
                SslMode = SslMode.VerifyFull,
                RootCertificate = target.CertificatePath,
                Pooling = false,
                Timeout = 10,
                CommandTimeout = 30,
                IncludeErrorDetail = false,
                LogParameters = false,
                PersistSecurityInfo = false
            };
            return builder.ConnectionString;
        }
        finally
        {
            // The connection string must hold the password briefly for Npgsql.
            // Its value is never printed, logged, or written to disk.
            Array.Clear(password);
        }
    }

    private static InitialSetup ReadInitialSetup()
    {
        var username = ReadRequired("Initial administrator username").Trim().ToLowerInvariant();
        if (username.Length is < 3 or > 50 || username.Any(ch => !(char.IsAsciiLetterLower(ch) || char.IsAsciiDigit(ch) || ch is '.' or '_' or '-')))
            throw new SetupValidationException("Use 3–50 lowercase letters, numbers, dots, underscores, or dashes for the administrator username.");

        var workspace = ReadRequired("Workspace name").Trim();
        if (workspace.Length is < 1 or > 200) throw new SetupValidationException("Workspace name must be 1–200 characters.");

        var clientCode = ReadRequired("First blank client card code").Trim().ToUpperInvariant();
        if (clientCode.Length is < 1 or > 30 || clientCode.Any(ch => !(char.IsAsciiLetterOrDigit(ch) || ch is '-' or '_')))
            throw new SetupValidationException("Client code must be 1–30 letters, numbers, dashes, or underscores.");

        var clientName = ReadRequired("First blank client card name").Trim();
        if (clientName.Length is < 1 or > 200) throw new SetupValidationException("Client name must be 1–200 characters.");

        var password = ReadSecret("New administrator password (12 or more characters)");
        try
        {
            if (password.Length < 12) throw new SetupValidationException("Use a password with at least 12 characters.");
            if (Encoding.UTF8.GetByteCount(password) > 72)
                throw new SetupValidationException("The password must be no more than 72 UTF-8 bytes for compatibility with C# password management.");
            var repeated = ReadSecret("Repeat new administrator password");
            try
            {
                if (!password.AsSpan().SequenceEqual(repeated))
                    throw new SetupValidationException("The two administrator passwords did not match.");
            }
            finally { Array.Clear(repeated); }
            return new InitialSetup(username, workspace, clientCode, clientName, password);
        }
        catch
        {
            Array.Clear(password);
            throw;
        }
    }

    private static async Task VerifyConnectionTargetAsync(NpgsqlConnection connection)
    {
        await using var command = new NpgsqlCommand("""
            SELECT current_database(), EXISTS (
                SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl IS TRUE)
            """, connection);
        await using var reader = await command.ExecuteReaderAsync();
        if (!await reader.ReadAsync()
            || reader.GetString(0) != "postgres"
            || !reader.GetBoolean(1))
            throw new SetupValidationException("The connection did not reach the postgres database over verified TLS.");
    }

    private static async Task<Inventory> ReadEmptySchemaInventoryAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction)
    {
        var required = new Dictionary<string, string[]>(StringComparer.Ordinal)
        {
            ["users"] = ["id", "username", "password_hash", "is_active", "role"],
            ["clients"] = ["id", "client_code", "client_name", "is_active"],
            ["platform_tenants"] = ["id", "name", "tenant_type", "is_active"],
            ["platform_tenant_memberships"] = ["tenant_id", "user_id", "is_active", "tenant_role"],
            ["platform_tenant_client_homes"] = ["client_id", "tenant_id"],
            ["sessions"] = ["token", "user_id", "expiry", "selected_tenant_id", "context_version"],
            ["audit_log"] = ["id", "user", "action", "module", "details", "event_id", "operation_id",
                "actor_user_id", "actor_kind", "client_id", "correlation_id", "source_origin"]
        };

        var existing = new HashSet<string>(StringComparer.Ordinal);
        await using (var command = new NpgsqlCommand("""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name = ANY($1)
            """, connection, transaction))
        {
            command.Parameters.AddWithValue(required.Keys.ToArray());
            await using var reader = await command.ExecuteReaderAsync();
            while (await reader.ReadAsync()) existing.Add(reader.GetString(0) + "." + reader.GetString(1));
        }
        foreach (var table in required)
        foreach (var column in table.Value)
            if (!existing.Contains(table.Key + "." + column))
                throw new SetupValidationException($"Required table shape is missing {table.Key}.{column}; review and apply the approved migrations first.");

        var tables = new List<string>();
        await using (var command = new NpgsqlCommand("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'
            ORDER BY table_name
            """, connection, transaction))
        {
            await using var reader = await command.ExecuteReaderAsync();
            while (await reader.ReadAsync()) tables.Add(reader.GetString(0));
        }
        if (tables.Count == 0) throw new SetupValidationException("The reviewed application tables are not present yet.");

        var populated = new List<string>();
        foreach (var table in tables)
        {
            if (table == "error_codes")
            {
                await using var command = new NpgsqlCommand("""
                    SELECT count(*) FILTER (WHERE code <> 'ERR-UNK-001') + greatest(count(*) - 1, 0)
                    FROM public.error_codes
                    """, connection, transaction);
                if (Convert.ToInt64(await command.ExecuteScalarAsync()) != 0)
                    populated.Add("error_codes (unexpected reference rows)");
                continue;
            }
            await using var check = new NpgsqlCommand($"SELECT EXISTS (SELECT 1 FROM public.{QuoteIdentifier(table)} LIMIT 1)", connection, transaction);
            if (Convert.ToBoolean(await check.ExecuteScalarAsync())) populated.Add(table);
        }
        if (populated.Count > 0)
            throw new SetupValidationException("First-run setup requires an otherwise empty app schema. Populated tables: " + string.Join(", ", populated));

        return new Inventory(tables.Count);
    }

    private static void PrintPreview(TargetSettings target, InitialSetup setup, Inventory inventory)
    {
        Console.WriteLine();
        Console.WriteLine("Read-only preflight passed.");
        Console.WriteLine($"Target: {target.Host}:{target.Port} / postgres");
        Console.WriteLine($"Project: {ProjectRef}; TLS: VerifyFull with the supplied root certificate");
        Console.WriteLine($"Tables checked: {inventory.TableCount}; existing non-setup application rows: none");
        Console.WriteLine($"Will create: administrator '{setup.Username}', workspace '{setup.WorkspaceName}', client '{setup.ClientCode}' / '{setup.ClientName}'");
        Console.WriteLine("The client card will be blank; no client transactions, reusable definitions, users, or existing rows will be copied.");
    }

    private static async Task ApplyAsync(NpgsqlConnection connection, InitialSetup setup, string passwordHash)
    {
        await using var transaction = await connection.BeginTransactionAsync(IsolationLevel.Serializable);
        try
        {
            await using (var lockCommand = new NpgsqlCommand("SELECT pg_advisory_xact_lock(699006217)", connection, transaction))
                await lockCommand.ExecuteNonQueryAsync();
            await ReadEmptySchemaInventoryAsync(connection, transaction);

            int tenantId;
            int clientId;
            int userId;
            await using (var command = new NpgsqlCommand("""
                INSERT INTO public.users(username,password_hash,is_active,role)
                VALUES($1,$2,TRUE,NULL) RETURNING id
                """, connection, transaction))
            {
                command.Parameters.AddWithValue(setup.Username);
                command.Parameters.AddWithValue(passwordHash);
                userId = Convert.ToInt32(await command.ExecuteScalarAsync());
            }
            await using (var command = new NpgsqlCommand("""
                INSERT INTO public.platform_tenants(name,tenant_type,is_active)
                VALUES($1,'audit_firm',TRUE) RETURNING id
                """, connection, transaction))
            {
                command.Parameters.AddWithValue(setup.WorkspaceName);
                tenantId = Convert.ToInt32(await command.ExecuteScalarAsync());
            }
            await using (var command = new NpgsqlCommand("""
                INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,is_active,tenant_role)
                VALUES($1,$2,TRUE,'admin')
                """, connection, transaction))
            {
                command.Parameters.AddWithValue(tenantId);
                command.Parameters.AddWithValue(userId);
                await command.ExecuteNonQueryAsync();
            }
            await using (var command = new NpgsqlCommand("""
                INSERT INTO public.clients(client_code,client_name,is_active)
                VALUES($1,$2,TRUE) RETURNING id
                """, connection, transaction))
            {
                command.Parameters.AddWithValue(setup.ClientCode);
                command.Parameters.AddWithValue(setup.ClientName);
                clientId = Convert.ToInt32(await command.ExecuteScalarAsync());
            }
            await using (var command = new NpgsqlCommand("""
                INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id)
                VALUES($1,$2)
                """, connection, transaction))
            {
                command.Parameters.AddWithValue(clientId);
                command.Parameters.AddWithValue(tenantId);
                await command.ExecuteNonQueryAsync();
            }
            await using (var command = new NpgsqlCommand("""
                INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,actor_kind,source_origin)
                VALUES('System','FirstRunSetup','Migration',
                    'Initial administrator, workspace and blank client created by guarded setup.',$1,$2,'system','supabase_development')
                """, connection, transaction))
            {
                command.Parameters.AddWithValue(Guid.NewGuid());
                command.Parameters.AddWithValue(Guid.NewGuid());
                await command.ExecuteNonQueryAsync();
            }

            try { await transaction.CommitAsync(); }
            catch
            {
                Console.WriteLine("Commit outcome unknown. Do not rerun; inspect the database before deciding what to do.");
                throw new CommitOutcomeUnknownException();
            }
            Console.WriteLine($"Created administrator ID {userId}, workspace ID {tenantId}, and blank client ID {clientId}.");
        }
        catch (CommitOutcomeUnknownException) { throw; }
        catch
        {
            await transaction.RollbackAsync();
            throw;
        }
    }

    private static string QuoteIdentifier(string name) => "\"" + name.Replace("\"", "\"\"", StringComparison.Ordinal) + "\"";

    private static string ReadRequired(string label)
    {
        Console.Write(label + ": ");
        return Console.ReadLine() ?? throw new OperationCanceledException();
    }

    private static string ReadOptional(string label, string defaultValue)
    {
        Console.Write($"{label} [Enter uses the supplied local certificate path]: ");
        var value = Console.ReadLine();
        return string.IsNullOrWhiteSpace(value) ? defaultValue : value.Trim();
    }

    private static char[] ReadSecret(string label)
    {
        Console.Write(label + ": ");
        var value = new StringBuilder();
        while (true)
        {
            var key = Console.ReadKey(intercept: true);
            if (key.Key == ConsoleKey.Enter) break;
            if (key.Key == ConsoleKey.Backspace)
            {
                if (value.Length > 0) value.Length--;
                continue;
            }
            if (!char.IsControl(key.KeyChar)) value.Append(key.KeyChar);
        }
        Console.WriteLine();
        var secret = value.ToString().ToCharArray();
        value.Clear();
        return secret;
    }

    private sealed record TargetSettings(string Host, int Port, string Username, string CertificatePath);
    private sealed record InitialSetup(string Username, string WorkspaceName, string ClientCode, string ClientName, char[] Password);
    private sealed record Inventory(int TableCount);
    private sealed class SetupValidationException(string message) : Exception(message) { }
    private sealed class CommitOutcomeUnknownException : Exception { }
}
