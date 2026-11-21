using System.Data;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace AuditPlatform.DefinitionTransfer;

public static class Program
{
    private const string TargetProject = "klljugkujxxmtbmbeewn";
    private const string TargetDirectHost = "db.klljugkujxxmtbmbeewn.supabase.co";
    private const string TargetPoolerHost = "aws-0-ap-south-1.pooler.supabase.com";
    private const string TargetPoolerUser = "postgres.klljugkujxxmtbmbeewn";
    private const string SourceConnectionEnvironmentVariable = "AUDIT_DEFINITION_TRANSFER_SOURCE_CONNECTION";
    private const string TargetConnectionEnvironmentVariable = "AUDIT_DEFINITION_TRANSFER_TARGET_CONNECTION";
    private const string CertificateEnvironmentVariable = "AUDIT_DEFINITION_TRANSFER_ROOT_CERT";
    private static readonly string[] DefinitionTables = ["master_columns", "templates", "template_column_mappings"];
    private static readonly string[] TargetRequiredColumns =
    [
        "master_columns.id", "master_columns.column_name", "master_columns.data_type", "master_columns.category",
        "master_columns.is_active", "master_columns.notes",
        "templates.id", "templates.template_name", "templates.is_active", "templates.is_system",
        "templates.has_subtotals_by_default", "templates.data_purpose", "templates.ledger_columns", "templates.rate_source",
        "template_column_mappings.id", "template_column_mappings.template_id", "template_column_mappings.column_id",
        "template_column_mappings.sort_order", "template_column_mappings.reconciliation_role",
        "template_column_mappings.compare_for_changes", "template_column_mappings.comparison_review_required",
        "audit_log.id", "audit_log.user", "audit_log.action", "audit_log.module", "audit_log.details",
        "audit_log.event_id", "audit_log.operation_id", "audit_log.actor_kind", "audit_log.source_origin"
    ];
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web) { WriteIndented = true };

    public static async Task<int> Main(string[] args)
    {
        try
        {
            var options = ParseArguments(args);
            var sourceProject = options["source-project-ref"];
            var certificatePath = Path.GetFullPath(Environment.GetEnvironmentVariable(CertificateEnvironmentVariable) ?? "");
            if (!File.Exists(certificatePath))
                throw new TransferValidationException("Set the supplied Supabase Root 2021 CA file in the protected process environment.");

            var sourceSettings = BuildSourceConnection(
                Environment.GetEnvironmentVariable(SourceConnectionEnvironmentVariable) ?? "", sourceProject, certificatePath);
            var targetSettings = BuildTargetConnection(
                Environment.GetEnvironmentVariable(TargetConnectionEnvironmentVariable) ?? "", certificatePath);

            await using var source = new NpgsqlConnection(sourceSettings.ConnectionString);
            await using var target = new NpgsqlConnection(targetSettings.ConnectionString);
            await source.OpenAsync();
            await target.OpenAsync();
            await VerifyDatabaseAsync(source);
            await VerifyDatabaseAsync(target);

            await using var sourceSnapshot = await source.BeginTransactionAsync(IsolationLevel.RepeatableRead);
            await SetReadOnlyAsync(source, sourceSnapshot);
            await VerifyReadOnlySourceAsync(source, sourceSnapshot);
            var sourceInventory = await ReadSourceInventoryAsync(source, sourceSnapshot);
            ValidateInventory(sourceInventory);
            await EnsureSourceIsUnscopedAsync(source, sourceSnapshot);

            var targetPreview = await ReadTargetPreflightAsync(target, null);
            var fingerprint = Fingerprint(sourceProject, sourceInventory);
            PrintPreview(sourceProject, sourceInventory, targetPreview, fingerprint);

            if (!options.ContainsKey("apply"))
            {
                await sourceSnapshot.RollbackAsync();
                return 0;
            }

            var expected = options["expected"];
            if (!string.Equals(expected, fingerprint, StringComparison.Ordinal))
                throw new TransferValidationException("Source definitions changed since preview; create and review a fresh preview.");

            var confirmation = $"TRANSFER-DEFINITIONS {sourceProject} TO {TargetProject}";
            if (!string.Equals(options["confirm"], confirmation, StringComparison.Ordinal))
                throw new TransferValidationException("The exact source-to-target confirmation phrase is required.");

            var outcome = await ApplyAsync(target, source, sourceSnapshot, sourceProject, sourceInventory, fingerprint,
                Guid.Parse(options["operation"]));
            await sourceSnapshot.RollbackAsync();
            Console.WriteLine(JsonSerializer.Serialize(outcome, Json));
            return 0;
        }
        catch (Exception error)
        {
            var response = new
            {
                status = "rejected",
                error = error.GetType().Name,
                sqlstate = (error as PostgresException)?.SqlState,
                reason = error is TransferValidationException
                    ? error.Message
                    : "The transfer did not report success. Check the protected target state before trying again; no automatic retry was made."
            };
            Console.WriteLine(JsonSerializer.Serialize(response));
            return error is CommitOutcomeUnknownException ? 2 : 1;
        }
    }

    public static Dictionary<string, string> ParseArguments(string[] args)
    {
        var allowed = new HashSet<string>(StringComparer.Ordinal)
            { "source-project-ref", "expected", "operation", "confirm", "apply" };
        var options = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var index = 0; index < args.Length; index++)
        {
            if (!args[index].StartsWith("--", StringComparison.Ordinal))
                throw new TransferValidationException("Use named operator options.");
            var key = args[index][2..];
            if (!allowed.Contains(key) || options.ContainsKey(key))
                throw new TransferValidationException("Unknown or duplicate option.");
            options[key] = key == "apply"
                ? "true"
                : ++index < args.Length ? args[index] : throw new TransferValidationException("An option value is missing.");
        }
        if (!options.TryGetValue("source-project-ref", out var sourceProject) || !ValidProjectRef(sourceProject)
            || sourceProject == TargetProject)
            throw new TransferValidationException("Supply the exact, different Supabase source project reference.");

        var apply = options.ContainsKey("apply");
        if (apply)
        {
            if (!options.TryGetValue("expected", out var fingerprint) || !ValidFingerprint(fingerprint)
                || !options.TryGetValue("operation", out var operation) || !Guid.TryParse(operation, out var id) || id == Guid.Empty
                || !options.ContainsKey("confirm"))
                throw new TransferValidationException("Apply requires the reviewed lowercase fingerprint, a fresh operation ID, and exact confirmation.");
        }
        else if (options.Keys.Any(key => key is "expected" or "operation" or "confirm"))
            throw new TransferValidationException("Apply options cannot be used during preview.");
        return options;
    }

    private static NpgsqlConnectionStringBuilder BuildSourceConnection(string value, string projectRef, string certificate)
    {
        var supplied = ParseConnection(value, SourceConnectionEnvironmentVariable);
        var directHost = $"db.{projectRef}.supabase.co";
        var direct = string.Equals(supplied.Host, directHost, StringComparison.OrdinalIgnoreCase);
        var sourceHost = supplied.Host ?? "";
        var sourceUsername = supplied.Username ?? "";
        var pooler = sourceHost.EndsWith(".pooler.supabase.com", StringComparison.OrdinalIgnoreCase)
            && sourceUsername.EndsWith("." + projectRef, StringComparison.Ordinal);
        if ((!direct && !pooler) || supplied.Port != 5432 || supplied.Database != "postgres")
            throw new TransferValidationException("Source host, database, port, or project-bound pooler username does not match the supplied source project reference.");
        return VerifiedConnection(supplied, certificate);
    }

    private static NpgsqlConnectionStringBuilder BuildTargetConnection(string value, string certificate)
    {
        var supplied = ParseConnection(value, TargetConnectionEnvironmentVariable);
        var direct = string.Equals(supplied.Host, TargetDirectHost, StringComparison.OrdinalIgnoreCase)
            && supplied.Username == "postgres";
        var pooler = string.Equals(supplied.Host, TargetPoolerHost, StringComparison.OrdinalIgnoreCase)
            && supplied.Username == TargetPoolerUser;
        if ((!direct && !pooler) || supplied.Port != 5432 || supplied.Database != "postgres")
            throw new TransferValidationException("Destination must be the dedicated C# project's approved direct or Mumbai Session Pooler owner connection.");
        return VerifiedConnection(supplied, certificate);
    }

    private static NpgsqlConnectionStringBuilder ParseConnection(string value, string variable)
    {
        if (string.IsNullOrWhiteSpace(value))
            throw new TransferValidationException($"Set {variable} only in the protected process environment.");
        NpgsqlConnectionStringBuilder supplied;
        try { supplied = new NpgsqlConnectionStringBuilder(value); }
        catch (ArgumentException) { throw new TransferValidationException($"{variable} is not a valid PostgreSQL connection string."); }
        if (string.IsNullOrWhiteSpace(supplied.Password) || string.IsNullOrWhiteSpace(supplied.Username))
            throw new TransferValidationException($"{variable} must include the approved login and secret.");
        return supplied;
    }

    private static NpgsqlConnectionStringBuilder VerifiedConnection(NpgsqlConnectionStringBuilder supplied, string certificate) => new()
    {
        Host = supplied.Host,
        Port = supplied.Port,
        Database = supplied.Database,
        Username = supplied.Username,
        Password = supplied.Password,
        SslMode = SslMode.VerifyFull,
        RootCertificate = certificate,
        Pooling = false,
        Timeout = 10,
        CommandTimeout = 30,
        IncludeErrorDetail = false,
        LogParameters = false,
        PersistSecurityInfo = false
    };

    private static async Task VerifyDatabaseAsync(NpgsqlConnection connection)
    {
        // VerifiedConnection configures VerifyFull and the supplied root certificate.
        // A successful OpenAsync therefore already proves the client TLS handshake and
        // certificate/hostname validation. pg_stat_ssl can be false behind Supabase's
        // Session Pooler, so it is not a reliable check of the client-to-pooler TLS link.
        await using var command = new NpgsqlCommand("SELECT current_database()", connection);
        await using var reader = await command.ExecuteReaderAsync();
        if (!await reader.ReadAsync() || reader.GetString(0) != "postgres")
            throw new TransferValidationException("A connection did not reach the expected postgres database.");
    }

    private static async Task SetReadOnlyAsync(NpgsqlConnection connection, NpgsqlTransaction transaction)
    {
        await using var command = new NpgsqlCommand("SET TRANSACTION READ ONLY", connection, transaction);
        await command.ExecuteNonQueryAsync();
    }

    private static async Task VerifyReadOnlySourceAsync(NpgsqlConnection connection, NpgsqlTransaction transaction)
    {
        await using (var role = new NpgsqlCommand("""
            SELECT rolsuper, rolcreatedb, rolcreaterole
            FROM pg_roles WHERE rolname=current_user
            """, connection, transaction))
        await using (var reader = await role.ExecuteReaderAsync())
        {
            if (!await reader.ReadAsync() || reader.GetBoolean(0) || reader.GetBoolean(1) || reader.GetBoolean(2))
                throw new TransferValidationException("The source connection must use a non-owner, non-privileged read-only login.");
        }

        foreach (var table in DefinitionTables)
        {
            await using var command = new NpgsqlCommand("""
                SELECT has_table_privilege(current_user,$1,'SELECT'),
                    has_table_privilege(current_user,$1,'INSERT') OR has_table_privilege(current_user,$1,'UPDATE')
                    OR has_table_privilege(current_user,$1,'DELETE') OR has_table_privilege(current_user,$1,'TRUNCATE')
                """, connection, transaction);
            command.Parameters.AddWithValue("public." + table);
            await using var reader = await command.ExecuteReaderAsync();
            if (!await reader.ReadAsync() || !reader.GetBoolean(0) || reader.GetBoolean(1))
                throw new TransferValidationException("The source login needs SELECT-only access to the three reusable-definition tables.");
        }
    }

    private static async Task<Snapshot> ReadSourceInventoryAsync(NpgsqlConnection connection, NpgsqlTransaction transaction)
    {
        await EnsureColumnsAsync(connection, transaction, SourceRequiredColumns(), "source");
        var columns = new List<ColumnDefinition>();
        await using (var command = new NpgsqlCommand("""
            SELECT id,column_name,data_type,category,notes,is_active
            FROM public.master_columns ORDER BY id
            """, connection, transaction))
        await using (var reader = await command.ExecuteReaderAsync())
        {
            while (await reader.ReadAsync())
                columns.Add(new ColumnDefinition(reader.GetInt32(0), reader.GetString(1), reader.GetString(2),
                    reader.IsDBNull(3) ? null : reader.GetString(3), reader.IsDBNull(4) ? null : reader.GetString(4),
                    reader.IsDBNull(5) ? null : reader.GetBoolean(5)));
        }

        var templates = new List<TemplateDefinition>();
        await using (var command = new NpgsqlCommand("""
            SELECT id,template_name,data_purpose,ledger_columns::text,is_active,is_system,
                has_subtotals_by_default,rate_source
            FROM public.templates ORDER BY id
            """, connection, transaction))
        await using (var reader = await command.ExecuteReaderAsync())
        {
            while (await reader.ReadAsync())
                templates.Add(new TemplateDefinition(reader.GetInt32(0), reader.GetString(1), reader.GetString(2),
                    reader.GetString(3), reader.IsDBNull(4) ? null : reader.GetBoolean(4),
                    reader.IsDBNull(5) ? null : reader.GetBoolean(5), reader.IsDBNull(6) ? null : reader.GetBoolean(6),
                    reader.IsDBNull(7) ? null : reader.GetString(7)));
        }

        var mappings = new List<MappingDefinition>();
        await using (var command = new NpgsqlCommand("""
            SELECT id,template_id,column_id,sort_order,reconciliation_role,compare_for_changes,comparison_review_required
            FROM public.template_column_mappings ORDER BY template_id,sort_order,id
            """, connection, transaction))
        await using (var reader = await command.ExecuteReaderAsync())
        {
            while (await reader.ReadAsync())
                mappings.Add(new MappingDefinition(reader.GetInt32(0), reader.GetInt32(1), reader.GetInt32(2),
                    reader.GetInt32(3), reader.IsDBNull(4) ? null : reader.GetString(4),
                    reader.IsDBNull(5) ? null : reader.GetBoolean(5), reader.GetBoolean(6)));
        }
        return new Snapshot(columns.ToArray(), templates.ToArray(), mappings.ToArray());
    }

    private static async Task EnsureSourceIsUnscopedAsync(NpgsqlConnection connection, NpgsqlTransaction transaction)
    {
        var columns = await ReadColumnNamesAsync(connection, transaction, DefinitionTables);
        var hasTenant = columns.Contains("template_column_mappings.tenant_id", StringComparer.Ordinal);
        var hasStorageKey = columns.Contains("template_column_mappings.storage_key", StringComparer.Ordinal);
        if (hasTenant != hasStorageKey)
            throw new TransferValidationException("Source template-binding ownership columns are only partially present.");
        if (hasTenant)
        {
            await using var command = new NpgsqlCommand("""
                SELECT EXISTS(SELECT 1 FROM public.template_column_mappings
                    WHERE tenant_id IS NOT NULL OR storage_key IS NOT NULL)
                """, connection, transaction);
            if (Convert.ToBoolean(await command.ExecuteScalarAsync()))
                throw new TransferValidationException("Source bindings already carry tenant ownership or storage keys; review the source before transfer.");
        }

        foreach (var table in new[] { "platform_tenant_columns", "platform_tenant_templates" })
        {
            if (!await TableExistsAsync(connection, transaction, table)) continue;
            await using var command = new NpgsqlCommand($"SELECT EXISTS(SELECT 1 FROM public.{QuoteIdentifier(table)} LIMIT 1)", connection, transaction);
            if (Convert.ToBoolean(await command.ExecuteScalarAsync()))
                throw new TransferValidationException("Source already has tenant-scoped Columns/Templates; only an unscoped reusable catalog is accepted.");
        }
    }

    private static async Task<TargetInventory> ReadTargetPreflightAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction)
    {
        await EnsureColumnsAsync(connection, transaction, TargetRequiredColumns, "destination");
        var counts = new Dictionary<string, long>(StringComparer.Ordinal);
        foreach (var table in DefinitionTables)
        {
            await using var command = new NpgsqlCommand($"SELECT count(*) FROM public.{QuoteIdentifier(table)}", connection, transaction);
            counts[table] = Convert.ToInt64(await command.ExecuteScalarAsync());
        }
        if (counts.Values.Any(count => count != 0))
            throw new TransferValidationException("Destination legacy definition tables must be empty; no existing definition will be overwritten.");

        foreach (var table in new[] { "platform_tenant_columns", "platform_tenant_templates", "platform_tenant_saved_mapping_sets" })
        {
            if (!await TableExistsAsync(connection, transaction, table)) continue;
            await using var command = new NpgsqlCommand($"SELECT EXISTS(SELECT 1 FROM public.{QuoteIdentifier(table)} LIMIT 1)", connection, transaction);
            if (Convert.ToBoolean(await command.ExecuteScalarAsync()))
                throw new TransferValidationException("Destination already contains native Columns/Templates; review before transfer.");
        }
        return new TargetInventory(counts);
    }

    private static async Task EnsureColumnsAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction,
        IEnumerable<string> required, string side)
    {
        var names = required.ToArray();
        var found = await ReadColumnNamesAsync(connection, transaction, names.Select(name => name[..name.IndexOf('.')]).Distinct().ToArray());
        foreach (var column in names)
            if (!found.Contains(column, StringComparer.Ordinal))
                throw new TransferValidationException($"The {side} schema is missing {column}; review the empty-schema bootstrap/migrations first.");
    }

    private static string[] SourceRequiredColumns() =>
    [
        "master_columns.id", "master_columns.column_name", "master_columns.data_type", "master_columns.category",
        "master_columns.notes", "master_columns.is_active",
        "templates.id", "templates.template_name", "templates.data_purpose", "templates.ledger_columns",
        "templates.is_active", "templates.is_system", "templates.has_subtotals_by_default", "templates.rate_source",
        "template_column_mappings.id", "template_column_mappings.template_id", "template_column_mappings.column_id",
        "template_column_mappings.sort_order", "template_column_mappings.reconciliation_role",
        "template_column_mappings.compare_for_changes", "template_column_mappings.comparison_review_required"
    ];

    private static async Task<string[]> ReadColumnNamesAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction,
        IEnumerable<string> tables)
    {
        await using var command = new NpgsqlCommand("""
            SELECT table_name,column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name=ANY($1)
            """, connection, transaction);
        command.Parameters.AddWithValue(tables.Distinct(StringComparer.Ordinal).ToArray());
        var found = new List<string>();
        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync()) found.Add(reader.GetString(0) + "." + reader.GetString(1));
        return found.ToArray();
    }

    private static async Task<bool> TableExistsAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction, string table)
    {
        await using var command = new NpgsqlCommand("""
            SELECT EXISTS(SELECT 1 FROM information_schema.tables
                WHERE table_schema='public' AND table_name=$1 AND table_type='BASE TABLE')
            """, connection, transaction);
        command.Parameters.AddWithValue(table);
        return Convert.ToBoolean(await command.ExecuteScalarAsync());
    }

    private static void ValidateInventory(Snapshot inventory)
    {
        var columns = inventory.Columns;
        var templates = inventory.Templates;
        var mappings = inventory.Mappings;
        if (columns.Any(column => column.Id <= 0 || string.IsNullOrWhiteSpace(column.Name) || column.Name.Contains('\0')
                || column.Name.Length > 200 || column.Category is null || column.Category.Contains('\0')
                || column.Type is not ("Text" or "Numeric" or "Date") || column.Active is null)
            || columns.Select(column => column.Id).Distinct().Count() != columns.Length
            || columns.GroupBy(column => column.Name, StringComparer.OrdinalIgnoreCase).Any(group => group.Count() > 1))
            throw new TransferValidationException("Column IDs, names, categories, types or active states need review.");

        if (templates.Any(template => template.Id <= 0 || string.IsNullOrWhiteSpace(template.Name)
                || template.Name.Length > 200 || template.Name.Contains('\0') || template.Purpose is not ("standard" or "account_ledger")
                || template.Active is null || template.IsSystem is null || template.Subtotals is null
                || template.LedgerJson.Contains('\0') || template.RateSource is not (null or "auto" or "imported" or "derived"))
            || templates.Select(template => template.Id).Distinct().Count() != templates.Length
            || templates.GroupBy(template => template.Name, StringComparer.OrdinalIgnoreCase).Any(group => group.Count() > 1))
            throw new TransferValidationException("Template IDs, names, purposes, flags or rate settings need review.");

        if (columns.Length == 0 || templates.Length == 0)
            throw new TransferValidationException("The source contains no reusable Columns/Templates to transfer.");
        if (mappings.Select(mapping => mapping.Id).Any(id => id <= 0)
            || mappings.Select(mapping => mapping.Id).Distinct().Count() != mappings.Length
            || mappings.Any(mapping => mapping.TemplateId <= 0 || mapping.ColumnId <= 0
                || mapping.Role is not (null or "document_id" or "document_date" or "footing"))
            || mappings.GroupBy(mapping => (mapping.TemplateId, mapping.ColumnId)).Any(group => group.Count() > 1)
            || mappings.GroupBy(mapping => (mapping.TemplateId, mapping.Order)).Any(group => group.Count() > 1))
            throw new TransferValidationException("Template bindings contain duplicate IDs, references or ordering that need review.");

        var columnsById = columns.ToDictionary(column => column.Id);
        var templatesById = templates.ToDictionary(template => template.Id);
        if (mappings.Any(mapping => !templatesById.ContainsKey(mapping.TemplateId) || !columnsById.ContainsKey(mapping.ColumnId)))
            throw new TransferValidationException("A template binding references a missing column or template.");

        foreach (var template in templates)
        {
            var ledger = ReadLedger(template.LedgerJson);
            var fields = mappings.Where(mapping => mapping.TemplateId == template.Id).OrderBy(mapping => mapping.Order)
                .Select(mapping =>
                {
                    var column = columnsById[mapping.ColumnId];
                    if (mapping.Role == "document_id" && column.Type != "Text"
                        || mapping.Role == "document_date" && column.Type != "Date"
                        || mapping.Role == "footing" && column.Type != "Numeric")
                        throw new TransferValidationException("A template role is incompatible with its mapped column type.");
                    return (Mapping: mapping, Column: column);
                }).ToArray();
            if (template.Active == true && (fields.Length == 0 || fields.Any(field => field.Column.Active != true)))
                throw new TransferValidationException("An active template must contain active mapped columns.");
            if (fields.Count(field => field.Mapping.Role == "document_id") > 1
                || fields.Count(field => field.Mapping.Role == "document_date") > 1)
                throw new TransferValidationException("A template contains repeated Document ID or Document Date roles.");
            ValidateLedger(template, ledger, fields);
        }
    }

    private static Dictionary<string, int> ReadLedger(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
                throw new TransferValidationException("Template ledger-column settings must be a JSON object.");
            var ledger = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var property in document.RootElement.EnumerateObject())
            {
                if (!ledger.TryAdd(property.Name, property.Value.TryGetInt32(out var id) ? id : 0) || id <= 0)
                    throw new TransferValidationException("Template ledger-column settings contain duplicate or invalid column IDs.");
            }
            return ledger;
        }
        catch (JsonException) { throw new TransferValidationException("A template contains invalid ledger-column JSON."); }
    }

    private static void ValidateLedger(TemplateDefinition template, Dictionary<string, int> ledger,
        (MappingDefinition Mapping, ColumnDefinition Column)[] fields)
    {
        if (template.Purpose == "standard")
        {
            if (ledger.Count != 0) throw new TransferValidationException("A standard template has account-ledger column settings.");
            return;
        }

        var roleNames = new[] { "voucher", "date", "description", "debit", "credit", "balance" };
        if (!ledger.Keys.Order(StringComparer.Ordinal).SequenceEqual(roleNames.Order(StringComparer.Ordinal), StringComparer.Ordinal)
            || ledger.Values.Distinct().Count() != roleNames.Length || fields.Length == 0
            || fields[0].Column.Id != ledger["voucher"] || ledger.Values.Except(fields.Select(field => field.Column.Id)).Any())
            throw new TransferValidationException("Account Ledger templates must map six distinct, selected ledger columns in the required order.");
        foreach (var field in fields)
        {
            var type = field.Column.Type;
            if (field.Mapping.Role is "document_id" && type != "Text"
                || field.Mapping.Role is "document_date" && type != "Date"
                || field.Mapping.Role is "footing" && type != "Numeric")
                throw new TransferValidationException("A template role is incompatible with its mapped column type.");
        }
        var documentId = fields.Where(field => field.Mapping.Role == "document_id")
            .Select(field => (int?)field.Column.Id).SingleOrDefault();
        if (documentId is int documentIdValue && documentIdValue != ledger["voucher"])
            throw new TransferValidationException("The Account Ledger Document ID must match its Voucher column.");

        foreach (var (role, id) in ledger)
        {
            var column = fields.Single(field => field.Column.Id == id).Column;
            var compatible = role switch
            {
                "voucher" or "description" => column.Type == "Text",
                "date" => column.Type == "Date",
                "balance" => column.Type is "Text" or "Numeric",
                _ => column.Type == "Numeric"
            };
            if (!compatible) throw new TransferValidationException("An Account Ledger role is incompatible with its column type.");
        }
    }

    private static async Task<TransferReceipt> ApplyAsync(NpgsqlConnection target, NpgsqlConnection source,
        NpgsqlTransaction sourceSnapshot, string sourceProject, Snapshot inventory, string fingerprint, Guid operation)
    {
        await using var transaction = await target.BeginTransactionAsync(IsolationLevel.Serializable);
        try
        {
            await using (var advisory = new NpgsqlCommand("SELECT pg_advisory_xact_lock(699006231)", target, transaction))
                await advisory.ExecuteNonQueryAsync();

            var prior = await ReadPriorReceiptAsync(target, transaction, operation);
            if (prior is not null)
            {
                using var document = JsonDocument.Parse(prior);
                var root = document.RootElement;
                if (root.GetProperty("fingerprint").GetString() != fingerprint
                    || root.GetProperty("sourceProjectRef").GetString() != sourceProject
                    || root.GetProperty("targetProjectRef").GetString() != TargetProject)
                    throw new TransferValidationException("That operation ID already belongs to a different transfer.");
                await transaction.RollbackAsync();
                return new("already_completed", operation, sourceProject, TargetProject, fingerprint,
                    GetCounts(inventory));
            }

            await ReadTargetPreflightAsync(target, transaction);
            var fresh = await ReadSourceInventoryAsync(source, sourceSnapshot);
            ValidateInventory(fresh);
            await EnsureSourceIsUnscopedAsync(source, sourceSnapshot);
            if (!string.Equals(Fingerprint(sourceProject, fresh), fingerprint, StringComparison.Ordinal))
                throw new TransferValidationException("Source definitions changed during transfer; no write was committed.");

            foreach (var column in inventory.Columns)
            {
                await using var command = new NpgsqlCommand("""
                    INSERT INTO public.master_columns(id,column_name,data_type,category,is_active,notes)
                    OVERRIDING SYSTEM VALUE VALUES($1,$2,$3,$4,$5,$6)
                    """, target, transaction);
                command.Parameters.AddWithValue(column.Id);
                command.Parameters.AddWithValue(column.Name);
                command.Parameters.AddWithValue(column.Type);
                command.Parameters.AddWithValue(column.Category!);
                command.Parameters.AddWithValue(column.Active!.Value);
                command.Parameters.AddWithValue((object?)column.Notes ?? DBNull.Value);
                await command.ExecuteNonQueryAsync();
            }

            foreach (var template in inventory.Templates)
            {
                await using var command = new NpgsqlCommand("""
                    INSERT INTO public.templates(id,template_name,created_by,created_date,is_active,is_system,
                        has_subtotals_by_default,data_purpose,ledger_columns,rate_source)
                    OVERRIDING SYSTEM VALUE VALUES($1,$2,'Definition Transfer',now(),$3,$4,$5,$6,$7,$8)
                    """, target, transaction);
                command.Parameters.AddWithValue(template.Id);
                command.Parameters.AddWithValue(template.Name);
                command.Parameters.AddWithValue(template.Active!.Value);
                command.Parameters.AddWithValue(template.IsSystem!.Value);
                command.Parameters.AddWithValue(template.Subtotals!.Value);
                command.Parameters.AddWithValue(template.Purpose);
                command.Parameters.AddWithValue(template.LedgerJson);
                command.Parameters.AddWithValue((object?)template.RateSource ?? DBNull.Value);
                await command.ExecuteNonQueryAsync();
            }

            foreach (var mapping in inventory.Mappings)
            {
                await using var command = new NpgsqlCommand("""
                    INSERT INTO public.template_column_mappings(id,template_id,column_id,sort_order,
                        reconciliation_role,compare_for_changes,comparison_review_required)
                    OVERRIDING SYSTEM VALUE VALUES($1,$2,$3,$4,$5,$6,$7)
                    """, target, transaction);
                command.Parameters.AddWithValue(mapping.Id);
                command.Parameters.AddWithValue(mapping.TemplateId);
                command.Parameters.AddWithValue(mapping.ColumnId);
                command.Parameters.AddWithValue(mapping.Order);
                command.Parameters.AddWithValue((object?)mapping.Role ?? DBNull.Value);
                command.Parameters.AddWithValue((object?)mapping.Compare ?? DBNull.Value);
                command.Parameters.AddWithValue(mapping.Review);
                await command.ExecuteNonQueryAsync();
            }

            await ResetIdentitySequenceAsync(target, transaction, "master_columns");
            await ResetIdentitySequenceAsync(target, transaction, "templates");
            await ResetIdentitySequenceAsync(target, transaction, "template_column_mappings");

            var receipt = new TransferReceipt("completed", operation, sourceProject, TargetProject, fingerprint, GetCounts(inventory));
            await using (var audit = new NpgsqlCommand("""
                INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,actor_kind,source_origin)
                VALUES('System','DefinitionTransfer','Migration',$1,$2,$3,'system','supabase_definition_transfer')
                """, target, transaction))
            {
                audit.Parameters.AddWithValue(JsonSerializer.Serialize(receipt));
                audit.Parameters.AddWithValue(Guid.NewGuid());
                audit.Parameters.AddWithValue(operation);
                await audit.ExecuteNonQueryAsync();
            }

            try { await transaction.CommitAsync(); }
            catch
            {
                Console.WriteLine("Commit outcome unknown. Do not rerun; inspect the destination audit receipt and definitions first.");
                throw new CommitOutcomeUnknownException();
            }
            return receipt;
        }
        catch (CommitOutcomeUnknownException) { throw; }
        catch
        {
            await transaction.RollbackAsync();
            throw;
        }
    }

    private static async Task<string?> ReadPriorReceiptAsync(NpgsqlConnection connection, NpgsqlTransaction transaction, Guid operation)
    {
        await using var command = new NpgsqlCommand("""
            SELECT action,module,details FROM public.audit_log WHERE operation_id=$1
            """, connection, transaction);
        command.Parameters.AddWithValue(operation);
        await using var reader = await command.ExecuteReaderAsync();
        if (!await reader.ReadAsync()) return null;
        var action = reader.GetString(0);
        var module = reader.GetString(1);
        var details = reader.IsDBNull(2) ? null : reader.GetString(2);
        if (await reader.ReadAsync() || action != "DefinitionTransfer" || module != "Migration" || details is null)
            throw new TransferValidationException("Operation ID already exists in the audit log; choose a fresh unique ID.");
        return details;
    }

    private static async Task ResetIdentitySequenceAsync(NpgsqlConnection connection, NpgsqlTransaction transaction, string table)
    {
        await using var maximum = new NpgsqlCommand($"SELECT max(id) FROM public.{table}", connection, transaction);
        var maxId = await maximum.ExecuteScalarAsync();
        if (maxId is null or DBNull) return;
        await using var set = new NpgsqlCommand("SELECT setval(pg_get_serial_sequence($1,$2),$3,TRUE)", connection, transaction);
        set.Parameters.AddWithValue("public." + table);
        set.Parameters.AddWithValue("id");
        set.Parameters.AddWithValue(Convert.ToInt64(maxId));
        await set.ExecuteScalarAsync();
    }

    private static string Fingerprint(string sourceProject, Snapshot inventory) =>
        Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(sourceProject + "\n" + TargetProject + "\n"
            + JsonSerializer.Serialize(inventory))));

    private static object GetCounts(Snapshot inventory) => new
    {
        columns = inventory.Columns.Length,
        templates = inventory.Templates.Length,
        templateColumnBindings = inventory.Mappings.Length
    };

    private static void PrintPreview(string sourceProject, Snapshot inventory, TargetInventory target, string fingerprint)
    {
        var preview = new
        {
            status = "preview",
            sourceProjectRef = sourceProject,
            targetProjectRef = TargetProject,
            fingerprint,
            counts = GetCounts(inventory),
            destinationDefinitionRows = target.Counts,
            definitions = inventory
        };
        Console.WriteLine(JsonSerializer.Serialize(preview, Json));
    }

    private static bool ValidProjectRef(string value) => value.Length == 20
        && value.All(character => character is >= 'a' and <= 'z' or >= '0' and <= '9');

    private static bool ValidFingerprint(string value) => value.Length == 64
        && value.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f');

    private static string QuoteIdentifier(string value) => "\"" + value.Replace("\"", "\"\"", StringComparison.Ordinal) + "\"";

    private sealed record ColumnDefinition(int Id, string Name, string Type, string? Category, string? Notes, bool? Active);
    private sealed record TemplateDefinition(int Id, string Name, string Purpose, string LedgerJson, bool? Active,
        bool? IsSystem, bool? Subtotals, string? RateSource);
    private sealed record MappingDefinition(int Id, int TemplateId, int ColumnId, int Order, string? Role, bool? Compare, bool Review);
    private sealed record Snapshot(ColumnDefinition[] Columns, TemplateDefinition[] Templates, MappingDefinition[] Mappings);
    private sealed record TargetInventory(Dictionary<string, long> Counts);
    private sealed record TransferReceipt(string Status, Guid Operation, string SourceProjectRef, string TargetProjectRef,
        string Fingerprint, object Counts);
    private sealed class TransferValidationException(string message) : Exception(message) { }
    private sealed class CommitOutcomeUnknownException : Exception { }
}
