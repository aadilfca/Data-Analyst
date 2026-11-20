using System.Data;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace AuditPlatform.WorkspaceAdoption;

public static class SupabaseDevelopmentAdoption
{
    private const string ConnectionEnvironmentVariable = "AUDIT_ADOPTION_SUPABASE_CONNECTION";
    private const string CertificateEnvironmentVariable = "AUDIT_ADOPTION_SUPABASE_ROOT_CERT";
    private const string AllowedHost = "aws-0-ap-south-1.pooler.supabase.com";
    private const string AllowedUsername = "postgres.klljugkujxxmtbmbeewn";
    private const string SupportedSettings = "'date_format','amount_grouping','decimal_precision','rate_precision','default_date_range','materiality_threshold','cashflow_fiscal_year_start_month','cashflow_default_period','cashflow_custom_start','cashflow_custom_end','upload_allow_without_backup'";

    public static async Task<int> RunAsync(Dictionary<string, string> options)
    {
        try
        {
            var stage = options.GetValueOrDefault("stage", "workspace");
            if (stage == "templates")
                ValidateTemplateOptions(options);
            else if (stage != "workspace"
                || options.ContainsKey("owner-dir") || options.ContainsKey("unassigned-users")
                || options.ContainsKey("tenant-id") || options.ContainsKey("retain-unresolved")
                || options.ContainsKey("binding-plan") || options.ContainsKey("actor-username"))
                throw new InvalidOperationException("The Supabase development target supports only first workspace adoption.");

            var connectionString = Environment.GetEnvironmentVariable(ConnectionEnvironmentVariable) ?? "";
            var settings = ValidateConnection(connectionString,
                Environment.GetEnvironmentVariable(CertificateEnvironmentVariable) ?? "");
            await using var connection = new NpgsqlConnection(settings.ConnectionString);
            await connection.OpenAsync();
            await VerifyTargetAsync(connection);

            if (stage == "templates")
            {
                int? requestedTenant = null;
                if (options.TryGetValue("tenant-id", out var tenantText))
                {
                    if (!int.TryParse(tenantText, out var parsedTenant) || parsedTenant <= 0)
                        throw new InvalidOperationException("The requested workspace ID is invalid.");
                    requestedTenant = parsedTenant;
                }
                var activeTenants = new List<int>();
                await using (var tenantCommand = new NpgsqlCommand(
                    "SELECT id FROM public.platform_tenants WHERE tenant_type='audit_firm' AND is_active ORDER BY id", connection))
                await using (var tenantReader = await tenantCommand.ExecuteReaderAsync())
                    while (await tenantReader.ReadAsync()) activeTenants.Add(tenantReader.GetInt32(0));

                var tenantId = TemplateAdoption.ResolveTenantId(activeTenants, requestedTenant);
                var previewOptions = new Dictionary<string, string>(options, StringComparer.Ordinal)
                    { ["tenant-id"] = tenantId.ToString(System.Globalization.CultureInfo.InvariantCulture) };
                return await TemplateAdoption.RunAsync(connection, previewOptions, "supabase_development");
            }

            var name = options.GetValueOrDefault("workspace-name", "Supabase development audit firm").Trim();
            if (name.Length is < 1 or > 200)
                throw new InvalidOperationException("Invalid workspace name.");
            var operation = options.TryGetValue("operation", out var operationValue) ? Guid.Parse(operationValue) : Guid.Empty;
            var apply = options.ContainsKey("apply");
            if (apply && (operation == Guid.Empty || !options.ContainsKey("expected")))
                throw new InvalidOperationException("Apply requires the preview fingerprint and a unique operation ID.");

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
            else
                await Execute("SET TRANSACTION READ ONLY");

            if (await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_tenants)") is true)
                throw new InvalidOperationException("This first-adoption tool requires an empty workspace catalog.");

            var source = await Scalar($"""
                SELECT jsonb_build_object(
                  'users',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'role',role,'active',is_active) ORDER BY id),'[]') FROM public.users),
                  'clients',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]') FROM public.clients),
                  'settings',(SELECT coalesce(jsonb_agg(jsonb_build_object('key',key,'value',value) ORDER BY key,id),'[]') FROM public.app_settings WHERE key IN ({SupportedSettings})),
                  'holidays',(SELECT coalesce(jsonb_agg(to_jsonb(h) ORDER BY id),'[]') FROM public.holidays h)
                )::text
                """) as string ?? throw new InvalidOperationException("Missing source inventory.");
            using var inventory = JsonDocument.Parse(source);
            var users = inventory.RootElement.GetProperty("users").EnumerateArray().ToArray();
            var pendingRoleUsers = users.Where(user =>
                user.GetProperty("active").ValueKind == JsonValueKind.True
                && user.GetProperty("role").ValueKind == JsonValueKind.Null).ToArray();
            var unsupportedActiveRoles = users.Where(user =>
            {
                if (user.GetProperty("active").ValueKind != JsonValueKind.True) return false;
                var role = user.GetProperty("role").GetString();
                return role is not null and not ("admin" or "analyst");
            }).ToArray();
            if (pendingRoleUsers.Length != 1 || unsupportedActiveRoles.Length != 0)
                throw new InvalidOperationException("Active role review differs from the approved one-unassigned-account mapping.");

            var excluded = pendingRoleUsers.Select(user => user.GetProperty("id").GetInt32()).ToArray();
            var eligible = Program.ResolveMemberships(users, excluded);
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
            var roleCounts = new
            {
                activeAdmins = users.Count(user => user.GetProperty("active").ValueKind == JsonValueKind.True && user.GetProperty("role").GetString() == "admin"),
                inactiveAdmins = users.Count(user => user.GetProperty("active").ValueKind != JsonValueKind.True && user.GetProperty("role").GetString() == "admin"),
                activeAnalysts = users.Count(user => user.GetProperty("active").ValueKind == JsonValueKind.True && user.GetProperty("role").GetString() == "analyst"),
                inactiveAnalysts = users.Count(user => user.GetProperty("active").ValueKind != JsonValueKind.True && user.GetProperty("role").GetString() == "analyst"),
                activeRoleless = pendingRoleUsers.Length,
                inactiveUnmapped = users.Count(user => user.GetProperty("active").ValueKind != JsonValueKind.True
                    && user.GetProperty("role").GetString() is not ("admin" or "analyst"))
            };
            var missingSettingsSchema = await Scalar("SELECT to_regclass('public.platform_tenant_settings') IS NULL OR to_regclass('public.platform_tenant_holidays') IS NULL") is true;
            if (!apply)
            {
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    status = "preview",
                    fingerprint,
                    counts,
                    roleCounts,
                    requiredMigration = missingSettingsSchema ? "0007_tenant_settings.sql" : null
                }));
                await transaction.RollbackAsync();
                return 0;
            }
            if (missingSettingsSchema)
                throw new InvalidOperationException("Apply requires reviewed migration 0007_tenant_settings.sql to be applied separately first.");
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
            await Execute($"INSERT INTO public.platform_tenant_settings(tenant_id,key,value) SELECT $1,key,value FROM public.app_settings WHERE key IN ({SupportedSettings})", tenant);
            await Execute("""
                INSERT INTO public.platform_tenant_holidays(id,tenant_id,holiday_date,label,repeats_yearly,created_at)
                OVERRIDING SYSTEM VALUE SELECT id,$1,holiday_date,label,repeats_yearly,created_at FROM public.holidays
                """, tenant);
            if (counts.holidays > 0)
                await Scalar("SELECT setval(pg_get_serial_sequence('public.platform_tenant_holidays','id'),(SELECT max(id) FROM public.platform_tenant_holidays),true)");
            var outcome = JsonSerializer.Serialize(new { status = "completed", operation, tenantId = tenant, fingerprint, counts });
            await Execute("""
                INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,actor_kind,source_origin)
                VALUES('System','WorkspaceAdoption','Migration',$1,$2,$3,'system','supabase_development')
                """, outcome, Guid.NewGuid(), operation);
            try
            {
                await transaction.CommitAsync();
            }
            catch
            {
                Console.WriteLine("{\"status\":\"commit_outcome_unknown\"}");
                return 2;
            }
            Console.WriteLine(outcome);
            return 0;
        }
        catch (Exception error)
        {
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                status = "rejected",
                error = error.GetType().Name,
                sqlstate = (error as PostgresException)?.SqlState,
                missingSchema = (error as PostgresException)?.SqlState == "42P01" ? (error as PostgresException)?.SchemaName : null,
                missingRelation = (error as PostgresException)?.SqlState == "42P01" ? (error as PostgresException)?.TableName : null,
                reason = error is InvalidOperationException ? error.Message
                    : error is PostgresException { SqlState: "42P01" } missing ? missing.MessageText
                    : "Inspect the protected operator input; no automatic retry was made."
            }));
            return 1;
        }
    }

    public static void ValidateTemplateOptions(IReadOnlyDictionary<string, string> options)
    {
        var allowed = new HashSet<string>(StringComparer.Ordinal)
            { "target", "stage", "tenant-id", "retain-unresolved", "apply", "operation", "expected" };
        if (options.Keys.Any(key => !allowed.Contains(key))
            || options.GetValueOrDefault("target") != "supabase-development"
            || options.GetValueOrDefault("stage") != "templates")
            throw new InvalidOperationException("The Supabase template stage accepts only its scoped adoption options.");

        var apply = options.ContainsKey("apply");
        var hasOperation = options.TryGetValue("operation", out var operationText);
        var hasFingerprint = options.TryGetValue("expected", out var fingerprint);
        if (!apply)
        {
            if (hasOperation || hasFingerprint)
                throw new InvalidOperationException("Apply identity options require --apply.");
            return;
        }

        if (!options.ContainsKey("retain-unresolved"))
            throw new InvalidOperationException("Apply requires the explicit --retain-unresolved review decision.");
        if (!hasOperation || !Guid.TryParse(operationText, out var operation) || operation == Guid.Empty)
            throw new InvalidOperationException("Apply requires a unique, non-empty operation ID.");
        if (!hasFingerprint || fingerprint is null || fingerprint.Length != 64
            || fingerprint.Any(character => !Uri.IsHexDigit(character))
            || !string.Equals(fingerprint, fingerprint.ToLowerInvariant(), StringComparison.Ordinal))
            throw new InvalidOperationException("Apply requires the lowercase SHA-256 fingerprint from the matching preview.");
    }

    private static NpgsqlConnectionStringBuilder ValidateConnection(string value, string certificatePath)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri)
            || uri.Scheme is not ("postgres" or "postgresql")
            || uri.Host != AllowedHost || uri.Port != 5432 || uri.AbsolutePath != "/postgres"
            || uri.Query.Length != 0 || uri.Fragment.Length != 0)
            throw new InvalidOperationException("Only the approved Supabase development session-pooler target is accepted.");

        var userInfo = uri.UserInfo.Split(':', 2);
        if (userInfo.Length != 2 || Uri.UnescapeDataString(userInfo[0]) != AllowedUsername
            || string.IsNullOrWhiteSpace(Uri.UnescapeDataString(userInfo[1])))
            throw new InvalidOperationException("The approved development database operator identity is required.");
        var certificate = Path.GetFullPath(certificatePath);
        if (!File.Exists(certificate))
            throw new InvalidOperationException("The Supabase root certificate file is required.");

        var settings = new NpgsqlConnectionStringBuilder
        {
            Host = uri.Host,
            Port = uri.Port,
            Database = uri.AbsolutePath.TrimStart('/'),
            Username = Uri.UnescapeDataString(userInfo[0]),
            Password = Uri.UnescapeDataString(userInfo[1]),
            SslMode = SslMode.VerifyFull,
            RootCertificate = certificate,
            Timeout = 5,
            CommandTimeout = 30,
            IncludeErrorDetail = false,
            LogParameters = false,
            Pooling = false
        };
        return settings;
    }

    private static async Task VerifyTargetAsync(NpgsqlConnection connection)
    {
        await using var command = new NpgsqlCommand("SELECT current_database(),current_user", connection);
        await using var reader = await command.ExecuteReaderAsync();
        if (!await reader.ReadAsync() || reader.GetString(0) != "postgres" || reader.GetString(1) != "postgres")
            throw new InvalidOperationException("Connected database identity does not match the approved development target.");
    }
}
