using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Templates;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.AuditRules;

public sealed class AuditRuleService(PostgresTransactions transactions, PostgresAuditWriter audit,
    IOptions<AuditRulesOptions> options)
{
    private const int SnapshotSchemaVersion = AuditRuleKinds.SnapshotSchemaVersion;
    private const int EvaluatorVersion = AuditRuleKinds.EvaluatorVersion;
    private const int MaximumChanges = 200;

    public Task<AuditRuleResult> ListAsync(VerifiedSession? session, string kind, bool includeInactive,
        int? limit, int offset, CancellationToken ct = default) => Run(async token =>
    {
        if (!AuditRuleKinds.Valid(kind) || limit is < 1 || limit > options.Value.MaximumPageSize || offset < 0)
            return Invalid("invalid_audit_rule_query");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, false, inner);
            if (tenant is null) return Forbidden();
            await EnsureStateAsync(db, tenant.Value, inner);
            var entities = new List<AuditRuleEntity>();
            int total;
            await using (var count = db.CreateCommand("SELECT count(*) FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind=$2 AND ($3 OR is_active IS TRUE)"))
            {
                count.Parameters.AddWithValue(tenant.Value); count.Parameters.AddWithValue(kind); count.Parameters.AddWithValue(includeInactive);
                total = Convert.ToInt32(await count.ExecuteScalarAsync(inner));
            }
            await using (var command = db.CreateCommand("""
                SELECT id,definition::text,is_active,revision,updated_at
                FROM public.platform_audit_rule_entities
                WHERE tenant_id=$1 AND entity_kind=$2 AND ($3 OR is_active IS TRUE)
                ORDER BY CASE WHEN $2 IN ('assertions','voucher-checks')
                    THEN CASE WHEN definition->>'sort_order' ~ '^[0-9]{1,9}$'
                        THEN (definition->>'sort_order')::integer ELSE id END END,
                    CASE WHEN $2 IN ('assertions','voucher-checks') THEN NULL ELSE definition->>'name' END,id
                LIMIT $4 OFFSET $5
                """))
            {
                command.Parameters.AddWithValue(tenant.Value); command.Parameters.AddWithValue(kind);
                command.Parameters.AddWithValue(includeInactive);
                command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Integer, Value = (object?)limit ?? DBNull.Value });
                command.Parameters.AddWithValue(offset);
                await using var reader = await command.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner))
                    entities.Add(new(reader.GetInt32(0), kind, JsonDocument.Parse(reader.GetString(1)).RootElement.Clone(),
                        reader.GetBoolean(2), reader.GetInt64(3), reader.GetDateTime(4)));
            }
            var rows = new List<JsonElement>(entities.Count);
            foreach (var entity in entities) rows.Add(await EntityViewAsync(db, tenant.Value, entity, listView: true, inner));
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            return Success(rows, version, limit is null ? null : total);
        }, token);
    }, ct);

    public Task<AuditRuleResult> SignatureSpecimensAsync(VerifiedSession? session, int clientId,
        int? documentId, bool includeInactive, CancellationToken ct = default) => Run(async token =>
    {
        if (clientId <= 0 || documentId is <= 0) return Invalid("invalid_signature_specimen_query");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, false, inner);
            if (tenant is null) return Forbidden();
            await EnsureStateAsync(db, tenant.Value, inner);
            if (!await SignatureSpecimenClientExistsAsync(db, tenant.Value, clientId, inner)) return NotFound();
            var rows = new List<object>();
            await using (var command = db.CreateCommand("""
                SELECT s.id,s.client_id,s.document_id,d.definition->>'name',s.role_name,
                    s.expected_signatory_name,s.authority,s.is_active,s.created_at,s.updated_at
                FROM public.platform_audit_rule_signature_specimens s
                JOIN public.platform_audit_rule_entities d
                  ON d.tenant_id=s.tenant_id AND d.entity_kind='documents' AND d.id=s.document_id
                WHERE s.tenant_id=$1 AND s.client_id=$2 AND ($3::integer IS NULL OR s.document_id=$3)
                  AND ($4 OR s.is_active IS TRUE)
                ORDER BY d.definition->>'name',s.role_name,s.id
                """))
            {
                command.Parameters.AddWithValue(tenant.Value); command.Parameters.AddWithValue(clientId);
                command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Integer, Value = (object?)documentId ?? DBNull.Value });
                command.Parameters.AddWithValue(includeInactive);
                await using var reader = await command.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner)) rows.Add(ReadSignatureSpecimen(reader));
            }
            return Success(rows, await CurrentVersionAsync(db, tenant.Value, inner));
        }, token);
    }, ct);

    public Task<AuditRuleResult> SignatureSpecimenAsync(VerifiedSession? session, int id,
        CancellationToken ct = default) => Run(async token =>
    {
        if (id <= 0) return Invalid("invalid_signature_specimen_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, false, inner);
            if (tenant is null) return Forbidden();
            await EnsureStateAsync(db, tenant.Value, inner);
            await using var command = db.CreateCommand("""
                SELECT s.id,s.client_id,s.document_id,d.definition->>'name',s.role_name,
                    s.expected_signatory_name,s.authority,s.is_active,s.created_at,s.updated_at
                FROM public.platform_audit_rule_signature_specimens s
                JOIN public.platform_audit_rule_entities d
                  ON d.tenant_id=s.tenant_id AND d.entity_kind='documents' AND d.id=s.document_id
                WHERE s.tenant_id=$1 AND s.id=$2
                """);
            command.Parameters.AddWithValue(tenant.Value); command.Parameters.AddWithValue(id);
            await using var reader = await command.ExecuteReaderAsync(inner);
            if (!await reader.ReadAsync(inner)) return NotFound();
            var value = ReadSignatureSpecimen(reader);
            await reader.DisposeAsync();
            return Success(value, await CurrentVersionAsync(db, tenant.Value, inner));
        }, token);
    }, ct);

    public Task<AuditRuleResult> SaveSignatureSpecimenAsync(VerifiedSession? session, int? id,
        SignatureSpecimenInput input, long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (id is <= 0 || expectedVersion is <= 0 || input.ClientId <= 0 || input.DocumentId <= 0
            || string.IsNullOrEmpty(input.RoleName) || input.RoleName.Length > 200
            || string.IsNullOrEmpty(input.ExpectedSignatoryName) || input.ExpectedSignatoryName.Length > 500
            || input.Authority?.Length > 500) return Invalid("invalid_signature_specimen_definition");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner);
            await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner))
                return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion.Value != version)
                return new("conflict", Code: "audit_rules_version_changed", Version: version);
            if (id is not null)
            {
                await using var exists = db.CreateCommand("SELECT 1 FROM public.platform_audit_rule_signature_specimens WHERE tenant_id=$1 AND id=$2 FOR UPDATE");
                exists.Parameters.AddWithValue(tenant.Value); exists.Parameters.AddWithValue(id.Value);
                if (await exists.ExecuteScalarAsync(inner) is null) return NotFound();
            }
            if (!await ValidateSignatureSpecimenReferencesAsync(db, tenant.Value, input, inner))
                return Invalid("signature_specimen_reference_invalid");
            if (await SignatureSpecimenDuplicateAsync(db, tenant.Value, input, id, inner))
                return new("conflict", Code: "signature_specimen_duplicate");

            int specimenId;
            string action;
            if (id is null)
            {
                await using var insert = db.CreateCommand("""
                    INSERT INTO public.platform_audit_rule_signature_specimens
                        (tenant_id,client_id,document_id,role_name,expected_signatory_name,authority,created_by,updated_by)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$7) RETURNING id
                    """);
                AddSignatureSpecimenInput(insert, tenant.Value, input);
                insert.Parameters.AddWithValue(session!.UserId);
                specimenId = (int)(await insert.ExecuteScalarAsync(inner))!; action = "created";
            }
            else
            {
                specimenId = id.Value; action = "updated";
                await using var update = db.CreateCommand("""
                    UPDATE public.platform_audit_rule_signature_specimens
                    SET client_id=$1,document_id=$2,role_name=$3,expected_signatory_name=$4,authority=$5,
                        revision=revision+1,updated_by=$6,updated_at=now()
                    WHERE tenant_id=$7 AND id=$8
                    """);
                update.Parameters.AddWithValue(input.ClientId); update.Parameters.AddWithValue(input.DocumentId);
                update.Parameters.AddWithValue(input.RoleName); update.Parameters.AddWithValue(input.ExpectedSignatoryName);
                update.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)input.Authority ?? DBNull.Value);
                update.Parameters.AddWithValue(session!.UserId); update.Parameters.AddWithValue(tenant.Value); update.Parameters.AddWithValue(specimenId);
                await update.ExecuteNonQueryAsync(inner);
            }
            long nextVersion = await PublishChangeAsync(db, tenant.Value, "signature-specimens", specimenId,
                action, input.ExpectedSignatoryName, session!.UserId, inner);
            await AuditAsync(db, session, tenant.Value, action, "signature-specimens", specimenId,
                input.ExpectedSignatoryName, nextVersion, inner);
            return Success(new { message = $"Signature specimen {action} successfully", specimen_id = specimenId,
                config_version = nextVersion }, version: nextVersion, id: specimenId);
        }, token);
    }, ct);

    public Task<AuditRuleResult> SetSignatureSpecimenActiveAsync(VerifiedSession? session, int id,
        bool active, long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (id <= 0 || expectedVersion is <= 0) return Invalid("invalid_signature_specimen_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner);
            await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner))
                return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion.Value != version)
                return new("conflict", Code: "audit_rules_version_changed", Version: version);
            string? name;
            await using (var update = db.CreateCommand("""
                UPDATE public.platform_audit_rule_signature_specimens
                SET is_active=$1,revision=revision+1,updated_by=$2,updated_at=now()
                WHERE tenant_id=$3 AND id=$4 AND is_active IS DISTINCT FROM $1
                RETURNING expected_signatory_name
                """))
            {
                update.Parameters.AddWithValue(active); update.Parameters.AddWithValue(session!.UserId);
                update.Parameters.AddWithValue(tenant.Value); update.Parameters.AddWithValue(id);
                name = await update.ExecuteScalarAsync(inner) as string;
            }
            if (name is null) return NotFound();
            string action = active ? "restored" : "deleted";
            long nextVersion = await PublishChangeAsync(db, tenant.Value, "signature-specimens", id,
                action, name, session!.UserId, inner);
            await AuditAsync(db, session, tenant.Value, action, "signature-specimens", id, name, nextVersion, inner);
            return Success(new { message = $"Signature specimen {action} successfully", specimen_id = id,
                is_active = active, config_version = nextVersion }, version: nextVersion, id: id);
        }, token);
    }, ct);

    internal static async Task<bool> HasAdoptionProvenanceAsync(PostgresTransactionContext db, int tenantId,
        int specimenId, CancellationToken ct)
    {
        await using (var exists = db.CreateCommand("SELECT to_regclass('public.platform_audit_rule_specimen_adoption_map') IS NOT NULL"))
            if (await exists.ExecuteScalarAsync(ct) is not true) return false;
        await using var command = db.CreateCommand("""
            SELECT EXISTS(SELECT 1 FROM public.platform_audit_rule_specimen_adoption_map
                WHERE tenant_id=$1 AND specimen_id=$2)
            """);
        command.Parameters.AddWithValue(tenantId); command.Parameters.AddWithValue(specimenId);
        return await command.ExecuteScalarAsync(ct) is true;
    }

    public Task<AuditRuleResult> HardDeleteSignatureSpecimenAsync(VerifiedSession? session, int id,
        long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (id <= 0 || expectedVersion is <= 0) return Invalid("invalid_signature_specimen_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner);
            await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner))
                return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion.Value != version)
                return new("conflict", Code: "audit_rules_version_changed", Version: version);
            if (await HasAdoptionProvenanceAsync(db, tenant.Value, id, inner))
                return new("conflict", Code: "signature_specimen_retained_as_migration_provenance", Version: version);
            string? name;
            await using (var delete = db.CreateCommand("""
                DELETE FROM public.platform_audit_rule_signature_specimens
                WHERE tenant_id=$1 AND id=$2 RETURNING expected_signatory_name
                """))
            {
                delete.Parameters.AddWithValue(tenant.Value); delete.Parameters.AddWithValue(id);
                name = await delete.ExecuteScalarAsync(inner) as string;
            }
            if (name is null) return NotFound();
            long nextVersion = await PublishChangeAsync(db, tenant.Value, "signature-specimens", id,
                "hard_deleted", name, session!.UserId, inner);
            await AuditAsync(db, session, tenant.Value, "hard_deleted", "signature-specimens", id, name, nextVersion, inner);
            return Success(new { message = "Signature specimen permanently deleted", specimen_id = id,
                config_version = nextVersion }, version: nextVersion, id: id);
        }, token);
    }, ct);

    public Task<AuditRuleResult> GetAsync(VerifiedSession? session, string kind, int id,
        CancellationToken ct = default) => Run(async token =>
    {
        if (!AuditRuleKinds.Valid(kind) || id <= 0) return Invalid("invalid_audit_rule_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, false, inner);
            if (tenant is null) return Forbidden();
            await EnsureStateAsync(db, tenant.Value, inner);
            AuditRuleEntity entity;
            await using (var command = db.CreateCommand("""
                SELECT id,definition::text,is_active,revision,updated_at
                FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind=$2 AND id=$3
                """))
            {
                command.Parameters.AddWithValue(tenant.Value); command.Parameters.AddWithValue(kind); command.Parameters.AddWithValue(id);
                await using var reader = await command.ExecuteReaderAsync(inner);
                if (!await reader.ReadAsync(inner)) return NotFound();
                entity = new AuditRuleEntity(reader.GetInt32(0), kind, JsonDocument.Parse(reader.GetString(1)).RootElement.Clone(),
                    reader.GetBoolean(2), reader.GetInt64(3), reader.GetDateTime(4));
            }
            var result = await EntityViewAsync(db, tenant.Value, entity, listView: false, inner);
            return Success(result, await CurrentVersionAsync(db, tenant.Value, inner));
        }, token);
    }, ct);

    public Task<AuditRuleResult> SaveAsync(VerifiedSession? session, string kind, int? id,
        JsonElement request, long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        // Validate an update as a patch first. The stored definition is merged
        // under the row lock below so omitted fields are preserved.
        var cleaned = ValidateDefinition(kind, request, requireComplete: id is null);
        if (cleaned is null) return Invalid("invalid_audit_rule_definition");
        if (id is <= 0 || expectedVersion is <= 0) return Invalid("invalid_audit_rule_version");
        string definition = cleaned.ToJsonString(JsonOptions);
        if (Encoding.UTF8.GetByteCount(definition) > options.Value.MaximumDefinitionBytes)
            return new("too_large", Code: "audit_rule_definition_too_large");

        try
        {
            return await transactions.ExecuteAsync(async (db, inner) =>
            {
                var tenant = await AuthorizeAsync(db, session, true, inner);
                if (tenant is null) return Forbidden();
                await LockTenantRulesAsync(db, tenant.Value, inner);
                await EnsureStateAsync(db, tenant.Value, inner);
                if (await HasActiveVerificationAsync(db, tenant.Value, inner))
                    return new("conflict", Code: "verification_in_progress");
                if (id is null && !await ValidateReferencesAsync(db, tenant.Value, kind, cleaned, inner))
                    return Invalid("audit_rule_reference_invalid");
                if (id is null && kind == "categories" && (!ValidateCategoryRules(cleaned)
                    || !await ValidateCategoryAttachmentsAsync(db, tenant.Value, cleaned, inner)))
                    return Invalid("audit_rule_category_validation_invalid");
                long version = await CurrentVersionAsync(db, tenant.Value, inner);
                if (expectedVersion is not null && expectedVersion.Value != version)
                    return new("conflict", Code: "audit_rules_version_changed", Version: version);

                int entityId;
                string action;
                if (id is null)
                {
                    if (kind == "categories")
                    {
                        int[] documentIds = ReadIds(cleaned["document_ids"]) ?? [];
                        var required = new JsonObject();
                        foreach (int documentId in documentIds)
                            required[documentId.ToString(System.Globalization.CultureInfo.InvariantCulture)] = true;
                        cleaned["matrix_version"] = 1;
                        cleaned["document_required"] = required;
                        cleaned["document_assertions"] = new JsonObject();
                        cleaned["cross_check_assertions"] = new JsonObject();
                    }
                    if (kind == "documents" && cleaned["form_fields"] is JsonArray newFields)
                    {
                        await AssignFormFieldIdsAsync(db, newFields, null, inner);
                        definition = cleaned.ToJsonString(JsonOptions);
                        if (Encoding.UTF8.GetByteCount(definition) > options.Value.MaximumDefinitionBytes)
                            return new("too_large", Code: "audit_rule_definition_too_large");
                    }
                    if (kind == "voucher-checks" && cleaned["sort_order"] is null)
                    {
                        await using var nextOrder = db.CreateCommand("""
                            SELECT COALESCE(MAX(CASE WHEN definition->>'sort_order' ~ '^[0-9]{1,9}$'
                                THEN (definition->>'sort_order')::integer END),-1)+1
                            FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='voucher-checks'
                            """);
                        nextOrder.Parameters.AddWithValue(tenant.Value);
                        cleaned["sort_order"] = Convert.ToInt32(await nextOrder.ExecuteScalarAsync(inner));
                        definition = cleaned.ToJsonString(JsonOptions);
                    }
                    await using var insert = db.CreateCommand("""
                        INSERT INTO public.platform_audit_rule_entities
                            (tenant_id,entity_kind,definition,created_by,updated_by)
                        VALUES($1,$2,$3,$4,$4) RETURNING id
                        """);
                    insert.Parameters.AddWithValue(tenant.Value); insert.Parameters.AddWithValue(kind);
                    insert.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = definition });
                    insert.Parameters.AddWithValue(session!.UserId);
                    entityId = (int)(await insert.ExecuteScalarAsync(inner))!; action = "created";
                    if (kind == "cross-checks" && cleaned["matrix"] is JsonObject matrix)
                    {
                        matrix["cross_check_id"] = entityId; definition = cleaned.ToJsonString(JsonOptions);
                        await using var updateMatrix = db.CreateCommand("UPDATE public.platform_audit_rule_entities SET definition=$1 WHERE tenant_id=$2 AND entity_kind='cross-checks' AND id=$3");
                        updateMatrix.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = definition });
                        updateMatrix.Parameters.AddWithValue(tenant.Value); updateMatrix.Parameters.AddWithValue(entityId);
                        await updateMatrix.ExecuteNonQueryAsync(inner);
                    }
                }
                else
                {
                    entityId = id.Value; action = "updated";
                    await using (var existing = db.CreateCommand("""
                        SELECT definition::text FROM public.platform_audit_rule_entities
                        WHERE tenant_id=$1 AND entity_kind=$2 AND id=$3 FOR UPDATE
                        """))
                    {
                        existing.Parameters.AddWithValue(tenant.Value); existing.Parameters.AddWithValue(kind);
                        existing.Parameters.AddWithValue(entityId);
                        if (await existing.ExecuteScalarAsync(inner) is not string prior) return NotFound();
                        var priorDefinition = JsonNode.Parse(prior)!.AsObject();
                        int[] priorCategoryDocumentIds = kind == "categories" ? ReadIds(priorDefinition["document_ids"]) ?? [] : [];
                        long? priorCategoryMatrixVersion = kind == "categories"
                            ? priorDefinition["matrix_version"]?.GetValue<int>() ?? 1 : null;
                        var priorDocumentRequired = kind == "categories"
                            ? priorDefinition["document_required"]?.DeepClone() as JsonObject ?? new JsonObject() : null;
                        var priorDocumentAssertions = kind == "categories"
                            ? priorDefinition["document_assertions"]?.DeepClone() as JsonObject ?? new JsonObject() : null;
                        var priorCrossCheckAssertions = kind == "categories"
                            ? priorDefinition["cross_check_assertions"]?.DeepClone() as JsonObject ?? new JsonObject() : null;
                        var submitted = JsonNode.Parse(request.GetRawText())?.AsObject();
                        if (submitted?["definition"] is JsonObject nested)
                            submitted = (JsonObject)nested.DeepClone();
                        if (submitted is null) return Invalid("invalid_audit_rule_definition");
                        foreach (var pair in submitted)
                            priorDefinition[pair.Key] = pair.Value?.DeepClone();
                        cleaned = ValidateDefinition(kind,
                            JsonDocument.Parse(priorDefinition.ToJsonString(JsonOptions)).RootElement,
                            requireComplete: true);
                        if (cleaned is null) return Invalid("invalid_audit_rule_definition");
                        if (kind == "categories")
                        {
                            int[] nextDocumentIds = ReadIds(cleaned["document_ids"]) ?? [];
                            int[] nextCrossCheckIds = ReadIds(cleaned["cross_check_ids"]) ?? [];
                            var documentKeys = nextDocumentIds.Select(value => value.ToString(System.Globalization.CultureInfo.InvariantCulture))
                                .ToHashSet(StringComparer.Ordinal);
                            var crossCheckKeys = nextCrossCheckIds.Select(value => value.ToString(System.Globalization.CultureInfo.InvariantCulture))
                                .ToHashSet(StringComparer.Ordinal);
                            var required = priorDocumentRequired ?? new JsonObject();
                            var documentAssertions = priorDocumentAssertions ?? new JsonObject();
                            var crossCheckAssertions = priorCrossCheckAssertions ?? new JsonObject();
                            foreach (string key in required.Select(pair => pair.Key).Where(key => !documentKeys.Contains(key)).ToArray()) required.Remove(key);
                            foreach (string key in documentAssertions.Select(pair => pair.Key).Where(key => !documentKeys.Contains(key)).ToArray()) documentAssertions.Remove(key);
                            foreach (string key in crossCheckAssertions.Select(pair => pair.Key).Where(key => !crossCheckKeys.Contains(key)).ToArray()) crossCheckAssertions.Remove(key);
                            foreach (string key in documentKeys) if (required[key] is null) required[key] = true;
                            bool membershipChanged = !priorCategoryDocumentIds.ToHashSet().SetEquals(nextDocumentIds);
                            cleaned["document_required"] = required;
                            cleaned["document_assertions"] = documentAssertions;
                            cleaned["cross_check_assertions"] = crossCheckAssertions;
                            cleaned["matrix_version"] = (priorCategoryMatrixVersion ?? 1) + (membershipChanged ? 1 : 0);
                        }
                        if (kind == "documents")
                        {
                            string currentType = ReadString(cleaned["document_type"]) ?? "linked_template";
                            if (currentType == "form" && (cleaned["form_fields"] is null)
                                && priorDefinition["form_fields"] is JsonNode savedFields)
                                cleaned["form_fields"] = savedFields.DeepClone();
                            if (cleaned["verification_field_policy"] is null)
                            {
                                string? oldTemplate = priorDefinition["template_id"]?.ToJsonString();
                                string? newTemplate = cleaned["template_id"]?.ToJsonString();
                                cleaned["verification_field_policy"] = oldTemplate == newTemplate
                                    ? priorDefinition["verification_field_policy"]?.DeepClone() : null;
                            }
                        }
                        if (kind == "voucher-checks" && cleaned["sort_order"] is null
                            && priorDefinition["sort_order"] is JsonNode order)
                            cleaned["sort_order"] = order.DeepClone();
                        if (kind == "cross-checks" && cleaned["matrix"] is JsonObject matrix)
                            matrix["cross_check_id"] = entityId;
                        if (!await ValidateReferencesAsync(db, tenant.Value, kind, cleaned, inner)
                            || kind == "categories" && (!ValidateCategoryRules(cleaned)
                                || !await ValidateCategoryAttachmentsAsync(db, tenant.Value, cleaned, inner)))
                            return Invalid("audit_rule_reference_invalid");
                        if (kind == "documents" && cleaned["form_fields"] is JsonArray formFields)
                            await AssignFormFieldIdsAsync(db, formFields, priorDefinition["form_fields"] as JsonArray, inner);
                        definition = cleaned.ToJsonString(JsonOptions);
                        if (Encoding.UTF8.GetByteCount(definition) > options.Value.MaximumDefinitionBytes)
                            return new("too_large", Code: "audit_rule_definition_too_large");
                    }
                    await using var update = db.CreateCommand("""
                        UPDATE public.platform_audit_rule_entities
                        SET definition=$1,revision=revision+1,updated_by=$2,updated_at=now()
                        WHERE tenant_id=$3 AND entity_kind=$4 AND id=$5
                        RETURNING id
                        """);
                    update.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = definition });
                    update.Parameters.AddWithValue(session!.UserId); update.Parameters.AddWithValue(tenant.Value);
                    update.Parameters.AddWithValue(kind); update.Parameters.AddWithValue(entityId);
                    if (await update.ExecuteScalarAsync(inner) is null) return NotFound();
                }
                string? name = ReadName(cleaned);
                long nextVersion = await PublishChangeAsync(db, tenant.Value, kind, entityId, action, name, session!.UserId, inner);
                await AuditAsync(db, session, tenant.Value, action, kind, entityId, name, nextVersion, inner);
                string idField = kind switch
                {
                    "documents" => "document_id", "cross-checks" => "cross_check_id", "categories" => "category_id",
                    "assertions" => "assertion_id", _ => "voucher_check_id"
                };
                var response = new Dictionary<string, object?>
                { ["message"] = $"{AuditRuleKinds.Singular(kind)} {action} successfully", [idField] = entityId, ["config_version"] = nextVersion };
                return Success(response, version: nextVersion, id: entityId);
            }, token);
        }
        catch (PostgresException ex) when (ex.SqlState == PostgresErrorCodes.UniqueViolation)
        { return new("conflict", Code: "audit_rule_conflict"); }
    }, ct);

    public Task<AuditRuleResult> SetActiveAsync(VerifiedSession? session, string kind, int id, bool active,
        long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (!AuditRuleKinds.Valid(kind) || id <= 0 || expectedVersion is <= 0) return Invalid("invalid_audit_rule_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner);
            await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner)) return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion.Value != version)
                return new("conflict", Code: "audit_rules_version_changed", Version: version);
            await using var update = db.CreateCommand("""
                UPDATE public.platform_audit_rule_entities SET is_active=$1,revision=revision+1,
                    updated_by=$2,updated_at=now()
                WHERE tenant_id=$3 AND entity_kind=$4 AND id=$5 AND is_active IS DISTINCT FROM $1
                RETURNING definition::text
                """);
            update.Parameters.AddWithValue(active); update.Parameters.AddWithValue(session!.UserId);
            update.Parameters.AddWithValue(tenant.Value); update.Parameters.AddWithValue(kind); update.Parameters.AddWithValue(id);
            if (await update.ExecuteScalarAsync(inner) is not string raw)
            {
                await using var exists = db.CreateCommand("SELECT 1 FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind=$2 AND id=$3");
                exists.Parameters.AddWithValue(tenant.Value); exists.Parameters.AddWithValue(kind); exists.Parameters.AddWithValue(id);
                return await exists.ExecuteScalarAsync(inner) is null ? NotFound() : new("conflict", Code: "audit_rule_state_unchanged");
            }
            string action = active ? "restored" : "deleted";
            string? name = ReadName(JsonNode.Parse(raw));
            long nextVersion = await PublishChangeAsync(db, tenant.Value, kind, id, action, name, session!.UserId, inner);
            await AuditAsync(db, session, tenant.Value, action, kind, id, name, nextVersion, inner);
            string idField = kind switch
            {
                "documents" => "document_id", "cross-checks" => "cross_check_id", "categories" => "category_id",
                "assertions" => "assertion_id", _ => "voucher_check_id"
            };
            return Success(new Dictionary<string, object?>
            { ["message"] = $"{AuditRuleKinds.Singular(kind)} {action} successfully", [idField] = id,
                ["is_active"] = active, ["config_version"] = nextVersion }, version: nextVersion, id: id);
        }, token);
    }, ct);

    public Task<AuditRuleResult> PropertyAsync(VerifiedSession? session, string kind, int id, string property,
        JsonElement? replacement = null, long? expectedVersion = null, CancellationToken ct = default) => Run(async token =>
    {
        if (!AuditRuleKinds.Valid(kind) || id <= 0 || !AllowedProperty(kind, property) || expectedVersion is <= 0)
            return Invalid("invalid_audit_rule_property");
        if (replacement is { } candidate && Encoding.UTF8.GetByteCount(candidate.GetRawText()) > options.Value.MaximumDefinitionBytes)
            return new("too_large", Code: "audit_rule_definition_too_large");
        if (replacement is null)
        {
            return await transactions.ExecuteAsync(async (db, inner) =>
            {
                var tenant = await AuthorizeAsync(db, session, false, inner);
                if (tenant is null) return Forbidden();
                await EnsureStateAsync(db, tenant.Value, inner);
                await using var read = db.CreateCommand("""
                    SELECT definition::text FROM public.platform_audit_rule_entities
                    WHERE tenant_id=$1 AND entity_kind=$2 AND id=$3
                    """);
                read.Parameters.AddWithValue(tenant.Value); read.Parameters.AddWithValue(kind); read.Parameters.AddWithValue(id);
                if (await read.ExecuteScalarAsync(inner) is not string raw) return NotFound();
                var definition = JsonNode.Parse(raw)!.AsObject();
                JsonElement propertyValue;
                if (definition[property] is JsonNode value)
                    propertyValue = JsonDocument.Parse(value.ToJsonString(JsonOptions)).RootElement.Clone();
                else if (kind == "categories" && property == "validation_rules_json")
                    propertyValue = JsonSerializer.SerializeToElement(new { procedure_evidence = new { }, mathematical_cross_checks = Array.Empty<object>() }, JsonOptions);
                else if (kind == "cross-checks" && property == "matrix")
                    propertyValue = JsonSerializer.SerializeToElement(new { cross_check_id = id, documents = Array.Empty<object>(), fields = Array.Empty<object>() }, JsonOptions);
                else propertyValue = JsonSerializer.SerializeToElement(Array.Empty<object>(), JsonOptions);
                if (kind == "cross-checks" && property == "matrix")
                    propertyValue = await MatrixViewAsync(db, tenant.Value, id, definition, inner);
                return Success(propertyValue, await CurrentVersionAsync(db, tenant.Value, inner));
            }, token);
        }
        var incoming = replacement.Value;
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner);
            await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner)) return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion.Value != version)
                return new("conflict", Code: "audit_rules_version_changed", Version: version);
            await using var read = db.CreateCommand("""
                SELECT definition::text FROM public.platform_audit_rule_entities
                WHERE tenant_id=$1 AND entity_kind=$2 AND id=$3 FOR UPDATE
                """);
            read.Parameters.AddWithValue(tenant.Value); read.Parameters.AddWithValue(kind); read.Parameters.AddWithValue(id);
            if (await read.ExecuteScalarAsync(inner) is not string raw) return NotFound();
            var definition = JsonNode.Parse(raw)!.AsObject();
            var priorValue = definition[property]?.DeepClone();
            var incomingValue = JsonNode.Parse(incoming.GetRawText());
            if (kind == "categories" && property == "validation_rules_json"
                && incomingValue is JsonObject incomingRules && priorValue is JsonObject priorRules
                && incomingRules["mathematical_cross_checks"] is null
                && priorRules["mathematical_cross_checks"] is JsonNode oldMath)
                incomingRules["mathematical_cross_checks"] = oldMath.DeepClone();
            definition[property] = incomingValue;
            if (kind == "categories" && property == "document_ids")
            {
                var priorIds = ReadIds(priorValue);
                var ids = ReadIds(definition[property]);
                if (ids is null || priorIds is null) return Invalid("invalid_category_documents");
                var required = definition["document_required"] as JsonObject ?? new JsonObject();
                var tags = definition["document_assertions"] as JsonObject ?? new JsonObject();
                var keep = ids.Select(value => value.ToString(System.Globalization.CultureInfo.InvariantCulture)).ToHashSet(StringComparer.Ordinal);
                foreach (string key in required.Select(pair => pair.Key).Where(key => !keep.Contains(key)).ToArray()) required.Remove(key);
                foreach (string key in tags.Select(pair => pair.Key).Where(key => !keep.Contains(key)).ToArray()) tags.Remove(key);
                foreach (string key in keep) if (required[key] is null) required[key] = true;
                definition["document_required"] = required; definition["document_assertions"] = tags;
                bool membershipChanged = !priorIds.ToHashSet().SetEquals(ids);
                if (membershipChanged)
                    definition["matrix_version"] = (definition["matrix_version"]?.GetValue<int>() ?? 1) + 1;
            }
            if (kind == "categories" && property == "cross_check_ids")
            {
                var ids = ReadIds(definition[property]);
                if (ids is null) return Invalid("invalid_category_cross_checks");
                var tags = definition["cross_check_assertions"] as JsonObject ?? new JsonObject();
                var keep = ids.Select(value => value.ToString(System.Globalization.CultureInfo.InvariantCulture)).ToHashSet(StringComparer.Ordinal);
                foreach (string key in tags.Select(pair => pair.Key).Where(key => !keep.Contains(key)).ToArray()) tags.Remove(key);
                definition["cross_check_assertions"] = tags;
            }
            if (kind == "cross-checks" && property == "matrix" && incomingValue is JsonObject matrix
                && matrix["documents"] is JsonArray columns)
            {
                var ids = ReadObjectIds(columns, "document_id");
                if (ids is null) return Invalid("invalid_cross_check_matrix");
                matrix["cross_check_id"] = id;
                definition["document_ids"] = JsonSerializer.SerializeToNode(ids, JsonOptions);
            }
            if (!await ValidateReferencesAsync(db, tenant.Value, kind, definition, inner)
                || kind == "categories" && (!ValidateCategoryRules(definition)
                    || !await ValidateCategoryAttachmentsAsync(db, tenant.Value, definition, inner)))
                return Invalid("audit_rule_reference_invalid");
            string changed = definition.ToJsonString(JsonOptions);
            await using (var update = db.CreateCommand("""
                UPDATE public.platform_audit_rule_entities SET definition=$1,revision=revision+1,
                    updated_by=$2,updated_at=now() WHERE tenant_id=$3 AND entity_kind=$4 AND id=$5
                """))
            {
                update.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = changed });
                update.Parameters.AddWithValue(session!.UserId); update.Parameters.AddWithValue(tenant.Value);
                update.Parameters.AddWithValue(kind); update.Parameters.AddWithValue(id); await update.ExecuteNonQueryAsync(inner);
            }
            long nextVersion = await PublishChangeAsync(db, tenant.Value, kind, id, "updated", ReadName(definition), session!.UserId, inner);
            await AuditAsync(db, session, tenant.Value, "updated", kind, id, ReadName(definition), nextVersion, inner);
            return Success(JsonDocument.Parse(definition[property]!.ToJsonString(JsonOptions)).RootElement.Clone(),
                version: nextVersion, id: id);
        }, token);
    }, ct);

    public async Task<AuditRuleResult> DocumentTransactionApprovalAsync(VerifiedSession? session, int documentId,
        JsonElement payload, long? expectedVersion, CancellationToken ct = default)
    {
        if (payload.ValueKind != JsonValueKind.Object || !payload.TryGetProperty("transaction_level_approval", out var value)
            || value.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return Invalid("invalid_document_approval_flag");
        var replacement = JsonSerializer.SerializeToElement(value.GetBoolean(), JsonOptions);
        var result = await PropertyAsync(session, "documents", documentId, "transaction_level_approval", replacement, expectedVersion, ct);
        return result.Status == "success"
            ? Success(new { document_id = documentId, transaction_level_approval = value.GetBoolean() }, result.Version, id: documentId)
            : result;
    }

    private async Task<JsonElement> MatrixViewAsync(PostgresTransactionContext db, int tenant, int id,
        JsonObject definition, CancellationToken ct)
    {
        var matrix = EnsureMatrix(definition, id);
        var documents = matrix["documents"] as JsonArray ?? new JsonArray();
        foreach (var node in documents.OfType<JsonObject>())
        {
            int documentId = node["document_id"]?.GetValue<int>() ?? 0;
            var document = await ReadEntityAsync(db, tenant, "documents", documentId, ct);
            if (document is null) continue;
            var source = JsonNode.Parse(document.Definition.GetRawText())!.AsObject();
            string type = ReadString(source["document_type"]) ?? "linked_template";
            var fields = new JsonArray();
            if (type == "form" && source["form_fields"] is JsonArray formFields)
                foreach (var field in formFields.OfType<JsonObject>())
                    fields.Add(new JsonObject { ["field_name"] = ReadString(field["field_name"]),
                        ["data_type"] = ReadString(field["field_type"]) ?? "text",
                        ["linked_column"] = field["linked_column"]?.DeepClone(),
                        ["arithmetic_operands"] = field["arithmetic_operands"]?.DeepClone() ?? new JsonArray(),
                        ["source"] = "form" });
            else if (type == "physical_bill")
                foreach (string name in new[] { "Bill No", "Bill Date", "Vendor Name", "Quantity", "Rate", "Amount" })
                    fields.Add(new JsonObject { ["field_name"] = name,
                        ["data_type"] = name is "Quantity" or "Rate" or "Amount" ? "numeric" : "text", ["source"] = "legacy_physical_bill" });
            node["name"] = ReadString(source["name"]); node["template_id"] = source["template_id"]?.DeepClone();
            node["document_type"] = type; node["field_options"] = fields;
        }
        return JsonDocument.Parse(matrix.ToJsonString(JsonOptions)).RootElement.Clone();
    }

    public Task<AuditRuleResult> RulebookAsync(VerifiedSession? session, CancellationToken ct = default) => Run(async token =>
    {
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, false, inner);
            if (tenant is null) return Forbidden();
            await EnsureStateAsync(db, tenant.Value, inner);
            await LockTenantRulesAsync(db, tenant.Value, inner);
            await EnsureSnapshotAsync(db, tenant.Value, inner);
            await using var command = db.CreateCommand("""
                SELECT config_version,fingerprint,evaluation_fingerprint,revision,payload::text,published_at
                FROM public.platform_audit_rule_snapshots WHERE tenant_id=$1
                """);
            command.Parameters.AddWithValue(tenant.Value);
            await using var reader = await command.ExecuteReaderAsync(inner);
            if (!await reader.ReadAsync(inner)) return new("unavailable", Code: "audit_rule_snapshot_unavailable");
            using var json = JsonDocument.Parse(reader.GetString(4));
            return Success(new { tenant_id = tenant.Value, config_version = reader.GetInt64(0),
                fingerprint = reader.GetString(1).Trim(), evaluation_fingerprint = reader.GetString(2).Trim(),
                revision = reader.GetInt64(3), snapshot = json.RootElement.Clone(), published_at = reader.GetDateTime(5) });
        }, token);
    }, ct);

    public Task<AuditRuleResult> ChangesAsync(VerifiedSession? session, long sinceVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (sinceVersion < 0) return Invalid("invalid_audit_rules_version");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, false, inner);
            if (tenant is null) return Forbidden();
            await EnsureStateAsync(db, tenant.Value, inner);
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            var changes = new List<object>();
            await using var command = db.CreateCommand("""
                SELECT config_version,entity_kind,entity_id,action,entity_name,actor_user_id,changed_at
                FROM public.platform_audit_rule_changes WHERE tenant_id=$1 AND config_version>$2
                ORDER BY config_version LIMIT $3
                """);
            command.Parameters.AddWithValue(tenant.Value); command.Parameters.AddWithValue(sinceVersion); command.Parameters.AddWithValue(MaximumChanges + 1);
            await using var reader = await command.ExecuteReaderAsync(inner);
            while (await reader.ReadAsync(inner))
            {
                if (changes.Count >= MaximumChanges) return new("too_large", Code: "audit_rule_change_capacity", Version: version);
                changes.Add(new { config_version = reader.GetInt64(0), entity_kind = reader.GetString(1),
                    entity_id = reader.IsDBNull(2) ? (int?)null : reader.GetInt32(2), action = reader.GetString(3),
                    entity_name = reader.IsDBNull(4) ? null : reader.GetString(4), actor_user_id = reader.GetInt32(5), changed_at = reader.GetDateTime(6) });
            }
            return Success(new { tenant_id = tenant.Value, current_version = version, changes });
        }, token);
    }, ct);

    public Task<AuditRuleResult> MatrixFieldAsync(VerifiedSession? session, int crossCheckId, string operation,
        int? fieldId, int? documentId, JsonElement? payload, long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (expectedVersion is <= 0 || (crossCheckId < 0) || (fieldId is <= 0) || (documentId is <= 0)) return Invalid("invalid_audit_rule_property");
        JsonObject? input = payload is { } json && json.ValueKind == JsonValueKind.Object
            ? JsonNode.Parse(json.GetRawText())?.AsObject() : null;
        if (payload is not null && input is null) return Invalid("invalid_audit_rule_property");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner); await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner)) return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion != version) return new("conflict", Code: "audit_rules_version_changed", Version: version);

            var entities = new List<(int Id, JsonObject Definition)>();
            await using (var read = db.CreateCommand("SELECT id,definition::text FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='cross-checks' ORDER BY id FOR UPDATE"))
            {
                read.Parameters.AddWithValue(tenant.Value); await using var reader = await read.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner)) entities.Add((reader.GetInt32(0), JsonNode.Parse(reader.GetString(1))!.AsObject()));
            }
            (int Id, JsonObject Definition)? target = null; int fieldIndex = -1;
            foreach (var entity in entities)
            {
                if (crossCheckId > 0 && entity.Id != crossCheckId) continue;
                var matrix = EnsureMatrix(entity.Definition, entity.Id);
                var fields = matrix["fields"]!.AsArray();
                int index = -1;
                for (int fieldIndexCandidate = 0; fieldIndexCandidate < fields.Count; fieldIndexCandidate++)
                    if (fields[fieldIndexCandidate] is JsonObject field && fieldId is int expected
                        && field["id"]?.GetValue<int>() == expected) { index = fieldIndexCandidate; break; }
                if ((operation is "delete" or "cell" or "condition") && index >= 0)
                { target = entity; fieldIndex = index; break; }
                if (crossCheckId > 0 && entity.Id == crossCheckId) { target = entity; break; }
            }
            if (target is null) return operation is "delete" or "cell" or "condition" ? NotFound() : NotFound();
            int targetId = target.Value.Id; var definition = target.Value.Definition; var targetMatrix = EnsureMatrix(definition, targetId);
            var fieldRows = targetMatrix["fields"]!.AsArray();
            string? message;
            switch (operation)
            {
                case "add":
                {
                    int nextId = 1;
                    foreach (var sourceEntity in entities)
                        foreach (var node in EnsureMatrix(sourceEntity.Definition, sourceEntity.Id)["fields"]!.AsArray())
                            if (node is JsonObject row && row["id"]?.GetValue<int>() is int existingId) nextId = Math.Max(nextId, existingId + 1);
                    fieldRows.Add(new JsonObject { ["id"] = nextId, ["sort_order"] = fieldRows.Count,
                        ["cells"] = new JsonObject(), ["operator"] = "=", ["tolerance"] = 0,
                        ["missing_data_behavior"] = "not_configured", ["blocking"] = false, ["risk"] = "Medium" });
                    message = "Cross-check field row added"; break;
                }
                case "delete":
                    if (fieldIndex < 0) return NotFound();
                    fieldRows.RemoveAt(fieldIndex); Renumber(fieldRows); message = "Cross-check field row deleted"; break;
                case "reorder":
                {
                    if (input?["field_ids"] is not JsonArray requested) return Invalid("invalid_cross_check_field_order");
                    int[] ids = ReadIds(requested) ?? [];
                    int[] existingIds = fieldRows.Select(node => node is JsonObject row ? row["id"]?.GetValue<int>() ?? -1 : -1).ToArray();
                    if (ids.Length != existingIds.Length || !ids.Order().SequenceEqual(existingIds.Order())) return Invalid("invalid_cross_check_field_order");
                    var byId = fieldRows.OfType<JsonObject>().ToDictionary(row => row["id"]!.GetValue<int>());
                    var ordered = new JsonArray();
                    foreach (int id in ids) ordered.Add(byId[id].DeepClone());
                    targetMatrix["fields"] = ordered; Renumber(ordered); message = "Cross-check field rows reordered"; break;
                }
                case "documents":
                {
                    if (input?["document_ids"] is not JsonArray requested) return Invalid("invalid_cross_check_documents");
                    int[] ids = ReadIds(requested) ?? [];
                    if (ids.Length != requested.Count || (ids.Length == 0 && ReadBoolean(definition["compare_to_voucher_line"]))) return Invalid("invalid_cross_check_documents");
                    var columns = new JsonArray();
                    var documentFields = definition["document_fields"] as JsonObject ?? new JsonObject();
                    var retained = ids.Select(value => value.ToString(System.Globalization.CultureInfo.InvariantCulture)).ToHashSet(StringComparer.Ordinal);
                    foreach (string key in documentFields.Select(pair => pair.Key).Where(key => !retained.Contains(key)).ToArray()) documentFields.Remove(key);
                    foreach (int id in ids)
                    {
                        var document = await ReadEntityAsync(db, tenant.Value, "documents", id, inner);
                        if (document is null || !document.IsActive) return Invalid("audit_rule_reference_invalid");
                        var source = JsonNode.Parse(document.Definition.GetRawText())!.AsObject();
                        string key = id.ToString(System.Globalization.CultureInfo.InvariantCulture);
                        var column = new JsonObject { ["document_id"] = id, ["name"] = ReadString(source["name"]),
                            ["template_id"] = source["template_id"]?.DeepClone(),
                            ["document_type"] = ReadString(source["document_type"]) ?? "linked_template", ["sort_order"] = columns.Count };
                        column["compare_field"] = documentFields[key]?.DeepClone(); columns.Add(column);
                    }
                    var allowed = ids.Select(id => id.ToString(System.Globalization.CultureInfo.InvariantCulture)).ToHashSet(StringComparer.Ordinal);
                    foreach (var row in fieldRows.OfType<JsonObject>())
                    {
                        var cells = row["cells"] as JsonObject ?? new JsonObject();
                        foreach (var key in cells.Select(pair => pair.Key).Where(key => !allowed.Contains(key)).ToArray()) cells.Remove(key);
                        row["cells"] = cells;
                    }
                    targetMatrix["documents"] = columns; definition["document_ids"] = JsonSerializer.SerializeToNode(ids, JsonOptions);
                    definition["document_fields"] = documentFields;
                    message = "Cross-check document columns updated"; break;
                }
                case "cell":
                {
                    if (fieldIndex < 0 || documentId is not int docId || input is null) return NotFound();
                    var columns = targetMatrix["documents"] as JsonArray ?? new JsonArray();
                    if (!columns.OfType<JsonObject>().Any(column => column["document_id"]?.GetValue<int>() == docId)) return Invalid("cross_check_document_not_attached");
                    if (input["column_name"] is JsonValue fieldValue && !fieldValue.TryGetValue<string>(out _)) return Invalid("invalid_cross_check_cell");
                    var columnName = input["column_name"] is null ? null : ReadString(input["column_name"]);
                    if (columnName is not null && !await DocumentFieldExistsAsync(db, tenant.Value, docId, columnName, inner)) return Invalid("invalid_cross_check_cell");
                    var cells = fieldRows[fieldIndex]!.AsObject()["cells"] as JsonObject ?? new JsonObject();
                    string key = docId.ToString(System.Globalization.CultureInfo.InvariantCulture);
                    if (columnName is null) cells.Remove(key); else cells[key] = columnName;
                    fieldRows[fieldIndex]!["cells"] = cells; message = columnName is null ? "Cross-check cell cleared" : "Cross-check cell set"; break;
                }
                case "condition":
                {
                    if (fieldIndex < 0 || input is null) return NotFound();
                    string op = ReadString(input["operator"]) ?? "=";
                    string missing = ReadString(input["missing_data_behavior"]) ?? "not_configured";
                    string risk = ReadString(input["risk"]) ?? "Medium";
                    double tolerance = input["tolerance"] is JsonValue amount && amount.TryGetValue<double>(out double parsed) ? parsed : 0;
                    if (!new[] { "=", ">=", "<=", ">", "<", "!=" }.Contains(op, StringComparer.Ordinal)
                        || !double.IsFinite(tolerance) || tolerance < 0
                        || !new[] { "not_configured", "not_linked", "missing", "shortfall" }.Contains(missing, StringComparer.Ordinal)
                        || !new[] { "Low", "Medium", "High" }.Contains(risk, StringComparer.Ordinal)) return Invalid("invalid_cross_check_condition");
                    var row = fieldRows[fieldIndex]!.AsObject(); row["operator"] = op; row["tolerance"] = tolerance;
                    row["missing_data_behavior"] = missing; row["blocking"] = ReadBoolean(input["blocking"]); row["risk"] = risk;
                    message = "Cross-check condition updated"; break;
                }
                case "meta":
                {
                    if (input is null || ReadName(input) is not { } name || ReadString(input["comparison_type"]) is not { } comparison
                        || !AuditRuleKinds.ComparisonTypes.Contains(comparison, StringComparer.Ordinal)) return Invalid("invalid_cross_check_metadata");
                    if (ReadBoolean(input["compare_to_voucher_line"]) && (ReadIds(definition["document_ids"]) ?? []).Length == 0) return Invalid("invalid_cross_check_metadata");
                    definition["name"] = name; definition["comparison_type"] = comparison;
                    definition["compare_to_voucher_line"] = ReadBoolean(input["compare_to_voucher_line"]);
                    message = "Cross-check metadata updated"; break;
                }
                default: return Invalid("invalid_cross_check_operation");
            }
            return await StoreMutationAsync(db, session!, tenant.Value, "cross-checks", targetId, definition, message!, inner);
        }, token);
    }, ct);

    private static JsonObject EnsureMatrix(JsonObject definition, int crossCheckId)
    {
        if (definition["matrix"] is JsonObject matrix) return matrix;
        matrix = new JsonObject { ["cross_check_id"] = crossCheckId, ["documents"] = new JsonArray(), ["fields"] = new JsonArray() };
        definition["matrix"] = matrix; return matrix;
    }

    private static void Renumber(JsonArray fields)
    {
        for (int index = 0; index < fields.Count; index++)
            if (fields[index] is JsonObject row) row["sort_order"] = index;
    }

    private async Task<AuditRuleResult> StoreMutationAsync(PostgresTransactionContext db, VerifiedSession session, int tenant,
        string kind, int id, JsonObject definition, string message, CancellationToken ct)
    {
        string raw = definition.ToJsonString(JsonOptions);
        if (Encoding.UTF8.GetByteCount(raw) > options.Value.MaximumDefinitionBytes) return new("too_large", Code: "audit_rule_definition_too_large");
        await using (var update = db.CreateCommand("UPDATE public.platform_audit_rule_entities SET definition=$1,revision=revision+1,updated_by=$2,updated_at=now() WHERE tenant_id=$3 AND entity_kind=$4 AND id=$5"))
        {
            update.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = raw });
            update.Parameters.AddWithValue(session.UserId); update.Parameters.AddWithValue(tenant); update.Parameters.AddWithValue(kind); update.Parameters.AddWithValue(id);
            if (await update.ExecuteNonQueryAsync(ct) != 1) return NotFound();
        }
        long version = await PublishChangeAsync(db, tenant, kind, id, "updated", ReadName(definition), session.UserId, ct);
        await AuditAsync(db, session, tenant, "updated", kind, id, ReadName(definition), version, ct);
        return Success(new { message, config_version = version }, version, id: id);
    }

    public Task<AuditRuleResult> CategoryRequiredAsync(VerifiedSession? session, int categoryId, int documentId,
        JsonElement payload, long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (categoryId <= 0 || documentId <= 0 || expectedVersion is <= 0 || payload.ValueKind != JsonValueKind.Object
            || !payload.TryGetProperty("is_required", out var requiredValue) || requiredValue.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            return Invalid("invalid_category_document_requirement");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner); await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner)) return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion != version) return new("conflict", Code: "audit_rules_version_changed", Version: version);
            var category = await ReadEntityAsync(db, tenant.Value, "categories", categoryId, inner);
            if (category is null) return NotFound();
            var definition = JsonNode.Parse(category.Definition.GetRawText())!.AsObject();
            if (!(ReadIds(definition["document_ids"]) ?? []).Contains(documentId)) return new("not_found", Code: "category_document_not_attached");
            var required = definition["document_required"] as JsonObject ?? new JsonObject();
            string key = documentId.ToString(System.Globalization.CultureInfo.InvariantCulture);
            bool value = requiredValue.GetBoolean(), previous = required[key]?.GetValue<bool>() ?? true;
            required[key] = value; definition["document_required"] = required;
            if (previous != value) definition["matrix_version"] = (definition["matrix_version"]?.GetValue<int>() ?? 1) + 1;
            string raw = definition.ToJsonString(JsonOptions);
            await using (var update = db.CreateCommand("UPDATE public.platform_audit_rule_entities SET definition=$1,revision=revision+1,updated_by=$2,updated_at=now() WHERE tenant_id=$3 AND entity_kind='categories' AND id=$4"))
            {
                update.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = raw });
                update.Parameters.AddWithValue(session!.UserId); update.Parameters.AddWithValue(tenant.Value); update.Parameters.AddWithValue(categoryId);
                await update.ExecuteNonQueryAsync(inner);
            }
            long next = await PublishChangeAsync(db, tenant.Value, "categories", categoryId, "updated", ReadName(definition), session!.UserId, inner);
            await AuditAsync(db, session!, tenant.Value, "updated", "categories", categoryId, ReadName(definition), next, inner);
            return Success(new { category_id = categoryId, document_id = documentId, is_required = value }, next, id: categoryId);
        }, token);
    }, ct);

    public Task<AuditRuleResult> CategoryAssertionTagsAsync(VerifiedSession? session, int categoryId, string kind,
        int itemId, JsonElement? payload, long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (categoryId <= 0 || itemId <= 0 || kind is not ("document" or "cross-check") || expectedVersion is <= 0)
            return Invalid("invalid_category_assertions");
        int[]? assertionIds = null;
        if (payload is { } json)
        {
            if (json.ValueKind != JsonValueKind.Object || !json.TryGetProperty("assertion_ids", out var idsValue)
                || idsValue.ValueKind != JsonValueKind.Array) return Invalid("invalid_category_assertions");
            assertionIds = idsValue.EnumerateArray().Select(value => value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int id) ? id : -1).ToArray();
            if (assertionIds.Any(id => id <= 0) || assertionIds.Distinct().Count() != assertionIds.Length) return Invalid("invalid_category_assertions");
        }
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, assertionIds is not null, inner);
            if (tenant is null) return Forbidden();
            if (assertionIds is null)
            {
                var categoryRead = await ReadEntityAsync(db, tenant.Value, "categories", categoryId, inner);
                if (categoryRead is null) return NotFound();
                var def = JsonNode.Parse(categoryRead.Definition.GetRawText())!.AsObject();
                string property = kind == "document" ? "document_assertions" : "cross_check_assertions";
                string getAttachedProperty = kind == "document" ? "document_ids" : "cross_check_ids";
                string itemKey = itemId.ToString(System.Globalization.CultureInfo.InvariantCulture);
                if (!(ReadIds(def[getAttachedProperty]) ?? []).Contains(itemId)) return new("not_found", Code: "category_item_not_attached");
                JsonArray values = await AssertionViewsAsync(db, tenant.Value, ReadIds(def[property]?[itemKey]) ?? [], inner);
                return Success(JsonDocument.Parse(values.ToJsonString(JsonOptions)).RootElement.Clone(),
                    await CurrentVersionAsync(db, tenant.Value, inner), id: categoryId);
            }
            await LockTenantRulesAsync(db, tenant.Value, inner); await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner)) return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion != version) return new("conflict", Code: "audit_rules_version_changed", Version: version);
            var category = await ReadEntityAsync(db, tenant.Value, "categories", categoryId, inner);
            if (category is null) return NotFound();
            var definition = JsonNode.Parse(category.Definition.GetRawText())!.AsObject();
            string categoryProperty = kind == "document" ? "document_assertions" : "cross_check_assertions";
            string attachedProperty = kind == "document" ? "document_ids" : "cross_check_ids";
            if (!(ReadIds(definition[attachedProperty]) ?? []).Contains(itemId)) return new("not_found", Code: "category_item_not_attached");
            if (!await ValidateAssertionIdsAsync(db, tenant.Value, assertionIds, inner)) return Invalid("audit_rule_reference_invalid");
            var map = definition[categoryProperty] as JsonObject ?? new JsonObject();
            string key = itemId.ToString(System.Globalization.CultureInfo.InvariantCulture);
            map[key] = JsonSerializer.SerializeToNode(assertionIds, JsonOptions); definition[categoryProperty] = map;
            string raw = definition.ToJsonString(JsonOptions);
            await using (var update = db.CreateCommand("UPDATE public.platform_audit_rule_entities SET definition=$1,revision=revision+1,updated_by=$2,updated_at=now() WHERE tenant_id=$3 AND entity_kind='categories' AND id=$4"))
            {
                update.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = raw });
                update.Parameters.AddWithValue(session!.UserId); update.Parameters.AddWithValue(tenant.Value); update.Parameters.AddWithValue(categoryId);
                await update.ExecuteNonQueryAsync(inner);
            }
            long next = await PublishChangeAsync(db, tenant.Value, "categories", categoryId, "updated", ReadName(definition), session!.UserId, inner);
            await AuditAsync(db, session!, tenant.Value, "updated", "categories", categoryId, ReadName(definition), next, inner);
            object response = kind == "document"
                ? new { category_id = categoryId, document_id = itemId, assertion_ids = assertionIds } as object
                : new { category_id = categoryId, cross_check_id = itemId, assertion_ids = assertionIds };
            return Success(response, next, id: categoryId);
        }, token);
    }, ct);

    public Task<AuditRuleResult> VoucherCheckAssertionsAsync(VerifiedSession? session, int voucherCheckId,
        JsonElement? payload, long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        if (voucherCheckId <= 0 || expectedVersion is <= 0) return Invalid("invalid_voucher_check_id");
        int[]? ids = null;
        if (payload is { } json)
        {
            if (json.ValueKind != JsonValueKind.Object || !json.TryGetProperty("assertion_ids", out var array) || array.ValueKind != JsonValueKind.Array)
                return Invalid("invalid_voucher_check_assertions");
            ids = array.EnumerateArray().Select(value => value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int id) ? id : -1).ToArray();
            if (ids.Any(id => id <= 0) || ids.Distinct().Count() != ids.Length) return Invalid("invalid_voucher_check_assertions");
        }
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, ids is not null, inner);
            if (tenant is null) return Forbidden();
            var entity = await ReadEntityAsync(db, tenant.Value, "voucher-checks", voucherCheckId, inner);
            if (entity is null) return NotFound();
            if (ids is null)
            {
                var definition = JsonNode.Parse(entity.Definition.GetRawText())!.AsObject();
                var value = await AssertionViewsAsync(db, tenant.Value, ReadIds(definition["assertion_ids"]) ?? [], inner);
                return Success(JsonDocument.Parse(value.ToJsonString(JsonOptions)).RootElement.Clone(),
                    await CurrentVersionAsync(db, tenant.Value, inner), id: voucherCheckId);
            }
            await LockTenantRulesAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner)) return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion != version) return new("conflict", Code: "audit_rules_version_changed", Version: version);
            if (!await ValidateAssertionIdsAsync(db, tenant.Value, ids, inner)) return Invalid("audit_rule_reference_invalid");
            var updated = JsonNode.Parse(entity.Definition.GetRawText())!.AsObject();
            updated["assertion_ids"] = JsonSerializer.SerializeToNode(ids, JsonOptions);
            var result = await StoreMutationAsync(db, session!, tenant.Value, "voucher-checks", voucherCheckId, updated, "Voucher check Assertion tags updated", inner);
            return result.Status == "success"
                ? Success(new { voucher_check_id = voucherCheckId, assertion_ids = ids }, result.Version, id: voucherCheckId)
                : result;
        }, token);
    }, ct);

    public Task<AuditRuleResult> ReorderVoucherChecksAsync(VerifiedSession? session, JsonElement payload,
        long? expectedVersion, CancellationToken ct = default) => Run(async token =>
    {
        JsonElement values = payload.ValueKind == JsonValueKind.Array ? payload
            : payload.ValueKind == JsonValueKind.Object && payload.TryGetProperty("check_ids", out var ids) ? ids : default;
        if (values.ValueKind != JsonValueKind.Array || expectedVersion is <= 0) return Invalid("invalid_voucher_check_order");
        int[] order = values.EnumerateArray().Select(value => value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int id) ? id : -1).ToArray();
        if (order.Any(id => id <= 0) || order.Distinct().Count() != order.Length) return Invalid("invalid_voucher_check_order");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, true, inner);
            if (tenant is null) return Forbidden();
            await LockTenantRulesAsync(db, tenant.Value, inner); await EnsureStateAsync(db, tenant.Value, inner);
            if (await HasActiveVerificationAsync(db, tenant.Value, inner)) return new("conflict", Code: "verification_in_progress");
            long version = await CurrentVersionAsync(db, tenant.Value, inner);
            if (expectedVersion is not null && expectedVersion != version) return new("conflict", Code: "audit_rules_version_changed", Version: version);
            var entities = new List<(int Id, JsonObject Definition)>();
            await using (var read = db.CreateCommand("SELECT id,definition::text FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='voucher-checks' ORDER BY id FOR UPDATE"))
            {
                read.Parameters.AddWithValue(tenant.Value); await using var reader = await read.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner)) entities.Add((reader.GetInt32(0), JsonNode.Parse(reader.GetString(1))!.AsObject()));
            }
            var map = entities.ToDictionary(item => item.Id, item => item.Definition);
            if (order.Any(id => !map.ContainsKey(id))) return Invalid("invalid_voucher_check_order");
            foreach (var pair in order.Select((id, index) => (id, index)))
            {
                map[pair.id]["sort_order"] = pair.index;
                string raw = map[pair.id].ToJsonString(JsonOptions);
                await using var update = db.CreateCommand("UPDATE public.platform_audit_rule_entities SET definition=$1,revision=revision+1,updated_by=$2,updated_at=now() WHERE tenant_id=$3 AND entity_kind='voucher-checks' AND id=$4");
                update.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = raw });
                update.Parameters.AddWithValue(session!.UserId); update.Parameters.AddWithValue(tenant.Value); update.Parameters.AddWithValue(pair.id);
                await update.ExecuteNonQueryAsync(inner);
            }
            int? first = order.FirstOrDefault() is int firstId && firstId > 0 ? firstId : null;
            long next = await PublishChangeAsync(db, tenant.Value, "voucher-checks", first ?? 0, "updated", "Voucher Checks", session!.UserId, inner);
            await AuditAsync(db, session!, tenant.Value, "updated", "voucher-checks", first ?? 0, "Voucher Checks", next, inner);
            return Success(new { message = "Order updated successfully", config_version = next }, next);
        }, token);
    }, ct);

    public Task<AuditRuleResult> AssertionGapsAsync(VerifiedSession? session, int categoryId, CancellationToken ct = default) => Run(async token =>
    {
        if (categoryId <= 0) return Invalid("invalid_category_id");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var tenant = await AuthorizeAsync(db, session, false, inner);
            if (tenant is null) return Forbidden();
            var category = await ReadEntityAsync(db, tenant.Value, "categories", categoryId, inner);
            if (category is null) return NotFound();
            var categoryDefinition = JsonNode.Parse(category.Definition.GetRawText())!.AsObject();
            var relevant = new HashSet<int>(); var covered = new HashSet<int>();
            var allCategories = await ReadDefinitionsAsync(db, tenant.Value, "categories", inner);
            foreach (var item in allCategories)
            {
                foreach (int id in Tags(item["document_assertions"])) relevant.Add(id);
                foreach (int id in Tags(item["cross_check_assertions"])) relevant.Add(id);
            }
            var voucherChecks = await ReadDefinitionsAsync(db, tenant.Value, "voucher-checks", inner);
            foreach (var check in voucherChecks) foreach (int id in ReadIds(check["assertion_ids"]) ?? []) relevant.Add(id);
            foreach (int id in Tags(categoryDefinition["document_assertions"])) covered.Add(id);
            foreach (int id in Tags(categoryDefinition["cross_check_assertions"])) covered.Add(id);
            foreach (var check in voucherChecks.Where(check => ReadBoolean(check["is_active"])))
                foreach (int id in ReadIds(check["assertion_ids"]) ?? []) covered.Add(id);
            var gaps = await ReadDefinitionsAsync(db, tenant.Value, "assertions", inner);
            var values = gaps.Where(assertion => ReadBoolean(assertion["is_active"]) && relevant.Contains((int)assertion["id"]!)
                    && !covered.Contains((int)assertion["id"]!))
                .OrderBy(assertion => assertion["sort_order"]?.GetValue<int>() ?? 0)
                .Select(assertion => new { id = (int)assertion["id"]!, name = ReadName(assertion) }).ToArray();
            return Success(values, await CurrentVersionAsync(db, tenant.Value, inner));
        }, token);
    }, ct);

    private static IEnumerable<int> Tags(JsonNode? node) => node is JsonObject map
        ? map.SelectMany(pair => ReadIds(pair.Value) ?? []) : [];

    private static async Task<List<JsonObject>> ReadDefinitionsAsync(PostgresTransactionContext db, int tenant, string kind, CancellationToken ct)
    {
        var result = new List<JsonObject>();
        await using var command = db.CreateCommand("SELECT id,definition::text,is_active FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind=$2");
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(kind);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            var definition = JsonNode.Parse(reader.GetString(1))!.AsObject(); definition["id"] = reader.GetInt32(0); definition["is_active"] = reader.GetBoolean(2); result.Add(definition);
        }
        return result;
    }

    private static async Task<bool> ValidateAssertionIdsAsync(PostgresTransactionContext db, int tenant, int[] ids, CancellationToken ct)
    {
        if (ids.Length == 0) return true;
        await using var command = db.CreateCommand("SELECT count(*) FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='assertions' AND id=ANY($2)");
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(ids);
        return Convert.ToInt32(await command.ExecuteScalarAsync(ct)) == ids.Length;
    }

    private static async Task<bool> SignatureSpecimenClientExistsAsync(PostgresTransactionContext db, int tenant,
        int clientId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT 1 FROM public.clients c JOIN public.platform_tenant_client_homes h ON h.client_id=c.id
            WHERE h.tenant_id=$1 AND c.id=$2
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(clientId);
        return await command.ExecuteScalarAsync(ct) is not null;
    }

    private static async Task<bool> ValidateSignatureSpecimenReferencesAsync(PostgresTransactionContext db,
        int tenant, SignatureSpecimenInput input, CancellationToken ct)
    {
        await using (var client = db.CreateCommand("""
            SELECT 1 FROM public.clients c JOIN public.platform_tenant_client_homes h ON h.client_id=c.id
            WHERE h.tenant_id=$1 AND c.id=$2 AND c.is_active IS TRUE FOR SHARE OF c,h
            """))
        {
            client.Parameters.AddWithValue(tenant); client.Parameters.AddWithValue(input.ClientId);
            if (await client.ExecuteScalarAsync(ct) is null) return false;
        }
        await using var document = db.CreateCommand("""
            SELECT definition::text FROM public.platform_audit_rule_entities
            WHERE tenant_id=$1 AND entity_kind='documents' AND id=$2 FOR SHARE
            """);
        document.Parameters.AddWithValue(tenant); document.Parameters.AddWithValue(input.DocumentId);
        if (await document.ExecuteScalarAsync(ct) is not string raw) return false;
        var definition = JsonNode.Parse(raw)?.AsObject();
        return (ReadStrings(definition?["auth_roles"]) ?? []).Contains(input.RoleName, StringComparer.Ordinal);
    }

    private static async Task<bool> SignatureSpecimenDuplicateAsync(PostgresTransactionContext db, int tenant,
        SignatureSpecimenInput input, int? excludeId, CancellationToken ct)
    {
        string exclusion = excludeId is null ? string.Empty : " AND id<>$6";
        await using var command = db.CreateCommand($"""
            SELECT 1 FROM public.platform_audit_rule_signature_specimens
            WHERE tenant_id=$1 AND client_id=$2 AND document_id=$3 AND role_name=$4
              AND is_active IS TRUE AND lower(trim(expected_signatory_name))=lower(trim($5)){exclusion}
            LIMIT 1
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(input.ClientId);
        command.Parameters.AddWithValue(input.DocumentId); command.Parameters.AddWithValue(input.RoleName);
        command.Parameters.AddWithValue(input.ExpectedSignatoryName);
        if (excludeId is not null) command.Parameters.AddWithValue(excludeId.Value);
        return await command.ExecuteScalarAsync(ct) is not null;
    }

    private static void AddSignatureSpecimenInput(NpgsqlCommand command, int tenant, SignatureSpecimenInput input)
    {
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(input.ClientId);
        command.Parameters.AddWithValue(input.DocumentId); command.Parameters.AddWithValue(input.RoleName);
        command.Parameters.AddWithValue(input.ExpectedSignatoryName);
        command.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)input.Authority ?? DBNull.Value);
    }

    private static object ReadSignatureSpecimen(NpgsqlDataReader reader) => new Dictionary<string, object?>
    {
        ["id"] = reader.GetInt32(0), ["client_id"] = reader.GetInt32(1), ["document_id"] = reader.GetInt32(2),
        ["document_name"] = reader.IsDBNull(3) ? null : reader.GetString(3), ["role_name"] = reader.GetString(4),
        ["expected_signatory_name"] = reader.GetString(5), ["authority"] = reader.IsDBNull(6) ? null : reader.GetString(6),
        ["is_active"] = reader.GetBoolean(7), ["created_at"] = reader.GetDateTime(8), ["updated_at"] = reader.GetDateTime(9)
    };

    private async Task<int?> AuthorizeAsync(PostgresTransactionContext db, VerifiedSession? session, bool write, CancellationToken ct)
    {
        if (session is null || session.UserId <= 0 || !session.TenantContextRequired || session.TenantId is not > 0
            || !AuthenticationService.ValidToken(session.Token)) return null;
        string tenantLock = write ? "FOR SHARE OF s,u,m FOR UPDATE OF t" : "FOR SHARE OF s,u,m,t";
        await using var command = db.CreateCommand($"""
            SELECT t.id FROM public.sessions s JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id
            JOIN public.platform_tenants t ON t.id=m.tenant_id
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
              AND m.tenant_id=$3 AND m.is_active IS TRUE AND m.tenant_role='admin'
              AND t.is_active IS TRUE AND s.selected_tenant_id=$3 AND s.context_version=$4
            {tenantLock}
            """);
        command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
        command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion);
        return await command.ExecuteScalarAsync(ct) is int id ? id : null;
    }

    private static async Task LockTenantRulesAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT pg_advisory_xact_lock(1095910220,$1)");
        command.Parameters.AddWithValue(tenant); await command.ExecuteNonQueryAsync(ct);
    }

    private static async Task<bool> HasActiveVerificationAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT EXISTS(
                SELECT 1 FROM public.verification_presence p
                JOIN public.verification_sessions v ON v.id=p.verification_session_id
                JOIN public.platform_tenant_client_homes h ON h.client_id=v.client_id
                WHERE h.tenant_id=$1 AND p.ended_at IS NULL
                  AND p.last_heartbeat>now()-interval '90 seconds')
            """);
        command.Parameters.AddWithValue(tenant);
        return (bool)(await command.ExecuteScalarAsync(ct))!;
    }

    internal static async Task<bool> ValidateReferencesAsync(PostgresTransactionContext db, int tenant, string kind,
        JsonObject definition, CancellationToken ct)
    {
        if (kind == "documents" && !await ValidateDocumentDefinitionAsync(db, tenant, definition, ct))
            return false;
        var references = new List<(string Kind, int[] Ids)>();
        if (kind == "cross-checks")
        {
            var ids = ReadIds(definition["document_ids"]);
            if (ids is null && definition["documents"] is JsonArray documents)
                ids = ReadObjectIds(documents, "document_id");
            if (ids is null) return false;
            if (ids.Length == 0 && ReadBoolean(definition["compare_to_voucher_line"])) return false;
            references.Add(("documents", ids));
        }
        else if (kind == "categories")
        {
            var documents = ReadIds(definition["document_ids"]);
            var checks = ReadIds(definition["cross_check_ids"]);
            var assertions = ReadIds(definition["assertion_ids"]);
            if (documents is null || checks is null || assertions is null) return false;
            references.Add(("documents", documents)); references.Add(("cross-checks", checks));
            references.Add(("assertions", assertions));
        }
        foreach (var reference in references)
        {
            if (reference.Ids.Length == 0) continue;
            await using var command = db.CreateCommand("""
                SELECT count(*) FROM public.platform_audit_rule_entities
                WHERE tenant_id=$1 AND entity_kind=$2 AND id=ANY($3) AND is_active IS TRUE
                """);
            command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(reference.Kind);
            command.Parameters.AddWithValue(reference.Ids);
            if (Convert.ToInt32(await command.ExecuteScalarAsync(ct)) != reference.Ids.Length) return false;
        }
        if (kind == "cross-checks")
        {
            var documentIds = ReadIds(definition["document_ids"])
                ?? (definition["documents"] is JsonArray docs ? ReadObjectIds(docs, "document_id") : null);
            if (documentIds is null) return false;
            var perDocument = definition["document_fields"] as JsonObject ?? new JsonObject();
            var matrix = definition["matrix"] as JsonObject;
            // Populated matrices select fields in their cells; their flat
            // compare_field can be the legacy '(matrix)' placeholder. Keep
            // flat-field checks for non-matrix definitions and empty drafts.
            if (matrix?["fields"] is not JsonArray { Count: > 0 })
            foreach (int documentId in documentIds)
            {
                string? field = ReadString(perDocument[documentId.ToString(System.Globalization.CultureInfo.InvariantCulture)])
                    ?? ReadString(definition["compare_field"]);
                if (field is null || !await DocumentFieldExistsAsync(db, tenant, documentId, field, ct)) return false;
            }
            if (matrix is not null && !await ValidateMatrixDefinitionAsync(db, tenant, definition, matrix, ct)) return false;
        }
        return true;
    }

    private static async Task AssignFormFieldIdsAsync(PostgresTransactionContext db, JsonArray fields,
        JsonArray? priorFields, CancellationToken ct)
    {
        var priorIds = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        if (priorFields is not null)
            foreach (var prior in priorFields.OfType<JsonObject>())
                if (ReadString(prior["field_name"]) is { } name && prior["id"] is JsonValue value
                    && value.TryGetValue<int>(out int id) && id > 0) priorIds[name] = id;
        foreach (var field in fields.OfType<JsonObject>())
        {
            if (ReadString(field["field_name"]) is not { } name) continue;
            if (priorIds.TryGetValue(name, out int existingId)) field["id"] = existingId;
            else
            {
                await using var command = db.CreateCommand("SELECT nextval('public.platform_audit_rule_form_field_id_seq')");
                field["id"] = Convert.ToInt32(await command.ExecuteScalarAsync(ct));
            }
        }
    }

    private static async Task<bool> ValidateMatrixDefinitionAsync(PostgresTransactionContext db, int tenant,
        JsonObject definition, JsonObject matrix, CancellationToken ct)
    {
        int[] documentIds = ReadIds(definition["document_ids"]) ?? [];
        if (matrix["documents"] is not JsonArray documents || matrix["fields"] is not JsonArray fields) return false;
        var matrixDocumentIds = ReadObjectIds(documents, "document_id");
        if (matrixDocumentIds is null || !matrixDocumentIds.SequenceEqual(documentIds)) return false;
        var fieldIds = new HashSet<int>();
        foreach (var node in fields)
        {
            if (node is not JsonObject field || field["id"] is not JsonValue idValue || !idValue.TryGetValue<int>(out int id)
                || id <= 0 || !fieldIds.Add(id)) return false;
            string op = ReadString(field["operator"]) ?? "=";
            string missing = ReadString(field["missing_data_behavior"]) ?? "not_configured";
            string risk = ReadString(field["risk"]) ?? "Medium";
            double tolerance = field["tolerance"] is JsonValue amount && amount.TryGetValue<double>(out double parsed) ? parsed : 0;
            if (!new[] { "=", ">=", "<=", ">", "<", "!=" }.Contains(op, StringComparer.Ordinal)
                || !double.IsFinite(tolerance) || tolerance < 0
                || !new[] { "not_configured", "not_linked", "missing", "shortfall" }.Contains(missing, StringComparer.Ordinal)
                || !new[] { "Low", "Medium", "High" }.Contains(risk, StringComparer.Ordinal)) return false;
            if (field["cells"] is not JsonObject cells) return false;
            foreach (var cell in cells)
            {
                if (!int.TryParse(cell.Key, out int documentId) || !documentIds.Contains(documentId)) return false;
                var column = ReadString(cell.Value);
                if (column is null || !await DocumentFieldExistsAsync(db, tenant, documentId, column, ct)) return false;
            }
        }
        int currentCrossCheckId = matrix["cross_check_id"] is JsonValue currentIdValue
            && currentIdValue.TryGetValue<int>(out int parsedId) ? parsedId : 0;
        await using var otherChecks = db.CreateCommand("SELECT id,definition::text FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='cross-checks' AND id<>$2");
        otherChecks.Parameters.AddWithValue(tenant); otherChecks.Parameters.AddWithValue(currentCrossCheckId);
        await using var reader = await otherChecks.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            var other = JsonNode.Parse(reader.GetString(1))?.AsObject();
            if (other?["matrix"]?["fields"] is JsonArray otherFields
                && otherFields.OfType<JsonObject>().Any(field => field["id"] is JsonValue value
                    && value.TryGetValue<int>(out int existingId) && fieldIds.Contains(existingId))) return false;
        }
        return true;
    }

    private static async Task<bool> ValidateDocumentDefinitionAsync(PostgresTransactionContext db, int tenant,
        JsonObject definition, CancellationToken ct)
    {
        string type = ReadString(definition["document_type"]) ?? "linked_template";
        int? templateId = definition["template_id"] is JsonValue value && value.TryGetValue<int>(out int id) ? id : null;
        if (type == "physical_bill" && templateId is not null) return false;
        if (type == "form" && !ValidateFormFields(definition["form_fields"] as JsonArray ?? [])) return false;
        if (type == "physical_bill" && definition["form_fields"] is JsonArray { Count: > 0 }) return false;

        TemplateDefinition? template = null;
        if (templateId is int selectedTemplate)
        {
            template = (await TenantTemplateService.LoadAsync(db, tenant, selectedTemplate, ct)).SingleOrDefault();
            if (template is null || template.Origin != "native") return false;
        }
        if (!NormalizeVerificationFieldPolicy(definition, template)) return false;

        var fields = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (type == "form")
        {
            foreach (var field in definition["form_fields"]!.AsArray())
            {
                if (field is not JsonObject formField || ReadString(formField["field_name"]) is not { } fieldName) continue;
                fields.Add(fieldName);
                var linkedColumn = ReadString(formField["linked_column"]);
                if (linkedColumn is not null && (template is null || template.Columns.All(column => !column.Active ||
                        !string.Equals(column.Name, linkedColumn, StringComparison.OrdinalIgnoreCase)
                        && !string.Equals(column.StorageKey, linkedColumn, StringComparison.OrdinalIgnoreCase)))) return false;
            }
        }
        else if (type == "physical_bill")
            fields.UnionWith(["Bill No", "Bill Date", "Vendor Name", "Quantity", "Rate", "Amount"]);
        else if (template is not null)
        {
            fields.UnionWith(template.Columns.Where(column => column.Active).Select(column => column.Name));
            fields.UnionWith(template.Columns.Where(column => column.Active).Select(column => column.StorageKey));
            if (definition["verification_field_policy"] is JsonObject policy
                && policy["selected_column_ids"] is JsonArray selected)
            {
                var ids = selected.Select(node => node is JsonValue scalar && scalar.TryGetValue<int>(out int columnId) ? columnId : -1).ToArray();
                if (ids.Any(columnId => columnId <= 0 || template.Columns.All(column => !column.Active || column.Id != columnId))
                    || ids.Distinct().Count() != ids.Length) return false;
            }
        }

        if (definition["cross_check_fields"] is JsonArray configuredFields)
            foreach (var node in configuredFields)
                if (ReadString(node) is not { } field || (fields.Count > 0 && !fields.Contains(field))) return false;
        return true;
    }

    internal static bool NormalizeVerificationFieldPolicy(JsonObject definition, TemplateDefinition? template)
    {
        if (definition["verification_field_policy"] is null) return true;
        if (definition["verification_field_policy"] is not JsonObject policy || template is null) return false;
        var activeColumns = template.Columns.Where(column => column.Active).ToArray();
        var allowedCore = new HashSet<string>(["vendor", "item", "unit", "quantity", "rate", "amount"], StringComparer.Ordinal);
        var coreSource = policy["core"] as JsonObject ?? new JsonObject();
        var core = new JsonObject(); var coreIds = new HashSet<int>();
        foreach (var pair in coreSource)
        {
            if (!allowedCore.Contains(pair.Key)) return false;
            if (pair.Value is null || pair.Value.ToJsonString() == "\"\"") continue;
            if (pair.Value is not JsonValue scalar || !scalar.TryGetValue<int>(out int id)
                || activeColumns.All(column => column.Id != id) || !coreIds.Add(id)) return false;
            core[pair.Key] = id;
        }
        if (policy["optional"] is not null && policy["optional"] is not JsonArray) return false;
        var optional = new JsonArray(); var used = new HashSet<int>(coreIds); var selected = coreIds.ToList();
        foreach (var node in (policy["optional"] as JsonArray) ?? new JsonArray())
        {
            if (node is not JsonObject entry || entry["column_id"] is not JsonValue columnValue
                || !columnValue.TryGetValue<int>(out int id) || activeColumns.All(column => column.Id != id) || !used.Add(id)) return false;
            string risk = ReadString(entry["risk_level"]) ?? "Medium";
            int order = entry["sort_order"] is JsonValue orderValue && orderValue.TryGetValue<int>(out int parsedOrder) ? parsedOrder : optional.Count;
            if (order < 0 || !new[] { "Low", "Medium", "High" }.Contains(risk, StringComparer.Ordinal)) return false;
            optional.Add(new JsonObject { ["column_id"] = id, ["risk_level"] = risk, ["sort_order"] = order });
        }
        var orderedOptional = optional.OfType<JsonObject>().OrderBy(item => item["sort_order"]!.GetValue<int>())
            .ThenBy(item => item["column_id"]!.GetValue<int>()).ToArray();
        optional = new JsonArray(orderedOptional.Select((entry, index) =>
        {
            selected.Add(entry["column_id"]!.GetValue<int>());
            return new JsonObject { ["column_id"] = entry["column_id"]!.GetValue<int>(),
                ["risk_level"] = ReadString(entry["risk_level"]), ["sort_order"] = index };
        }).ToArray());
        JsonArray brief;
        if (policy["brief_column_ids"] is JsonArray providedBrief)
        {
            var ids = ReadIds(providedBrief);
            if (ids is null || ids.Length > 5 || ids.Any(id => !selected.Contains(id))) return false;
            brief = JsonSerializer.SerializeToNode(ids, JsonOptions)!.AsArray();
        }
        else brief = JsonSerializer.SerializeToNode(selected.Take(5).ToArray(), JsonOptions)!.AsArray();
        definition["verification_field_policy"] = new JsonObject
        { ["version"] = 1, ["core"] = core, ["optional"] = optional, ["brief_column_ids"] = brief };
        return true;
    }

    internal static bool ValidateFormFields(JsonArray fields)
    {
        var names = new Dictionary<string, (string Type, string Scope, string[] Operands)>(StringComparer.OrdinalIgnoreCase);
        foreach (var node in fields)
        {
            if (node is not JsonObject field || ReadString(field["field_name"]) is not { } name
                || ReadString(field["field_type"]) is not { } type || !AuditRuleKinds.FormFieldTypes.Contains(type, StringComparer.Ordinal)
                || ReadString(field["field_scope"]) is not { } scope || !AuditRuleKinds.FormFieldScopes.Contains(scope, StringComparer.Ordinal)
                || names.ContainsKey(name)) return false;
            string[] operands = field["arithmetic_operands"] is JsonArray array
                ? array.Select(ReadString).Where(item => item is not null).Cast<string>().ToArray() : [];
            if (field["arithmetic_operands"] is not null && field["arithmetic_operands"] is not JsonArray) return false;
            if (type == "arithmetic_multiply" && (operands.Length != 2 || scope != "item" || field["linked_column"] is JsonValue)) return false;
            if (type != "arithmetic_multiply" && operands.Length > 0) return false;
            names[name] = (type, scope, operands);
        }
        foreach (var pair in names)
        {
            var (type, scope, operands) = pair.Value;
            if (type != "arithmetic_multiply") continue;
            if (operands.Distinct(StringComparer.OrdinalIgnoreCase).Count() != 2 || operands.Any(operand =>
                    !names.TryGetValue(operand, out var source) || source.Scope != scope
                    || source.Type != "numeric")) return false;
        }
        return true;
    }

    private static bool NormalizeFormFields(JsonArray fields)
    {
        for (int index = 0; index < fields.Count; index++)
        {
            if (fields[index] is not JsonObject field) return false;
            if (ReadString(field["field_name"]) is null) field["field_name"] = ReadString(field["column_name"]);
            if (ReadString(field["field_name"]) is { } name) field["field_name"] = name;
            if (ReadString(field["field_type"]) is null) field["field_type"] = ReadString(field["column_type"]) ?? "text";
            if (ReadString(field["field_type"]) is { } fieldType) field["field_type"] = fieldType.ToLowerInvariant();
            if (ReadString(field["field_scope"]) is null) field["field_scope"] = "header";
            if (ReadString(field["field_scope"]) is { } scope) field["field_scope"] = scope.ToLowerInvariant();
            field["linked_column"] = ReadString(field["linked_column"]);
            if (field["arithmetic_operands"] is null && field["calculation_operands"] is not null)
                field["arithmetic_operands"] = field["calculation_operands"]?.DeepClone();
            if (field["arithmetic_operands"] is JsonArray operands)
                field["arithmetic_operands"] = new JsonArray(operands.Select(ReadString).Where(value => value is not null)
                    .Select(value => JsonValue.Create(value!.Trim())).ToArray());
            else field["arithmetic_operands"] = new JsonArray();
            field.Remove("column_name"); field.Remove("column_type"); field.Remove("calculation_operands");
            field["sort_order"] = index;
        }
        return ValidateFormFields(fields);
    }

    private static async Task<bool> DocumentFieldExistsAsync(PostgresTransactionContext db, int tenant, int documentId,
        string field, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT definition::text FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='documents' AND id=$2 AND is_active IS TRUE");
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(documentId);
        if (await command.ExecuteScalarAsync(ct) is not string raw) return false;
        var definition = JsonNode.Parse(raw)?.AsObject();
        if (definition is null) return false;
        string type = ReadString(definition["document_type"]) ?? "linked_template";
        if (type == "form") return definition["form_fields"] is JsonArray fields
            && fields.Any(node => node is JsonObject item && string.Equals(ReadString(item["field_name"]), field, StringComparison.OrdinalIgnoreCase));
        if (type == "physical_bill") return new[] { "Bill No", "Bill Date", "Vendor Name", "Quantity", "Rate", "Amount" }
            .Contains(field, StringComparer.OrdinalIgnoreCase);
        if (definition["template_id"] is JsonValue templateValue && templateValue.TryGetValue<int>(out int templateId))
        {
            var template = (await TenantTemplateService.LoadAsync(db, tenant, templateId, ct)).SingleOrDefault();
            return template is not null && template.Columns.Any(column => column.Active
                && (string.Equals(column.Name, field, StringComparison.OrdinalIgnoreCase)
                    || string.Equals(column.StorageKey, field, StringComparison.OrdinalIgnoreCase)));
        }
        await using var master = db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.master_columns m JOIN public.platform_tenant_columns c ON c.column_id=m.id WHERE c.tenant_id=$1 AND c.binding_origin='native' AND m.is_active IS TRUE AND m.column_name=$2)");
        master.Parameters.AddWithValue(tenant); master.Parameters.AddWithValue(field);
        return (bool)(await master.ExecuteScalarAsync(ct))!;
    }

    private static int[]? ReadIds(JsonNode? value)
    {
        if (value is null) return [];
        if (value is not JsonArray array) return null;
        var ids = new List<int>(array.Count);
        foreach (var node in array)
        {
            if (node is not JsonValue json || !json.TryGetValue<int>(out var id) || id <= 0) return null;
            ids.Add(id);
        }
        return ids.Count == ids.Distinct().Count() ? ids.ToArray() : null;
    }

    private static int[]? ReadObjectIds(JsonArray array, string key)
    {
        var ids = new List<int>(array.Count);
        foreach (var node in array)
        {
            if (node is not JsonObject obj || obj[key] is not JsonValue json || !json.TryGetValue<int>(out var id) || id <= 0)
                return null;
            ids.Add(id);
        }
        return ids.Count == ids.Distinct().Count() ? ids.ToArray() : null;
    }

    private static bool ReadBoolean(JsonNode? value) => value is JsonValue json && json.TryGetValue<bool>(out var result) && result;

    private static bool AllowedProperty(string kind, string property) => kind switch
    {
        "documents" => property is "transaction_level_approval",
        "cross-checks" => property is "matrix" or "documents" or "fields",
        "categories" => property is "validation_rules_json" or "document_ids" or "cross_check_ids"
            or "document_assertions" or "cross_check_assertions",
        "voucher-checks" => property is "assertion_ids" or "sort_order",
        _ => false
    };

    internal static bool ValidateCategoryRules(JsonObject definition)
    {
        JsonNode? rules = definition["validation_rules_json"] ?? definition["validation_rules"];
        if (rules is null) return true;
        if (rules is not JsonObject ruleObject) return false;
        var allowedConditions = new HashSet<string>(["required_document", "prepared_by", "checked_by", "approved_by", "signature"], StringComparer.Ordinal);
        var allowedRisks = new HashSet<string>(["Low", "Medium", "High"], StringComparer.Ordinal);
        var allowedMissing = new HashSet<string>(["not_configured", "not_linked", "missing", "shortfall"], StringComparer.Ordinal);
        var documentIds = ReadIds(definition["document_ids"])?.ToHashSet() ?? [];
        if (ruleObject["procedure_evidence"] is not null && ruleObject["procedure_evidence"] is not JsonObject) return false;
        if (ruleObject["procedure_evidence"] is JsonObject procedure)
        {
            if (procedure["checks"] is not null && procedure["checks"] is not JsonArray) return false;
            if (procedure["checks"] is JsonArray checks) foreach (var node in checks)
            {
                if (node is not JsonObject check || !int.TryParse(ReadScalar(check["document_id"]), out int documentId)
                    || !documentIds.Contains(documentId)
                    || !allowedConditions.Contains(ReadString(check["condition"]) ?? "")
                    || !allowedRisks.Contains(ReadString(check["risk"]) ?? "")) return false;
                string condition = ReadString(check["condition"])!;
                string? detail = ReadString(check["sub_value"]) ?? ReadString(check["detail"]) ?? ReadString(check["selected_detail"]);
                if (condition != "required_document" && detail is null) return false;
            }
        }
        if (ruleObject["mathematical_cross_checks"] is not null && ruleObject["mathematical_cross_checks"] is not JsonArray) return false;
        if (ruleObject["mathematical_cross_checks"] is JsonArray comparisons)
        {
            foreach (var node in comparisons)
            {
                if (node is not JsonObject comparison
                    || !int.TryParse(ReadScalar(comparison["left_document_id"]), out int leftId)
                    || !int.TryParse(ReadScalar(comparison["right_document_id"]), out int rightId)
                    || !documentIds.Contains(leftId) || !documentIds.Contains(rightId)
                    || ReadString(comparison["left_field"]) is null || ReadString(comparison["right_field"]) is null
                    || ReadString(comparison["operator"]) is null) return false;
                var missing = ReadString(comparison["missing_data_behavior"]);
                if (missing is not null && !allowedMissing.Contains(missing)) return false;
            }
        }
        return true;
    }

    private static async Task<bool> ValidateCategoryAttachmentsAsync(PostgresTransactionContext db, int tenant,
        JsonObject definition, CancellationToken ct)
    {
        var documentIds = ReadIds(definition["document_ids"]);
        var crossCheckIds = ReadIds(definition["cross_check_ids"]);
        if (documentIds is null || crossCheckIds is null) return false;
        if (!ValidateIdMap(definition["document_required"], documentIds, requireBoolean: true, out _)
            || !ValidateIdMap(definition["document_assertions"], documentIds, requireBoolean: false, out var documentAssertions)
            || !ValidateIdMap(definition["cross_check_assertions"], crossCheckIds, requireBoolean: false, out var crossCheckAssertions)) return false;
        var assertions = documentAssertions.Concat(crossCheckAssertions).Distinct().ToArray();
        return await ValidateAssertionIdsAsync(db, tenant, assertions, ct);
    }

    private static bool ValidateIdMap(JsonNode? node, int[] allowedIds, bool requireBoolean, out int[] assertionIds)
    {
        assertionIds = [];
        if (node is null) return true;
        if (node is not JsonObject map) return false;
        var allowed = allowedIds.Select(id => id.ToString(System.Globalization.CultureInfo.InvariantCulture)).ToHashSet(StringComparer.Ordinal);
        var assertions = new List<int>();
        foreach (var pair in map)
        {
            if (!int.TryParse(pair.Key, out int id) || !allowed.Contains(pair.Key)) return false;
            if (requireBoolean)
            {
                if (pair.Value is not JsonValue value || !value.TryGetValue<bool>(out _)) return false;
            }
            else
            {
                var ids = ReadIds(pair.Value);
                if (ids is null) return false;
                assertions.AddRange(ids);
            }
        }
        assertionIds = assertions.Distinct().ToArray();
        return true;
    }

    private static string? ReadScalar(JsonNode? value) => value is JsonValue scalar
        ? scalar.ToString() : null;

    private static async Task EnsureStateAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("INSERT INTO public.platform_audit_rule_state(tenant_id) VALUES($1) ON CONFLICT(tenant_id) DO NOTHING");
        command.Parameters.AddWithValue(tenant); await command.ExecuteNonQueryAsync(ct);
    }

    internal static async Task EnsureCurrentSnapshotAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await EnsureStateAsync(db, tenant, ct);
        await EnsureSnapshotAsync(db, tenant, ct);
    }

    private async Task<long> PublishChangeAsync(PostgresTransactionContext db, int tenant, string kind, int id,
        string action, string? name, int actor, CancellationToken ct)
    {
        long version;
        await using (var update = db.CreateCommand("UPDATE public.platform_audit_rule_state SET config_version=config_version+1,updated_at=now() WHERE tenant_id=$1 RETURNING config_version"))
        { update.Parameters.AddWithValue(tenant); version = (long)(await update.ExecuteScalarAsync(ct))!; }
        await using (var insert = db.CreateCommand("""
            INSERT INTO public.platform_audit_rule_changes(tenant_id,config_version,entity_kind,entity_id,action,entity_name,actor_user_id)
            VALUES($1,$2,$3,$4,$5,$6,$7)
            """))
        {
            insert.Parameters.AddWithValue(tenant); insert.Parameters.AddWithValue(version); insert.Parameters.AddWithValue(kind);
            insert.Parameters.AddWithValue(id); insert.Parameters.AddWithValue(action); insert.Parameters.AddWithValue((object?)name ?? DBNull.Value);
            insert.Parameters.AddWithValue(actor); await insert.ExecuteNonQueryAsync(ct);
        }
        await PublishSnapshotsAsync(db, tenant, version, ct);
        return version;
    }

    private static async Task EnsureSnapshotAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        long version = await CurrentVersionAsync(db, tenant, ct);
        await using (var current = db.CreateCommand("""
            SELECT config_version,schema_version,evaluator_version,fingerprint,evaluation_fingerprint,payload::text
            FROM public.platform_audit_rule_snapshots WHERE tenant_id=$1 FOR UPDATE
            """))
        {
            current.Parameters.AddWithValue(tenant);
            await using var reader = await current.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct))
            {
                long storedVersion = reader.GetInt64(0);
                int storedSchema = reader.GetInt32(1), storedEvaluator = reader.GetInt32(2);
                string storedFingerprint = reader.GetString(3).Trim();
                string storedEvaluation = reader.GetString(4).Trim();
                var storedPayload = JsonDocument.Parse(reader.GetString(5)).RootElement.Clone();
                string actualFingerprint = Fingerprint(Canonical(storedPayload));
                if (storedVersion > version)
                    throw new InvalidOperationException("Refusing to replace a newer Audit Rules snapshot.");
                bool valid = storedVersion == version && storedSchema == SnapshotSchemaVersion
                    && storedEvaluator == EvaluatorVersion
                    && string.Equals(storedFingerprint, actualFingerprint, StringComparison.Ordinal);
                await reader.DisposeAsync();
                if (valid)
                {
                    var expectedPayload = await BuildSnapshotAsync(db, tenant, version, ct);
                    valid = string.Equals(storedFingerprint, Fingerprint(Canonical(expectedPayload)), StringComparison.Ordinal);
                    valid &= string.Equals(storedEvaluation, Fingerprint(BuildGlobalEvaluationContract(expectedPayload)), StringComparison.Ordinal);
                    if (valid && await CategorySnapshotsCurrentAsync(db, tenant, version, ct)) return;
                }
            }
        }
        await PublishSnapshotsAsync(db, tenant, version, ct);
    }

    private static async Task<bool> CategorySnapshotsCurrentAsync(PostgresTransactionContext db, int tenant, long version, CancellationToken ct)
    {
        var expected = await BuildCategorySnapshotsAsync(db, tenant, ct);
        await using var command = db.CreateCommand("""
            SELECT category_id,config_version,schema_version,evaluator_version,fingerprint,evaluation_fingerprint,payload::text
            FROM public.platform_audit_rule_category_snapshots WHERE tenant_id=$1
            """);
        command.Parameters.AddWithValue(tenant);
        var actual = new Dictionary<int, (long Version, int Schema, int Evaluator, string Fingerprint, string Evaluation, string Payload)>();
        await using (var reader = await command.ExecuteReaderAsync(ct))
            while (await reader.ReadAsync(ct)) actual.Add(reader.GetInt32(0), (reader.GetInt64(1), reader.GetInt32(2), reader.GetInt32(3),
                reader.GetString(4).Trim(), reader.GetString(5).Trim(), reader.GetString(6)));
        if (actual.Count != expected.Count) return false;
        foreach (var item in expected)
        {
            if (!actual.TryGetValue(item.Id, out var row) || row.Version != version
                || row.Schema != SnapshotSchemaVersion || row.Evaluator != EvaluatorVersion
                || row.Fingerprint != item.Fingerprint || row.Evaluation != item.EvaluationFingerprint
                || Fingerprint(Canonical(JsonDocument.Parse(row.Payload).RootElement)) != row.Fingerprint) return false;
        }
        return true;
    }

    internal static async Task PublishSnapshotsAsync(PostgresTransactionContext db, int tenant, long version, CancellationToken ct)
    {
        var payload = await BuildSnapshotAsync(db, tenant, version, ct);
        string canonical = Canonical(payload), fingerprint = Fingerprint(canonical);
        string evaluationFingerprint = Fingerprint(BuildGlobalEvaluationContract(payload));
        long globalPriorRevision = 0, priorVersion = 0; string? globalPriorFingerprint = null;
        await using (var prior = db.CreateCommand("SELECT fingerprint,revision,config_version FROM public.platform_audit_rule_snapshots WHERE tenant_id=$1 FOR UPDATE"))
        {
            prior.Parameters.AddWithValue(tenant); await using var reader = await prior.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct))
            {
                globalPriorFingerprint = reader.GetString(0).Trim(); globalPriorRevision = reader.GetInt64(1); priorVersion = reader.GetInt64(2);
                if (priorVersion > version) throw new InvalidOperationException("Refusing to replace a newer Audit Rules snapshot.");
            }
        }
        long globalRevision = globalPriorRevision == 0 ? 1 : globalPriorRevision + (globalPriorFingerprint == fingerprint ? 0 : 1);
        await using (var snapshot = db.CreateCommand("""
            INSERT INTO public.platform_audit_rule_snapshots
                (tenant_id,config_version,schema_version,evaluator_version,fingerprint,evaluation_fingerprint,revision,payload)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT(tenant_id) DO UPDATE SET config_version=EXCLUDED.config_version,
                schema_version=EXCLUDED.schema_version,evaluator_version=EXCLUDED.evaluator_version,
                fingerprint=EXCLUDED.fingerprint,evaluation_fingerprint=EXCLUDED.evaluation_fingerprint,
                revision=EXCLUDED.revision,payload=EXCLUDED.payload,published_at=now()
            WHERE public.platform_audit_rule_snapshots.config_version <= EXCLUDED.config_version
            """))
        {
            snapshot.Parameters.AddWithValue(tenant); snapshot.Parameters.AddWithValue(version);
            snapshot.Parameters.AddWithValue(SnapshotSchemaVersion); snapshot.Parameters.AddWithValue(EvaluatorVersion);
            snapshot.Parameters.AddWithValue(fingerprint); snapshot.Parameters.AddWithValue(evaluationFingerprint);
            snapshot.Parameters.AddWithValue(globalRevision);
            snapshot.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = canonical });
            if (await snapshot.ExecuteNonQueryAsync(ct) != 1)
                throw new InvalidOperationException("Refusing to replace a newer Audit Rules snapshot.");
        }
        foreach (var category in await BuildCategorySnapshotsAsync(db, tenant, ct))
        {
            long categoryPriorRevision = 0; string? categoryPriorFingerprint = null;
            await using (var existing = db.CreateCommand("""
                SELECT fingerprint,revision,config_version FROM public.platform_audit_rule_category_snapshots
                WHERE tenant_id=$1 AND category_id=$2 FOR UPDATE
                """))
            {
                existing.Parameters.AddWithValue(tenant); existing.Parameters.AddWithValue(category.Id);
                await using var reader = await existing.ExecuteReaderAsync(ct);
                if (await reader.ReadAsync(ct))
                {
                    categoryPriorFingerprint = reader.GetString(0).Trim(); categoryPriorRevision = reader.GetInt64(1);
                    if (reader.GetInt64(2) > version)
                        throw new InvalidOperationException("Refusing to replace a newer Category snapshot.");
                }
            }
            long categoryRevision = categoryPriorRevision == 0 ? 1 : categoryPriorRevision + (categoryPriorFingerprint == category.Fingerprint ? 0 : 1);
            await using var upsert = db.CreateCommand("""
                INSERT INTO public.platform_audit_rule_category_snapshots
                    (tenant_id,category_id,config_version,schema_version,evaluator_version,fingerprint,evaluation_fingerprint,revision,is_active,payload)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT(tenant_id,category_id) DO UPDATE SET config_version=EXCLUDED.config_version,
                    schema_version=EXCLUDED.schema_version,evaluator_version=EXCLUDED.evaluator_version,
                    fingerprint=EXCLUDED.fingerprint,evaluation_fingerprint=EXCLUDED.evaluation_fingerprint,
                    revision=EXCLUDED.revision,is_active=EXCLUDED.is_active,payload=EXCLUDED.payload,published_at=now()
                WHERE public.platform_audit_rule_category_snapshots.config_version <= EXCLUDED.config_version
                """);
            upsert.Parameters.AddWithValue(tenant); upsert.Parameters.AddWithValue(category.Id); upsert.Parameters.AddWithValue(version);
            upsert.Parameters.AddWithValue(SnapshotSchemaVersion); upsert.Parameters.AddWithValue(EvaluatorVersion);
            upsert.Parameters.AddWithValue(category.Fingerprint); upsert.Parameters.AddWithValue(category.EvaluationFingerprint);
            upsert.Parameters.AddWithValue(categoryRevision); upsert.Parameters.AddWithValue(category.IsActive);
            upsert.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Jsonb, Value = category.CanonicalPayload });
            if (await upsert.ExecuteNonQueryAsync(ct) != 1)
                throw new InvalidOperationException("Refusing to replace a newer Category snapshot.");
        }
    }

    private static async Task<long> CurrentVersionAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT config_version FROM public.platform_audit_rule_state WHERE tenant_id=$1");
        command.Parameters.AddWithValue(tenant); return (long)(await command.ExecuteScalarAsync(ct) ?? 1L);
    }

    private static async Task<JsonElement> BuildSnapshotAsync(PostgresTransactionContext db, int tenant, long version, CancellationToken ct)
    {
        var assertions = new List<JsonElement>(); var voucherChecks = new List<JsonElement>();
        await using var command = db.CreateCommand("""
            SELECT id,entity_kind,definition::text,is_active,revision,updated_at
            FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND is_active IS TRUE
                AND entity_kind IN ('assertions','voucher-checks')
            ORDER BY entity_kind,
                CASE WHEN definition->>'sort_order' ~ '^[0-9]{1,9}$' THEN (definition->>'sort_order')::integer ELSE id END,id
            """);
        command.Parameters.AddWithValue(tenant);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            var definition = JsonNode.Parse(reader.GetString(2))!.AsObject();
            int id = reader.GetInt32(0);
            JsonObject item = reader.GetString(1) == "assertions"
                ? new JsonObject { ["id"] = id, ["name"] = ReadString(definition["name"]),
                    ["description"] = definition["description"]?.DeepClone(),
                    ["sort_order"] = definition["sort_order"]?.DeepClone() ?? JsonValue.Create(0) }
                : new JsonObject { ["id"] = id, ["name"] = ReadString(definition["name"]),
                    ["authority"] = definition["authority"]?.DeepClone(),
                    ["sort_order"] = definition["sort_order"]?.DeepClone() ?? JsonValue.Create(0),
                    ["assertion_ids"] = definition["assertion_ids"]?.DeepClone() ?? new JsonArray() };
            var element = JsonDocument.Parse(item.ToJsonString(JsonOptions)).RootElement.Clone();
            if (reader.GetString(1) == "assertions") assertions.Add(element); else voucherChecks.Add(element);
        }
        // The Category compiler borrows this same connection. End the global
        // reader before it starts its queries, including when no rows remain.
        await reader.DisposeAsync();
        var activeAssertionIds = assertions.Select(assertion => assertion.GetProperty("id").GetInt32()).ToHashSet();
        for (int index = 0; index < voucherChecks.Count; index++)
        {
            var check = voucherChecks[index];
            var normalized = JsonNode.Parse(check.GetRawText())!.AsObject();
            normalized["assertion_ids"] = NormalizeAssertionIds(normalized["assertion_ids"], activeAssertionIds);
            voucherChecks[index] = JsonDocument.Parse(normalized.ToJsonString(JsonOptions)).RootElement.Clone();
        }
        var categories = (await BuildCategorySnapshotsAsync(db, tenant, ct))
            .Select(item => JsonDocument.Parse(item.CanonicalPayload).RootElement.Clone()).ToArray();
        var payload = JsonSerializer.SerializeToElement(new
        {
            schema_version = SnapshotSchemaVersion, evaluator_version = EvaluatorVersion,
            assertions, voucher_checks = voucherChecks, categories
        }, JsonOptions);
        return payload;
    }

    private sealed record CategorySnapshotBuild(int Id, bool IsActive, string Fingerprint,
        string EvaluationFingerprint, string CanonicalPayload);

    private static async Task<List<CategorySnapshotBuild>> BuildCategorySnapshotsAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        var entities = AuditRuleKinds.All.ToDictionary(kind => kind, _ => new List<JsonObject>(), StringComparer.Ordinal);
        await using (var command = db.CreateCommand("""
            SELECT id,entity_kind,definition::text,is_active,revision,updated_at
            FROM public.platform_audit_rule_entities WHERE tenant_id=$1 ORDER BY entity_kind,id
            """))
        {
            command.Parameters.AddWithValue(tenant);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                var definition = JsonNode.Parse(reader.GetString(2))!.AsObject();
                definition["id"] = reader.GetInt32(0); definition["is_active"] = reader.GetBoolean(3);
                definition["revision"] = reader.GetInt64(4); definition["updated_at"] = reader.GetDateTime(5);
                entities[reader.GetString(1)].Add(definition);
            }
        }
        var documents = entities["documents"].ToDictionary(entity => (int)entity["id"]!, entity => entity);
        var crossChecks = entities["cross-checks"].ToDictionary(entity => (int)entity["id"]!, entity => entity);
        var activeAssertionIds = entities["assertions"].Where(entity => ReadBoolean(entity["is_active"]))
            .Select(entity => (int)entity["id"]!).ToHashSet();
        var templates = (await TenantTemplateService.LoadAsync(db, tenant, null, ct))
            .ToDictionary(template => template.Id);
        var masterFieldSpecs = new JsonArray();
        await using (var command = db.CreateCommand("""
            SELECT m.id,m.column_name FROM public.master_columns m
            JOIN public.platform_tenant_columns c ON c.column_id=m.id
            WHERE c.tenant_id=$1 AND c.binding_origin='native' AND m.is_active IS TRUE
            ORDER BY c.category,m.column_name
            """))
        {
            command.Parameters.AddWithValue(tenant);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct)) masterFieldSpecs.Add(new JsonObject
            { ["field_name"] = reader.GetString(1), ["master_column_id"] = reader.GetInt32(0) });
        }
        var snapshots = new List<CategorySnapshotBuild>();
        foreach (var category in entities["categories"])
        {
            int categoryId = (int)category["id"]!;
            var categoryDocuments = new JsonArray(); var categoryChecks = new JsonArray(); var warnings = new JsonArray();
            var required = category["document_required"] as JsonObject ?? new JsonObject();
            var documentTags = category["document_assertions"] as JsonObject ?? new JsonObject();
            var crossCheckTags = category["cross_check_assertions"] as JsonObject ?? new JsonObject();
            var attachedDocumentIds = (ReadIds(category["document_ids"]) ?? []).ToHashSet();
            var compiledDocuments = new Dictionary<int, JsonObject>();
            foreach (int documentId in ReadIds(category["document_ids"]) ?? [])
            {
                if (!documents.TryGetValue(documentId, out var source))
                {
                    warnings.Add(new JsonObject { ["code"] = "inactive_document_requirement", ["category_id"] = categoryId, ["document_id"] = documentId });
                    continue;
                }
                if (!ReadBoolean(source["is_active"]))
                {
                    warnings.Add(new JsonObject { ["code"] = "inactive_document_requirement", ["category_id"] = categoryId, ["document_id"] = documentId });
                    continue;
                }
                var copy = (JsonObject)source.DeepClone();
                copy.Remove("id"); copy.Remove("revision"); copy.Remove("updated_at");
                copy.Remove("auth_roles"); copy.Remove("auth_role_authorities");
                string key = documentId.ToString(System.Globalization.CultureInfo.InvariantCulture);
                copy["document_id"] = documentId;
                copy["required"] = required[key]?.GetValue<bool>() ?? true;
                copy["assertion_ids"] = NormalizeAssertionIds(documentTags[key], activeAssertionIds);
                string documentType = ReadString(source["document_type"]) ?? "linked_template";
                int? templateId = source["template_id"] is JsonValue templateValue && templateValue.TryGetValue<int>(out int parsedTemplateId)
                    ? parsedTemplateId : null;
                TemplateDefinition? template = templateId is int selectedTemplate ? templates.GetValueOrDefault(selectedTemplate) : null;
                var fieldSpecs = documentType == "physical_bill" ? new JsonArray()
                    : new JsonArray(masterFieldSpecs.Select(field => field?.DeepClone()).ToArray());
                copy["fields"] = fieldSpecs;
                copy["form_fields"] ??= new JsonArray();
                copy["auth_roles"] ??= new JsonArray();
                var authorityRoles = new JsonArray();
                var authorities = source["auth_role_authorities"] as JsonObject ?? new JsonObject();
                foreach (string role in ReadStrings(source["auth_roles"]).Order(StringComparer.Ordinal))
                    authorityRoles.Add(new JsonObject { ["role_name"] = role,
                        ["required_authority"] = authorities[role]?.DeepClone() });
                copy["authority_roles"] = authorityRoles;
                var signatureRequirements = new JsonArray();
                foreach (var role in authorityRoles.OfType<JsonObject>())
                    if (!string.IsNullOrWhiteSpace(ReadString(role["required_authority"])))
                        signatureRequirements.Add(new JsonObject { ["role_name"] = ReadString(role["role_name"]),
                            ["required_authority"] = role["required_authority"]?.DeepClone() });
                copy["signature_requirements"] = signatureRequirements;
                copy["template_mapping_column_ids"] = template is null ? new JsonArray()
                    : new JsonArray(template.Columns.Where(column => column.Active).Select(column => (JsonNode?)JsonValue.Create(column.Id)).ToArray());
                copy["template_columns"] = template is null ? new JsonArray()
                    : new JsonArray(template.Columns.Where(column => column.Active).Select(column => (JsonNode?)new JsonObject
                    {
                        ["column_id"] = column.Id, ["column_name"] = column.StorageKey, ["data_type"] = column.Type,
                        ["sort_order"] = column.Order
                    }).ToArray());
                copy["verification_fields"] = BuildSelectedVerificationFields(copy, template, brief: false);
                copy["verification_brief_fields"] = BuildSelectedVerificationFields(copy, template, brief: true);
                if (!ReadBoolean(source["is_active"]))
                    warnings.Add(new JsonObject { ["code"] = "inactive_document", ["category_id"] = categoryId, ["document_id"] = documentId });
                if (templateId is int inactiveTemplateId && template is null)
                    warnings.Add(new JsonObject { ["code"] = "inactive_template", ["document_id"] = documentId, ["template_id"] = inactiveTemplateId });
                compiledDocuments[documentId] = copy;
                categoryDocuments.Add(copy);
            }
            foreach (int crossCheckId in ReadIds(category["cross_check_ids"]) ?? [])
            {
                if (!crossChecks.TryGetValue(crossCheckId, out var source) || !ReadBoolean(source["is_active"])) continue;
                var copy = (JsonObject)source.DeepClone();
                copy.Remove("id"); copy.Remove("revision"); copy.Remove("updated_at");
                copy["cross_check_id"] = crossCheckId;
                copy.Remove("document_fields");
                var matrix = copy["matrix"] as JsonObject;
                if (matrix is not null) matrix["cross_check_id"] = crossCheckId;
                var chain = new JsonArray();
                var missing = new JsonArray(); var inactive = new JsonArray();
                if (matrix?["documents"] is JsonArray matrixDocuments)
                {
                    foreach (var node in matrixDocuments)
                    {
                        if (node is not JsonObject matrixDocument || matrixDocument["document_id"] is not JsonValue idNode
                            || !idNode.TryGetValue<int>(out int matrixDocumentId)) continue;
                        documents.TryGetValue(matrixDocumentId, out var matrixSource);
                        var chainDocument = new JsonObject
                        {
                            ["document_id"] = matrixDocumentId,
                            ["name"] = matrixSource is null ? null : ReadString(matrixSource["name"]),
                            ["document_type"] = matrixSource is null ? "linked_template" : ReadString(matrixSource["document_type"]) ?? "linked_template",
                            ["template_id"] = matrixSource?["template_id"]?.DeepClone(),
                            ["sort_order"] = chain.Count
                        };
                        if (matrixSource is not null)
                        {
                            var matrixType = ReadString(matrixSource["document_type"]) ?? "linked_template";
                            chainDocument["field_options"] = matrixType switch
                            {
                                "form" => CompiledFormFieldSpecs(matrixSource),
                                "physical_bill" => CompiledPhysicalBillFieldSpecs(),
                                _ => new JsonArray()
                            };
                        }
                        chain.Add(chainDocument);
                        if (!attachedDocumentIds.Contains(matrixDocumentId)) missing.Add(matrixDocumentId);
                        if (matrixSource is null || !ReadBoolean(matrixSource["is_active"])) inactive.Add(matrixDocumentId);
                    }
                }
                if (matrix is not null)
                {
                    matrix["documents"] = chain.DeepClone();
                    if (matrix["fields"] is JsonArray configuredFields)
                    {
                        var normalizedFields = new JsonArray();
                        int sortOrder = 0;
                        foreach (var configured in configuredFields.OfType<JsonObject>())
                        {
                            double tolerance = configured["tolerance"] is JsonValue amount
                                && amount.TryGetValue<double>(out double parsedTolerance) ? parsedTolerance : 0;
                            normalizedFields.Add(new JsonObject
                            {
                                ["id"] = configured["id"]?.DeepClone(),
                                ["sort_order"] = configured["sort_order"]?.DeepClone() ?? JsonValue.Create(sortOrder),
                                ["cells"] = configured["cells"]?.DeepClone() ?? new JsonObject(),
                                ["operator"] = ReadString(configured["operator"]) ?? "=",
                                ["tolerance"] = tolerance,
                                ["missing_data_behavior"] = ReadString(configured["missing_data_behavior"]) ?? "not_configured",
                                ["blocking"] = ReadBoolean(configured["blocking"]),
                                ["risk"] = ReadString(configured["risk"]) ?? "Medium"
                            });
                            sortOrder++;
                        }
                        matrix["fields"] = normalizedFields;
                    }
                }
                copy["document_ids"] = new JsonArray(chain.OfType<JsonObject>().Select(document =>
                    (JsonNode?)JsonValue.Create(document["document_id"]!.GetValue<int>())).ToArray());
                copy["document_chain"] = chain;
                if (missing.Count > 0 || inactive.Count > 0)
                {
                    warnings.Add(new JsonObject { ["code"] = "cross_check_dependency_invalid", ["category_id"] = categoryId,
                        ["cross_check_id"] = crossCheckId, ["missing_document_ids"] = missing, ["inactive_document_ids"] = inactive });
                    continue;
                }
                copy["assertion_ids"] = NormalizeAssertionIds(
                    crossCheckTags[crossCheckId.ToString(System.Globalization.CultureInfo.InvariantCulture)], activeAssertionIds);
                categoryChecks.Add(copy);
            }
            JsonNode validation = category["validation_rules_json"]?.DeepClone() ?? new JsonObject();
            var procedureEvidence = CompileProcedureEvidence(validation as JsonObject ?? new JsonObject(), compiledDocuments);
            var canonicalWarnings = warnings.OfType<JsonObject>().Select(warning => (warning, json: Canonical(
                    JsonDocument.Parse(warning.ToJsonString(JsonOptions)).RootElement)))
                .GroupBy(item => item.json, StringComparer.Ordinal).OrderBy(group => group.Key, StringComparer.Ordinal)
                .Select(group => group.First().warning).ToArray();
            var payload = JsonSerializer.SerializeToElement(new
            {
                schema_version = SnapshotSchemaVersion, evaluator_version = EvaluatorVersion,
                category_id = categoryId, name = ReadString(category["name"]),
                kb_overview = ReadString(category["kb_overview"]), evidence_type = ReadString(category["evidence_type"]),
                matrix_version = category["matrix_version"]?.GetValue<int>() ?? 1,
                is_active = ReadBoolean(category["is_active"]),
                documents = categoryDocuments, cross_checks = categoryChecks,
                validation_rules = validation, procedure_evidence = procedureEvidence,
                working_paper_gates = new { approval = new Dictionary<string, object?>(),
                    supporting_documents = new Dictionary<string, object?>(),
                    account_head_classification = new Dictionary<string, object?>(),
                    observation = new Dictionary<string, object?>() },
                warnings = canonicalWarnings
            }, JsonOptions);
            string canonical = Canonical(payload);
            snapshots.Add(new(categoryId, ReadBoolean(category["is_active"]), Fingerprint(canonical),
                Fingerprint(BuildEvaluationContract(payload)), canonical));
        }
        return snapshots;
    }

    private static JsonArray NormalizeAssertionIds(JsonNode? configured, IReadOnlySet<int>? allowedIds = null)
    {
        var result = new JsonArray();
        foreach (int id in (ReadIds(configured) ?? []).Where(id => allowedIds is null || allowedIds.Contains(id)).Distinct().Order()) result.Add(id);
        return result;
    }

    private static IEnumerable<string> ReadStrings(JsonNode? node) => node is JsonArray array
        ? array.Select(ReadString).Where(value => !string.IsNullOrWhiteSpace(value)).Cast<string>() : [];

    private static JsonArray CompiledFormFieldSpecs(JsonObject document)
    {
        var specs = new JsonArray();
        foreach (var field in (document["form_fields"] as JsonArray ?? []).OfType<JsonObject>())
            specs.Add(new JsonObject { ["field_name"] = ReadString(field["field_name"]),
                ["data_type"] = ReadString(field["field_type"]) ?? "text", ["linked_column"] = field["linked_column"]?.DeepClone(),
                ["arithmetic_operands"] = field["arithmetic_operands"]?.DeepClone() ?? new JsonArray(), ["source"] = "form" });
        return specs;
    }

    private static JsonArray CompiledPhysicalBillFieldSpecs()
    {
        var specs = new JsonArray();
        foreach (string fieldName in new[] { "Bill No", "Bill Date", "Vendor Name", "Quantity", "Rate", "Amount" })
            specs.Add(new JsonObject { ["field_name"] = fieldName,
                ["data_type"] = fieldName is "Quantity" or "Rate" or "Amount" ? "numeric" : "text",
                ["linked_column"] = null, ["source"] = "legacy_physical_bill" });
        return specs;
    }

    private static JsonArray BuildSelectedVerificationFields(JsonObject document, TemplateDefinition? template, bool brief)
    {
        var columns = template?.Columns.Where(column => column.Active).OrderBy(column => column.Order).ThenBy(column => column.Id).ToArray() ?? [];
        if (columns.Length == 0) return new JsonArray();
        var policy = document["verification_field_policy"] as JsonObject;
        var selections = new List<JsonObject>();
        if (policy is null)
        {
            selections.AddRange(columns.Select((column, index) => new JsonObject { ["column_id"] = column.Id,
                ["column_name"] = column.StorageKey, ["data_type"] = column.Type, ["selection"] = "default", ["sort_order"] = index }));
        }
        else
        {
            var core = policy["core"] as JsonObject ?? new JsonObject();
            foreach (var pair in core)
                if (pair.Value is JsonValue value && value.TryGetValue<int>(out int id)
                    && columns.FirstOrDefault(column => column.Id == id) is { } column)
                    selections.Add(new JsonObject { ["column_id"] = id, ["column_name"] = column.StorageKey,
                        ["data_type"] = column.Type, ["selection"] = "core", ["key"] = pair.Key, ["sort_order"] = selections.Count });
            foreach (var item in (policy["optional"] as JsonArray ?? []).OfType<JsonObject>()
                .OrderBy(item => item["sort_order"]?.GetValue<int>() ?? 0))
                if (item["column_id"] is JsonValue value && value.TryGetValue<int>(out int id)
                    && columns.FirstOrDefault(column => column.Id == id) is { } column)
                    selections.Add(new JsonObject { ["column_id"] = id, ["column_name"] = column.StorageKey,
                        ["data_type"] = column.Type, ["selection"] = "optional",
                        ["risk_level"] = ReadString(item["risk_level"]) ?? "Medium", ["sort_order"] = selections.Count });
        }
        IEnumerable<JsonObject> selected = selections;
        if (brief)
        {
            var briefIds = ReadIds(policy?["brief_column_ids"]);
            var allowed = (briefIds ?? selections.Take(5).Select(item => item["column_id"]!.GetValue<int>()).ToArray()).ToHashSet();
            selected = selections.Where(item => allowed.Contains(item["column_id"]!.GetValue<int>())).Take(5);
        }
        return new JsonArray(selected.Select(item => (JsonNode?)item.DeepClone()).ToArray());
    }

    private static JsonObject CompileProcedureEvidence(JsonObject validation, IReadOnlyDictionary<int, JsonObject> documents)
    {
        var rawEvidence = validation["procedure_evidence"] as JsonObject ?? new JsonObject();
        var result = (JsonObject)rawEvidence.DeepClone();
        var checks = new JsonArray();
        foreach (var raw in (rawEvidence["checks"] as JsonArray ?? []).OfType<JsonObject>())
        {
            var check = (JsonObject)raw.DeepClone();
            int? documentId = check["document_id"] is JsonValue idValue && idValue.TryGetValue<int>(out int id) ? id : null;
            string? roleName = new[] { "role", "role_name", "signature" }.Select(key => ReadString(check[key]))
                .FirstOrDefault(value => !string.IsNullOrWhiteSpace(value));
            string? selectedDetail = ReadString(check["sub_value"]) ?? ReadString(check["detail"]) ?? ReadString(check["selected_detail"]);
            if (documentId is int selectedId && documents.TryGetValue(selectedId, out var document))
            {
                var roles = document["authority_roles"] as JsonArray ?? new JsonArray();
                roleName ??= roles.OfType<JsonObject>().Select(role => ReadString(role["role_name"]))
                    .FirstOrDefault(name => string.Equals(name, selectedDetail, StringComparison.OrdinalIgnoreCase));
                check["document_id"] = selectedId;
                check["document_name"] = ReadString(document["name"]);
                check["document_type"] = ReadString(document["document_type"]) ?? "linked_template";
                check["template_id"] = document["template_id"]?.DeepClone();
                check["available_roles"] = roles.DeepClone();
                check["role_name"] = roleName;
                check["configured_signatories"] = new JsonArray();
            }
            else
            {
                check["document_id"] = documentId;
                check["document_name"] = null; check["document_type"] = null; check["template_id"] = null;
                check["available_roles"] = new JsonArray(); check["role_name"] = roleName;
                check["configured_signatories"] = new JsonArray();
            }
            check["selected_detail"] = selectedDetail;
            check["risk"] = ReadString(raw["risk"]) ?? ReadString(raw["risk_rating"]) ?? "Medium";
            check["requires_client_resolution"] = !string.IsNullOrWhiteSpace(selectedDetail) && string.IsNullOrWhiteSpace(roleName);
            check.Remove("configured_signatories");
            checks.Add(check);
        }
        result["checks"] = checks;
        return result;
    }

    private static string BuildEvaluationContract(JsonElement payload)
    {
        var documents = new JsonArray();
        foreach (var source in payload.GetProperty("documents").EnumerateArray())
        {
            var document = new JsonObject
            {
                ["document_id"] = source.GetProperty("document_id").GetInt32(),
                ["document_type"] = source.TryGetProperty("document_type", out var type) ? type.GetString() ?? "linked_template" : "linked_template",
                ["template_id"] = source.TryGetProperty("template_id", out var template) && template.ValueKind == JsonValueKind.Number ? template.GetInt32() : null
            };
            if (source.TryGetProperty("authority_roles", out var roles))
            {
                var roleNames = new JsonArray();
                foreach (var role in roles.EnumerateArray()
                    .Select(value => value.TryGetProperty("role_name", out var roleName) ? roleName.GetString() ?? "" : "")
                    .Order(StringComparer.Ordinal)) roleNames.Add(role);
                document["roles"] = roleNames;
            }
            if (source.TryGetProperty("form_fields", out var formFields))
            {
                var normalized = new JsonArray();
                foreach (var form in formFields.EnumerateArray())
                {
                    var field = new JsonObject();
                    foreach (string key in new[] { "id", "field_name", "field_type", "field_scope", "arithmetic_operands" })
                        if (form.TryGetProperty(key, out var value)) field[key] = JsonNode.Parse(value.GetRawText());
                    normalized.Add(field);
                }
                document["form_fields"] = new JsonArray(normalized.OrderBy(field => field?["id"]?.GetValue<int>() ?? 0)
                    .Select(field => field?.DeepClone()).ToArray());
            }
            documents.Add(document);
        }
        var checks = new JsonArray();
        foreach (var source in payload.GetProperty("cross_checks").EnumerateArray())
        {
            var check = new JsonObject
            {
                ["cross_check_id"] = source.GetProperty("cross_check_id").GetInt32(),
                ["compare_to_voucher_line"] = source.TryGetProperty("compare_to_voucher_line", out var voucher) && voucher.ValueKind == JsonValueKind.True
            };
            if (source.TryGetProperty("matrix", out var matrix))
            {
                var normalizedMatrix = new JsonObject();
                if (matrix.TryGetProperty("documents", out var matrixDocuments))
                {
                    var docs = new JsonArray();
                    foreach (var document in matrixDocuments.EnumerateArray())
                    {
                        var item = new JsonObject();
                        foreach (string key in new[] { "document_id", "document_type", "template_id" })
                            if (document.TryGetProperty(key, out var value)) item[key] = JsonNode.Parse(value.GetRawText());
                        docs.Add(item);
                    }
                    normalizedMatrix["documents"] = docs;
                }
                if (matrix.TryGetProperty("fields", out var matrixFields))
                {
                    var fields = new JsonArray();
                    foreach (var field in matrixFields.EnumerateArray())
                    {
                        var item = new JsonObject();
                        foreach (string key in new[] { "id", "operator", "tolerance", "missing_data_behavior", "blocking", "cells" })
                            if (field.TryGetProperty(key, out var value)) item[key] = JsonNode.Parse(value.GetRawText());
                        fields.Add(item);
                    }
                    normalizedMatrix["fields"] = fields;
                }
                check["matrix"] = normalizedMatrix;
            }
            checks.Add(check);
        }
        var procedures = new JsonArray();
        if (payload.TryGetProperty("procedure_evidence", out var procedureEvidence)
            && procedureEvidence.TryGetProperty("checks", out var configuredChecks))
            foreach (var source in configuredChecks.EnumerateArray())
            {
                var check = new JsonObject();
                foreach (string key in new[] { "document_id", "condition", "role", "role_name", "signature", "selected_detail", "requires_client_resolution" })
                    if (source.TryGetProperty(key, out var value)) check[key] = JsonNode.Parse(value.GetRawText());
                procedures.Add(check);
            }
        var orderedDocuments = new JsonArray(documents.OfType<JsonObject>()
            .OrderBy(document => document["document_id"]?.GetValue<int>() ?? 0).Select(document => document.DeepClone()).ToArray());
        var orderedChecks = new JsonArray(checks.OfType<JsonObject>()
            .OrderBy(check => check["cross_check_id"]?.GetValue<int>() ?? 0).Select(check => check.DeepClone()).ToArray());
        var orderedProcedures = new JsonArray(procedures.OfType<JsonObject>()
            .Select(check => (check, canonical: Canonical(JsonDocument.Parse(check.ToJsonString(JsonOptions)).RootElement)))
            .OrderBy(item => item.canonical, StringComparer.Ordinal).Select(item => item.check.DeepClone()).ToArray());
        var contract = JsonSerializer.SerializeToElement(new
        {
            schema_version = SnapshotSchemaVersion, evaluator_version = EvaluatorVersion,
            category_id = payload.GetProperty("category_id").GetInt32(),
            is_active = payload.GetProperty("is_active").GetBoolean(),
            documents = orderedDocuments, cross_checks = orderedChecks, procedures = orderedProcedures,
            warnings = payload.GetProperty("warnings")
        }, JsonOptions);
        return Canonical(contract);
    }

    private static string BuildGlobalEvaluationContract(JsonElement payload)
    {
        int[] voucherCheckIds = payload.GetProperty("voucher_checks").EnumerateArray()
            .Select(check => check.GetProperty("id").GetInt32()).Order().ToArray();
        var contract = JsonSerializer.SerializeToElement(new
        { evaluator_version = EvaluatorVersion, voucher_check_ids = voucherCheckIds }, JsonOptions);
        return Canonical(contract);
    }

    private static string Fingerprint(string canonical) => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)));

    private static string Canonical(JsonElement element) => Canonicalize(element);
    private static string Canonicalize(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Object => "{" + string.Join(",", value.EnumerateObject().OrderBy(p => p.Name, StringComparer.Ordinal)
            .Select(p => JsonSerializer.Serialize(p.Name, CanonicalJsonOptions) + ":" + Canonicalize(p.Value))) + "}",
        JsonValueKind.Array => "[" + string.Join(",", value.EnumerateArray().Select(Canonicalize)) + "]",
        JsonValueKind.String => JsonSerializer.Serialize(value.GetString(), CanonicalJsonOptions),
        JsonValueKind.Number => value.GetRawText(),
        JsonValueKind.True => "true", JsonValueKind.False => "false", JsonValueKind.Null => "null",
        _ => throw new InvalidOperationException("Rulebook snapshot contains an unsupported JSON value.")
    };

    private static readonly JsonSerializerOptions CanonicalJsonOptions = new()
    { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private async Task<JsonElement> EntityViewAsync(PostgresTransactionContext db, int tenant, AuditRuleEntity entity,
        bool listView, CancellationToken ct)
    {
        var definition = JsonNode.Parse(entity.Definition.GetRawText())!.AsObject();
        var result = JsonNode.Parse(EntityJson(entity.Id, entity.Kind, entity.Definition, entity.IsActive,
            entity.Revision, entity.UpdatedAt).GetRawText())!.AsObject();
        if (entity.Kind == "documents")
        {
            string type = ReadString(definition["document_type"]) ?? "linked_template";
            var specs = new JsonArray();
            if (type == "form" && definition["form_fields"] is JsonArray formFields)
            {
                foreach (var node in formFields)
                    if (node is JsonObject field) specs.Add(new JsonObject { ["field_name"] = ReadString(field["field_name"]),
                        ["data_type"] = ReadString(field["field_type"]) ?? "text", ["linked_column"] = field["linked_column"]?.DeepClone(),
                        ["arithmetic_operands"] = field["arithmetic_operands"]?.DeepClone(), ["source"] = "form" });
            }
            else if (type == "physical_bill")
                foreach (string name in new[] { "Bill No", "Bill Date", "Vendor Name", "Quantity", "Rate", "Amount" })
                    specs.Add(new JsonObject { ["field_name"] = name, ["data_type"] = name is "Quantity" or "Rate" or "Amount" ? "numeric" : "text", ["source"] = "legacy_physical_bill" });
            else
            {
                await using var fields = db.CreateCommand("""
                    SELECT m.column_name,m.data_type FROM public.master_columns m
                    JOIN public.platform_tenant_columns c ON c.column_id=m.id
                    WHERE c.tenant_id=$1 AND c.binding_origin='native' AND m.is_active IS TRUE ORDER BY c.category,m.column_name
                    """);
                fields.Parameters.AddWithValue(tenant); await using var reader = await fields.ExecuteReaderAsync(ct);
                while (await reader.ReadAsync(ct)) specs.Add(new JsonObject { ["field_name"] = reader.GetString(0),
                    ["data_type"] = reader.IsDBNull(1) ? "text" : reader.GetString(1), ["source"] = "master_column" });
            }
            result["document_type"] = type;
            result["cross_check_field_specs"] = specs.DeepClone();
            result["cross_check_fields"] = new JsonArray(specs.OfType<JsonObject>().Select(spec => JsonValue.Create(ReadString(spec["field_name"]))).ToArray());
            if (definition["form_fields"] is null) result["form_fields"] = new JsonArray();
            result["auth_roles"] ??= new JsonArray(); result["auth_role_authorities"] ??= new JsonObject();
            TemplateDefinition? policyTemplate = null;
            if (definition["template_id"] is JsonValue templateIdValue && templateIdValue.TryGetValue<int>(out int policyTemplateId))
                policyTemplate = (await TenantTemplateService.LoadAsync(db, tenant, policyTemplateId, ct)).SingleOrDefault();
            result["verification_field_policy"] = VerificationFieldPolicyView(definition, policyTemplate);
            result["used_by_categories_count"] = await CountCategoryUsageAsync(db, tenant, "document_ids", entity.Id, ct);
        }
        else if (entity.Kind == "cross-checks")
        {
            var matrix = EnsureMatrix(definition, entity.Id);
            var ids = ReadIds(definition["document_ids"]) ?? [];
            var documents = new JsonArray();
            foreach (int id in ids)
            {
                var child = await ReadEntityAsync(db, tenant, "documents", id, ct);
                if (child is null) continue;
                var childDefinition = JsonNode.Parse(child.Definition.GetRawText())!.AsObject();
                var document = new JsonObject { ["document_id"] = id, ["name"] = ReadString(childDefinition["name"]),
                    ["is_active"] = child.IsActive, ["sort_order"] = documents.Count };
                if (!listView)
                {
                    string? overrideField = matrix["documents"] is JsonArray matrixDocs
                        ? matrixDocs.OfType<JsonObject>().FirstOrDefault(item => item["document_id"]?.GetValue<int>() == id)?["compare_field"]?.GetValue<string>() : null;
                    overrideField ??= definition["document_fields"]?[id.ToString(System.Globalization.CultureInfo.InvariantCulture)]?.GetValue<string>();
                    document["compare_field"] = string.IsNullOrWhiteSpace(overrideField) ? null : overrideField;
                    document["effective_compare_field"] = string.IsNullOrWhiteSpace(overrideField) ? ReadString(definition["compare_field"]) : overrideField;
                }
                documents.Add(document);
            }
            result["documents"] = documents;
            result["compare_to_voucher_line"] = ReadBoolean(definition["compare_to_voucher_line"]);
            result["used_by_categories_count"] = await CountCategoryUsageAsync(db, tenant, "cross_check_ids", entity.Id, ct);
        }
        else if (entity.Kind == "categories")
        {
            var documents = new JsonArray(); var checks = new JsonArray();
            var required = definition["document_required"] as JsonObject ?? new JsonObject();
            foreach (int id in ReadIds(definition["document_ids"]) ?? [])
            {
                var child = await ReadEntityAsync(db, tenant, "documents", id, ct); if (child is null) continue;
                var childDef = JsonNode.Parse(child.Definition.GetRawText())!.AsObject(); string key = id.ToString(System.Globalization.CultureInfo.InvariantCulture);
                documents.Add(new JsonObject { ["document_id"] = id, ["name"] = ReadString(childDef["name"]), ["is_active"] = child.IsActive,
                    ["is_required"] = required[key]?.GetValue<bool>() ?? true,
                    ["document_type"] = ReadString(childDef["document_type"]) ?? "linked_template" });
            }
            foreach (int id in ReadIds(definition["cross_check_ids"]) ?? [])
            {
                var child = await ReadEntityAsync(db, tenant, "cross-checks", id, ct); if (child is null) continue;
                var childDef = JsonNode.Parse(child.Definition.GetRawText())!.AsObject();
                checks.Add(new JsonObject { ["cross_check_id"] = id, ["name"] = ReadString(childDef["name"]) });
            }
            result["documents"] = documents; result["cross_checks"] = checks;
            result["validation_rules"] = definition["validation_rules_json"]?.DeepClone()
                ?? JsonNode.Parse("{\"procedure_evidence\":{},\"mathematical_cross_checks\":[]}");
            result["matrix_version"] = definition["matrix_version"]?.DeepClone() ?? JsonValue.Create(1);
            result["evidence_type"] = definition["evidence_type"]?.DeepClone();
            result["used_by_vouchers_count"] = 0;
        }
        if (listView && entity.Kind == "documents")
            result = new JsonObject { ["id"] = entity.Id, ["name"] = ReadString(definition["name"]),
                ["is_active"] = entity.IsActive, ["document_type"] = ReadString(definition["document_type"]) ?? "linked_template",
                ["template_id"] = definition["template_id"]?.DeepClone(),
                ["cross_check_fields"] = result["cross_check_fields"]?.DeepClone() ?? new JsonArray() };
        else if (listView && entity.Kind == "cross-checks")
            result = new JsonObject { ["id"] = entity.Id, ["name"] = ReadString(definition["name"]),
                ["is_active"] = entity.IsActive, ["documents"] = result["documents"]?.DeepClone() ?? new JsonArray() };
        // Voucher Check assertion tags use a separate endpoint; keep the
        // base entity response limited to its authored fields.
        return JsonDocument.Parse(result.ToJsonString(JsonOptions)).RootElement.Clone();
    }

    private static async Task<int> CountCategoryUsageAsync(PostgresTransactionContext db, int tenant,
        string property, int id, CancellationToken ct)
    {
        if (property is not ("document_ids" or "cross_check_ids")) return 0;
        await using var command = db.CreateCommand($"""
            SELECT count(*) FROM public.platform_audit_rule_entities c
            WHERE c.tenant_id=$1 AND c.entity_kind='categories'
              AND coalesce(c.definition->'{property}','[]'::jsonb) @> jsonb_build_array($2::integer)
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(id);
        return Convert.ToInt32(await command.ExecuteScalarAsync(ct));
    }

    private static async Task<JsonArray> AssertionViewsAsync(PostgresTransactionContext db, int tenant, int[] ids, CancellationToken ct)
    {
        var result = new JsonArray(); if (ids.Length == 0) return result;
        await using var command = db.CreateCommand("""
            SELECT id,definition->>'name',is_active FROM public.platform_audit_rule_entities
            WHERE tenant_id=$1 AND entity_kind='assertions' AND id=ANY($2)
            ORDER BY CASE WHEN definition->>'sort_order' ~ '^[0-9]{1,9}$' THEN (definition->>'sort_order')::integer ELSE id END,id
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(ids);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) result.Add(new JsonObject { ["id"] = reader.GetInt32(0), ["name"] = reader.GetString(1), ["is_active"] = reader.GetBoolean(2) });
        return result;
    }

    private static JsonNode VerificationFieldPolicyView(JsonObject document, TemplateDefinition? template)
    {
        var columns = template?.Columns.Where(column => column.Active).OrderBy(column => column.Order).ThenBy(column => column.Id).ToArray() ?? [];
        var byId = columns.ToDictionary(column => column.Id);
        var policy = document["verification_field_policy"] as JsonObject;
        var rawCore = policy?["core"] as JsonObject ?? new JsonObject();
        var configuredCore = new Dictionary<string, int>(StringComparer.Ordinal);
        var unavailable = new JsonArray();
        var selectedCore = new Dictionary<string, int>(StringComparer.Ordinal);
        var fields = new (string Key, string Label, string[] Aliases)[]
        {
            ("vendor", "Vendor", ["vendor", "vendor name", "supplier", "supplier name"]),
            ("item", "Item", ["item", "item name", "item description", "description"]),
            ("unit", "Unit of measurement", ["unit", "unit of measurement", "uom"]),
            ("quantity", "Quantity", ["quantity", "qty"]),
            ("rate", "Rate", ["rate", "unit rate"]),
            ("amount", "Amount", ["amount", "total amount", "line amount", "net amount"])
        };
        foreach (var field in fields)
        {
            int? id = rawCore[field.Key] is JsonValue value && value.TryGetValue<int>(out int parsed) ? parsed : null;
            if (id is int configured)
            {
                if (byId.ContainsKey(configured)) selectedCore[field.Key] = configured;
                else unavailable.Add(new JsonObject { ["kind"] = "core", ["key"] = field.Key, ["label"] = field.Label, ["column_id"] = configured });
            }
            else
            {
                var aliases = field.Aliases.Select(VerificationNameKey).ToHashSet(StringComparer.Ordinal);
                var match = columns.FirstOrDefault(column => aliases.Contains(VerificationNameKey(column.StorageKey)));
                if (match is not null) selectedCore[field.Key] = match.Id;
            }
        }
        var used = selectedCore.Values.ToHashSet(); var optional = new List<(int Id, string Risk, int Order)>();
        if (policy?["optional"] is JsonArray rawOptional)
            foreach (var node in rawOptional.OfType<JsonObject>())
            {
                if (node["column_id"] is not JsonValue value || !value.TryGetValue<int>(out int id)) continue;
                if (!byId.ContainsKey(id)) { unavailable.Add(new JsonObject { ["kind"] = "optional", ["column_id"] = id }); continue; }
                if (!used.Add(id)) continue;
                string risk = ReadString(node["risk_level"]) is { } selectedRisk && (selectedRisk is "Low" or "Medium" or "High") ? selectedRisk : "Medium";
                optional.Add((id, risk, node["sort_order"]?.GetValue<int>() ?? optional.Count));
            }
        optional = optional.OrderBy(item => item.Order).ThenBy(item => item.Id).ToList();
        var optionalViews = optional.Select((item, index) =>
        {
            var column = byId[item.Id];
            return new { column_id = item.Id, column_name = column.StorageKey, data_type = column.Type,
                risk_level = item.Risk, sort_order = index };
        }).ToArray();
        var coreViews = fields.Select(field =>
        {
            selectedCore.TryGetValue(field.Key, out int id); var column = id > 0 ? byId.GetValueOrDefault(id) : null;
            return new { key = field.Key, label = field.Label, aliases = field.Aliases,
                column_id = (int?)column?.Id, column_name = column?.StorageKey, data_type = column?.Type, available = column is not null };
        }).ToArray();
        var selectedIds = coreViews.Where(item => item.column_id is not null).Select(item => item.column_id!.Value)
            .Concat(optional.Select(item => item.Id)).ToArray();
        int[] briefIds;
        if (policy?["brief_column_ids"] is JsonArray rawBrief && ReadIds(rawBrief) is { } savedBrief)
            briefIds = savedBrief.Where(selectedIds.Contains).Take(5).ToArray();
        else briefIds = policy is null ? columns.Take(5).Select(column => column.Id).ToArray() : selectedIds.Take(5).ToArray();
        return JsonSerializer.SerializeToNode(new
        {
            version = 1, template_id = template?.Id, configured = policy is not null,
            template_columns = columns.Select(column => new { column_id = column.Id, column_name = column.StorageKey,
                data_type = column.Type, sort_order = column.Order }).ToArray(),
            core_fields = coreViews, optional_fields = optionalViews, unavailable_fields = unavailable,
            selected_column_ids = selectedIds, brief_column_ids = briefIds
        }, JsonOptions)!;
    }

    private static string VerificationNameKey(string? value) => value is null ? string.Empty
        : new(value.Where(char.IsLetterOrDigit).Select(char.ToLowerInvariant).ToArray());

    private static async Task<AuditRuleEntity?> ReadEntityAsync(PostgresTransactionContext db, int tenant, string kind, int id, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT id,definition::text,is_active,revision,updated_at FROM public.platform_audit_rule_entities
            WHERE tenant_id=$1 AND entity_kind=$2 AND id=$3
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(kind); command.Parameters.AddWithValue(id);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? new(reader.GetInt32(0), kind, JsonDocument.Parse(reader.GetString(1)).RootElement.Clone(),
            reader.GetBoolean(2), reader.GetInt64(3), reader.GetDateTime(4)) : null;
    }

    private Task AuditAsync(PostgresTransactionContext db, VerifiedSession session, int tenant,
        string action, string kind, int id, string? name, long version, CancellationToken ct) => audit.WriteAsync(db,
        new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), new LogActor("authenticated", session.UserId, session.Username), null,
            "AUDIT_RULE_" + action.ToUpperInvariant(), "AuditRules", JsonSerializer.SerializeToElement(new
            { tenant_id = tenant, entity_kind = kind, entity_id = id, entity_name = name, config_version = version })), ct);

    private static JsonElement EntityJson(int id, string kind, JsonElement definition, bool active, long revision, DateTime updated)
    {
        var value = JsonNode.Parse(definition.GetRawText())?.AsObject() ?? new JsonObject();
        value["id"] = id; value["is_active"] = active; value["revision"] = revision; value["updated_at"] = updated;
        return JsonDocument.Parse(value.ToJsonString(JsonOptions)).RootElement.Clone();
    }

    internal static JsonObject? ValidateDefinition(string kind, JsonElement request, bool requireComplete = true)
    {
        string[] comparisonTypes = ["All equal", "Sequential increasing", "Sequential decreasing",
            "Within tolerance %", "Must be equal", "May be less, never more"];
        string[] documentTypes = ["linked_template", "form", "physical_bill"];
        string[] riskRatings = ["Low", "Medium", "High"];
        if (!AuditRuleKinds.Valid(kind) || request.ValueKind != JsonValueKind.Object) return null;
        try
        {
            var value = JsonNode.Parse(request.GetRawText())?.AsObject();
            if (value is null || value.Count == 0) return null;
            if (value["definition"] is JsonObject nested) value = nested;
            if (value.Count == 0 || value.ContainsKey("id") || value.ContainsKey("tenant_id")
                || value.ContainsKey("created_by") || value.ContainsKey("updated_by")
                || value.ContainsKey("is_active") || value.ContainsKey("revision")
                || value.ContainsKey("created_at") || value.ContainsKey("updated_at")
                || value.ContainsKey("is_system")) return null;
            var name = ReadName(value);
            if ((requireComplete && name is null) || (value.ContainsKey("name") && name is null)) return null;
            if (name is { Length: > 200 }) return null;
            if (kind == "cross-checks")
            {
                var compareField = ReadString(value["compare_field"]);
                var comparison = ReadString(value["comparison_type"]);
                if ((requireComplete && compareField is null) || (value.ContainsKey("compare_field") && compareField is null)) return null;
                if ((requireComplete && comparison is null) || (value.ContainsKey("comparison_type") && (comparison is null || !comparisonTypes.Contains(comparison, StringComparer.Ordinal)))) return null;
            }
            if (kind == "documents" && (requireComplete || value.ContainsKey("document_type")
                || value.ContainsKey("template_id") || value.ContainsKey("form_fields")
                || value.ContainsKey("cross_check_fields") || value.ContainsKey("verification_field_policy")))
            {
                if (value["document_type"] is not null && ReadString(value["document_type"]) is null) return null;
                string documentType = ReadString(value["document_type"]) ?? "linked_template";
                if (!documentTypes.Contains(documentType, StringComparer.Ordinal)) return null;
                value["document_type"] = documentType;
                if (value["risk_rating"] is not null && ReadString(value["risk_rating"]) is null) return null;
                var risk = ReadString(value["risk_rating"]);
                if (risk is not null && !riskRatings.Contains(risk, StringComparer.Ordinal)) return null;
                if (documentType == "physical_bill")
                {
                    value["template_id"] = null;
                    value["cross_check_fields"] = new JsonArray();
                }
                if (value.ContainsKey("auth_roles") && value["auth_roles"] is not JsonArray) return null;
                if (value["auth_roles"] is JsonArray roleArray && roleArray.Any(item => ReadString(item) is null)) return null;
                if (value.ContainsKey("auth_role_authorities") && value["auth_role_authorities"] is not JsonObject) return null;
                if (value["auth_role_authorities"] is JsonObject suppliedAuthorityMap
                    && suppliedAuthorityMap.Any(pair => pair.Value is not null
                        && (pair.Value is not JsonValue authorityValue || !authorityValue.TryGetValue<string>(out _)))) return null;
                if (requireComplete)
                {
                    var roleNames = ReadStrings(value["auth_roles"]).ToArray();
                    var suppliedAuthorities = value["auth_role_authorities"] as JsonObject ?? new JsonObject();
                    var approvedAuthorities = new JsonObject();
                    foreach (string roleName in roleNames.Where(role => role.Contains("approv", StringComparison.OrdinalIgnoreCase)))
                        if (suppliedAuthorities[roleName] is JsonValue authority && authority.TryGetValue<string>(out string? text)
                            && text is not null)
                            approvedAuthorities[roleName] = text;
                    value["auth_role_authorities"] = approvedAuthorities;
                }
                if (value["form_fields"] is JsonNode fields && fields is not JsonArray) return null;
                if (documentType == "form" && value["form_fields"] is JsonArray normalizedFields
                    && !NormalizeFormFields(normalizedFields)) return null;
                if (documentType != "form" && value["form_fields"] is JsonArray existingFields && existingFields.Count > 0) return null;
                if (value["cross_check_fields"] is JsonNode checkFields && (checkFields is not JsonArray names
                    || names.Any(item => ReadString(item) is null))) return null;
                if (value["template_id"] is JsonValue template && (!template.TryGetValue<int>(out var templateId) || templateId <= 0)) return null;
                if (value["transaction_level_approval"] is JsonNode approval
                    && (approval is not JsonValue approvalValue || !approvalValue.TryGetValue<bool>(out _))) return null;
            }
            if (kind == "categories" && value["validation_rules_json"] is JsonNode rules && rules is not JsonObject) return null;
            if (kind is "assertions" or "voucher-checks")
            {
                if (value["sort_order"] is not null && (value["sort_order"] is not JsonValue order || !order.TryGetValue<int>(out int sort) || sort < 0)) return null;
                if (requireComplete && kind == "assertions" && value["sort_order"] is null) value["sort_order"] = 0;
            }
            if (requireComplete)
            {
                if (kind == "documents")
                {
                    value["auth_roles"] ??= new JsonArray(); value["auth_role_authorities"] ??= new JsonObject();
                    value["cross_check_fields"] ??= new JsonArray(); value["form_fields"] ??= new JsonArray();
                    if (value["document_type"] is null) value["document_type"] = "linked_template";
                }
                else if (kind == "cross-checks")
                {
                    value["document_ids"] ??= new JsonArray(); value["document_fields"] ??= new JsonObject();
                    value["compare_to_voucher_line"] ??= false;
                    if (value["matrix"] is not JsonObject) value["matrix"] = new JsonObject();
                    var matrix = value["matrix"]!.AsObject();
                    matrix["documents"] ??= new JsonArray(); matrix["fields"] ??= new JsonArray();
                    if (matrix["documents"] is JsonArray columns && columns.Count == 0 && value["document_ids"] is JsonArray documentIds)
                        foreach (var documentId in documentIds)
                            if (documentId is JsonValue id && id.TryGetValue<int>(out int numericId))
                                columns.Add(new JsonObject { ["document_id"] = numericId, ["sort_order"] = columns.Count });
                }
                else if (kind == "categories")
                {
                    value["document_ids"] ??= new JsonArray(); value["cross_check_ids"] ??= new JsonArray();
                    value["validation_rules_json"] ??= new JsonObject { ["procedure_evidence"] = new JsonObject(), ["mathematical_cross_checks"] = new JsonArray() };
                    value["document_required"] ??= new JsonObject(); value["document_assertions"] ??= new JsonObject();
                    value["cross_check_assertions"] ??= new JsonObject(); value["matrix_version"] ??= 1;
                }
                else if (kind == "voucher-checks") value["assertion_ids"] ??= new JsonArray();
            }
            return value;
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException) { return null; }
    }

    private static string? ReadName(JsonNode? value) => value is JsonObject obj ? ReadString(obj["name"]) : null;
    private static string? ReadString(JsonNode? value) => value is JsonValue json && json.TryGetValue<string>(out var text)
        && !string.IsNullOrWhiteSpace(text) && text.Length <= 200 && !text.Any(char.IsControl) ? text.Trim() : null;

    private static JsonElement Clone(JsonElement value) => JsonDocument.Parse(value.GetRawText()).RootElement.Clone();
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
    private static AuditRuleResult Success<T>(T value, long? version = null, int? total = null, int? id = null) =>
        new("success", JsonSerializer.SerializeToElement(value, JsonOptions), EntityId: id, Version: version, Total: total);
    private static AuditRuleResult Invalid(string code) => new("invalid", Code: code);
    private static AuditRuleResult Forbidden() => new("forbidden", Code: "tenant_admin_required");
    private static AuditRuleResult NotFound() => new("not_found", Code: "audit_rule_not_found");
    private async Task<AuditRuleResult> Run(Func<CancellationToken, Task<AuditRuleResult>> operation, CancellationToken ct)
    {
        if (!options.Value.Enabled || !options.Value.Valid()) return new("unavailable", Code: "audit_rules_disabled");
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
            return await operation(timeout.Token);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested) { return new("timeout", Code: "audit_rule_timeout"); }
        catch (PostgresException ex) when (ex.SqlState == "55P03") { return new("busy", Code: "audit_rule_busy"); }
    }
}
