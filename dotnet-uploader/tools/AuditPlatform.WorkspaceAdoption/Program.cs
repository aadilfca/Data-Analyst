using System.Data;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace AuditPlatform.WorkspaceAdoption;

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        try
        {
            var options = ParseArguments(args);
            var connectionString = Environment.GetEnvironmentVariable("AUDIT_ADOPTION_CONNECTION") ?? "";
            var settings = ValidateLocalConnection(connectionString);
            await using var connection = new NpgsqlConnection(settings.ConnectionString);
            await connection.OpenAsync();
            var ownerDirectory = Path.GetFullPath(options["owner-dir"]);
            var marker = File.ReadAllText(Path.Combine(ownerDirectory, "cluster-marker"));
            if (!Guid.TryParseExact(marker, "N", out _)) throw new InvalidOperationException("Invalid owned-cluster marker.");
            await using (var command = new NpgsqlCommand("SELECT current_setting('data_directory'),host(inet_server_addr()),host(inet_client_addr())", connection))
            await using (var reader = await command.ExecuteReaderAsync())
            {
                await reader.ReadAsync();
                if (!string.Equals(Path.GetFullPath(reader.GetString(0)), Path.Combine(ownerDirectory, "cluster"),
                        OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal)
                    || reader.GetString(1) != "127.0.0.1" || reader.GetString(2) != "127.0.0.1")
                    throw new InvalidOperationException("Target is not the supplied owned local cluster.");
            }
            var stage = options.GetValueOrDefault("stage", "workspace");
            if (stage == "templates") return await TemplateAdoption.RunAsync(connection, options);
            if (stage == "upload-bindings") return await UploadBindingAdoption.RunAsync(connection, options);
            if (stage == "audit-rules") return await RuleAdoption.RunAsync(connection, options);
            if (stage != "workspace") throw new InvalidOperationException("Unknown adoption stage.");
            var excluded = options.GetValueOrDefault("unassigned-users", "").Split(',', StringSplitOptions.RemoveEmptyEntries)
                .Select(int.Parse).Distinct().Order().ToArray();
            var name = options.GetValueOrDefault("workspace-name", "Local audit-firm adoption rehearsal").Trim();
            if (name.Length is < 1 or > 200 || excluded.Any(id => id <= 0)) throw new InvalidOperationException("Invalid adoption selection.");
            var operation = options.TryGetValue("operation", out var operationValue) ? Guid.Parse(operationValue) : Guid.Empty;
            var apply = options.ContainsKey("apply");
            if (apply && (operation == Guid.Empty || !options.ContainsKey("expected")))
                throw new InvalidOperationException("Apply requires an operation ID and the preview fingerprint.");

            await using var transaction = await connection.BeginTransactionAsync(IsolationLevel.Serializable);
            async Task<object?> Scalar(string query, params object[] values)
            {
                await using var command = new NpgsqlCommand(query, connection, transaction);
                foreach (var value in values) command.Parameters.AddWithValue(value);
                return await command.ExecuteScalarAsync();
            }
            async Task Execute(string query, params object[] values)
            {
                await using var command = new NpgsqlCommand(query, connection, transaction);
                foreach (var value in values) command.Parameters.AddWithValue(value);
                await command.ExecuteNonQueryAsync();
            }
            if (apply)
            {
                await Scalar("SELECT pg_advisory_xact_lock(699006209)");
                var prior = await Scalar("SELECT details FROM public.audit_log WHERE operation_id=$1 AND action='WorkspaceAdoption' AND module='Migration'", operation);
                if (prior is string priorJson)
                {
                    using var receipt = JsonDocument.Parse(priorJson);
                    if (receipt.RootElement.GetProperty("fingerprint").GetString() != options["expected"])
                        throw new InvalidOperationException("Operation identity cannot be reused for another preview.");
                    Console.WriteLine(priorJson);
                    await transaction.RollbackAsync();
                    return 0;
                }
            }
            else await Execute("SET TRANSACTION READ ONLY");
            if (await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_tenants)") is true)
                throw new InvalidOperationException("This first-adoption tool requires an empty workspace catalog.");
            // Only supported, non-secret settings are copied. Other keys remain in
            // their legacy table and must be inventoried for a separate decision.
            const string supported = "'date_format','amount_grouping','decimal_precision','rate_precision','default_date_range','materiality_threshold','cashflow_fiscal_year_start_month','cashflow_default_period','cashflow_custom_start','cashflow_custom_end','upload_allow_without_backup'";
            var source = (string)(await Scalar($"""
                SELECT jsonb_build_object(
                  'users',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'role',role,'active',is_active) ORDER BY id),'[]') FROM public.users),
                  'clients',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]') FROM public.clients),
                  'settings',(SELECT coalesce(jsonb_agg(jsonb_build_object('key',key,'value',value) ORDER BY key,id),'[]') FROM public.app_settings WHERE key IN ({supported})),
                  'holidays',(SELECT coalesce(jsonb_agg(to_jsonb(h) ORDER BY id),'[]') FROM public.holidays h)
                )::text
                """) ?? throw new InvalidOperationException("Missing source inventory."));
            using var inventory = JsonDocument.Parse(source);
            var users = inventory.RootElement.GetProperty("users").EnumerateArray().ToArray();
            var eligible = ResolveMemberships(users, excluded);
            var selectedSettings = inventory.RootElement.GetProperty("settings").EnumerateArray().ToArray();
            if (selectedSettings.Any(setting => setting.GetProperty("value").ValueKind != JsonValueKind.String)
                || selectedSettings.GroupBy(setting => setting.GetProperty("key").GetString()).Any(group => group.Count() != 1))
                throw new InvalidOperationException("Settings contain null or duplicate values; review before adoption.");
            var fingerprint = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(
                source + "\n" + name + "\n" + string.Join(',', excluded))));
            var counts = new
            {
                preservedUsers = users.Length,
                mappedUsers = eligible.Length,
                unassignedUsers = users.Length - eligible.Length,
                clients = inventory.RootElement.GetProperty("clients").GetArrayLength(),
                settings = selectedSettings.Length,
                holidays = inventory.RootElement.GetProperty("holidays").GetArrayLength()
            };
            if (!apply)
            {
                Console.WriteLine(JsonSerializer.Serialize(new { status = "preview", fingerprint, counts }));
                await transaction.RollbackAsync();
                return 0;
            }
            if (!string.Equals(options["expected"], fingerprint, StringComparison.Ordinal))
                throw new InvalidOperationException("Source or selection changed since preview.");
            var tenant = (int)(await Scalar("INSERT INTO public.platform_tenants(name,tenant_type) VALUES($1,'audit_firm') RETURNING id", name))!;
            foreach (var user in eligible)
            {
                await Execute("INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,is_active,tenant_role) VALUES($1,$2,$3,$4)",
                    tenant, user.GetProperty("id").GetInt32(), user.GetProperty("active").ValueKind == JsonValueKind.True,
                    user.GetProperty("role").GetString() == "admin" ? "admin" : "member");
            }
            await Execute("INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) SELECT id,$1 FROM public.clients", tenant);
            await Execute($"INSERT INTO public.platform_tenant_settings(tenant_id,key,value) SELECT $1,key,value FROM public.app_settings WHERE key IN ({supported})", tenant);
            await Execute("""
                INSERT INTO public.platform_tenant_holidays(id,tenant_id,holiday_date,label,repeats_yearly,created_at)
                OVERRIDING SYSTEM VALUE SELECT id,$1,holiday_date,label,repeats_yearly,created_at FROM public.holidays
                """, tenant);
            if (counts.holidays > 0)
                await Scalar("SELECT setval(pg_get_serial_sequence('public.platform_tenant_holidays','id'),(SELECT max(id) FROM public.platform_tenant_holidays),true)");
            var outcome = JsonSerializer.Serialize(new { status = "completed", operation, tenantId = tenant, fingerprint, counts });
            await Execute("""
                INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,actor_kind,source_origin)
                VALUES('System','WorkspaceAdoption','Migration',$1,$2,$3,'system','owned_local_rehearsal')
                """, outcome, Guid.NewGuid(), operation);
            // A lost acknowledgement must be reconciled with the same operation ID;
            // never automatically replay the writes under a new operation.
            try { await transaction.CommitAsync(); }
            catch { Console.WriteLine("{\"status\":\"commit_outcome_unknown\"}"); return 2; }
            Console.WriteLine(outcome);
            return 0;
        }
        catch (Exception error)
        {
            Console.WriteLine(JsonSerializer.Serialize(new { status = "rejected", error = error.GetType().Name,
                sqlstate = (error as PostgresException)?.SqlState,
                reason = error is InvalidOperationException ? error.Message : "Inspect the protected operator input; no automatic retry was made." }));
            return 1;
        }
    }

    public static NpgsqlConnectionStringBuilder ValidateLocalConnection(string connectionString)
    {
        var settings = new NpgsqlConnectionStringBuilder(connectionString);
        // Validate before opening any socket; the owned-cluster marker is checked
        // separately against PostgreSQL's actual data directory after connection.
        if (settings.Host != "127.0.0.1" || settings.Port < 1024
            || settings.Database?.StartsWith("backup_rehearsal_", StringComparison.Ordinal) != true)
            throw new InvalidOperationException("Only an owned loopback recovery database is accepted.");
        settings.Timeout = 5;
        settings.CommandTimeout = 30;
        settings.IncludeErrorDetail = false;
        settings.LogParameters = false;
        return settings;
    }

    public static JsonElement[] ResolveMemberships(JsonElement[] users, int[] excluded)
    {
        foreach (var user in users)
        {
            var role = user.GetProperty("role").GetString();
            var active = user.GetProperty("active").ValueKind == JsonValueKind.True;
            if (active && role is not ("admin" or "analyst") && !excluded.Contains(user.GetProperty("id").GetInt32()))
                throw new InvalidOperationException("An active account needs an explicit role or exclusion decision.");
        }
        if (excluded.Any(id => !users.Any(user => user.GetProperty("id").GetInt32() == id)))
            throw new InvalidOperationException("An excluded user does not exist.");
        var eligible = users.Where(user => !excluded.Contains(user.GetProperty("id").GetInt32())
            && user.GetProperty("role").GetString() is "admin" or "analyst").ToArray();
        if (!eligible.Any(user => user.GetProperty("role").GetString() == "admin"
                && user.GetProperty("active").ValueKind == JsonValueKind.True))
            throw new InvalidOperationException("An existing active administrator is required.");
        return eligible;
    }

    public static Dictionary<string, string> ParseArguments(string[] args)
    {
        var allowed = new HashSet<string> { "owner-dir", "workspace-name", "unassigned-users", "operation", "expected", "apply", "stage", "tenant-id", "retain-unresolved", "binding-plan", "actor-username" };
        var options = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var index = 0; index < args.Length; index++)
        {
            if (!args[index].StartsWith("--", StringComparison.Ordinal)) throw new InvalidOperationException("Use named operator options.");
            var key = args[index][2..];
            if (!allowed.Contains(key) || options.ContainsKey(key)) throw new InvalidOperationException("Unknown or duplicate option.");
            options[key] = key is "apply" or "retain-unresolved" ? "true" : ++index < args.Length ? args[index] : throw new InvalidOperationException("Missing option value.");
        }
        if (!options.ContainsKey("owner-dir")) throw new InvalidOperationException("An owned recovery directory is required.");
        return options;
    }
}
