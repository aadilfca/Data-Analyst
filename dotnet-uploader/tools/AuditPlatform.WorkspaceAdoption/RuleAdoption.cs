using System.Data;
using System.Text.Json;
using System.Text.Json.Nodes;
using AuditPlatform.Api.AuditRules;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.WorkspaceAdoption;

public static class RuleAdoption
{
    public static async Task<int> RunAsync(NpgsqlConnection connection,Dictionary<string,string> options)
    {
        if(!options.TryGetValue("tenant-id",out var text)||!int.TryParse(text,out int tenant)||tenant<=0
            ||!options.TryGetValue("actor-username",out var actorName)||string.IsNullOrWhiteSpace(actorName))
            throw new InvalidOperationException("Rule adoption requires a workspace and explicit migration-actor username.");
        bool apply=options.ContainsKey("apply"),retain=options.ContainsKey("retain-unresolved");
        var operation=options.TryGetValue("operation",out var operationText)?Guid.Parse(operationText):Guid.Empty;
        if(apply&&(operation==Guid.Empty||!options.ContainsKey("expected")))throw new InvalidOperationException("Apply requires the preview fingerprint and operation ID.");
        await using var transaction=await connection.BeginTransactionAsync(IsolationLevel.Serializable);
        var db=new PostgresTransactionContext(connection,transaction);
        async Task<object?> Scalar(string sql,params object[] values)
        {
            await using var command=db.CreateCommand(sql);foreach(var value in values)command.Parameters.AddWithValue(value);
            return await command.ExecuteScalarAsync();
        }
        async Task Execute(string sql,params object[] values){await Scalar(sql,values);}
        if(apply)
        {
            await Scalar("SELECT pg_advisory_xact_lock(699006209)");
            await Scalar("SELECT pg_advisory_xact_lock(746102)");
            await Scalar("SELECT pg_advisory_xact_lock(1095910220,$1)",tenant);
            await using var prior=db.CreateCommand("SELECT action,details FROM public.audit_log WHERE operation_id=$1 AND module='Migration'");
            prior.Parameters.AddWithValue(operation);await using var reader=await prior.ExecuteReaderAsync();
            if(await reader.ReadAsync())
            {
                var receipt=reader.GetString(1);using var parsed=JsonDocument.Parse(receipt);
                if(reader.GetString(0)!="RuleAdoption"||parsed.RootElement.GetProperty("fingerprint").GetString()!=options["expected"]
                    ||parsed.RootElement.GetProperty("tenantId").GetInt32()!=tenant||parsed.RootElement.GetProperty("actorUsername").GetString()!=actorName
                    ||parsed.RootElement.GetProperty("retainUnresolved").GetBoolean()!=retain)
                    throw new InvalidOperationException("Operation identity belongs to another rule selection.");
                await reader.DisposeAsync();await transaction.RollbackAsync();Console.WriteLine(receipt);return 0;
            }
        }
        else await Execute("SET TRANSACTION READ ONLY");
        await Execute("SET LOCAL TimeZone='UTC'; SET LOCAL extra_float_digits=3; SET LOCAL DateStyle='ISO, YMD'");
        if(await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_tenants WHERE id=$1 AND tenant_type='audit_firm' AND is_active)",tenant) is not true)
            throw new InvalidOperationException("An active rehearsed firm workspace is required.");
        if(await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_audit_rule_entities) OR EXISTS(SELECT 1 FROM public.platform_audit_rule_state) OR EXISTS(SELECT 1 FROM public.platform_audit_rule_signature_specimens)") is true)
            throw new InvalidOperationException("First rule adoption requires an empty native Rulebook catalogue.");
        if(await Scalar("SELECT to_regclass('public.platform_audit_rule_adoption_map') IS NOT NULL") is not true)
            throw new InvalidOperationException("Candidate adoption-map schema is required.");
        int actor;
        await using(var actorQuery=db.CreateCommand("""
            SELECT u.id FROM public.users u JOIN public.platform_tenant_memberships m ON m.user_id=u.id
            WHERE u.username=$1 AND u.is_active IS TRUE AND m.tenant_id=$2 AND m.is_active IS TRUE AND m.tenant_role='admin'
            """))
        {
            actorQuery.Parameters.AddWithValue(actorName);actorQuery.Parameters.AddWithValue(tenant);
            await using var reader=await actorQuery.ExecuteReaderAsync();
            if(!await reader.ReadAsync())throw new InvalidOperationException("The named migration actor must be an existing active workspace administrator.");
            actor=reader.GetInt32(0);if(await reader.ReadAsync())throw new InvalidOperationException("Ambiguous migration actor.");
        }
        var source=new Dictionary<string,JsonArray>(StringComparer.Ordinal);
        foreach(var table in RuleAdoptionDefinition.Tables.Order(StringComparer.Ordinal))
        {
            // Identifiers come only from the reviewed constant table inventory.
            var raw=(string)(await Scalar($"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM public.{table} t"))!;
            source.Add(table,JsonNode.Parse(raw)!.AsArray());
        }
        var templates=await TenantTemplateService.LoadAsync(db,tenant,null,CancellationToken.None,lockRows:apply);
        var converted=RuleAdoptionDefinition.Convert(source,templates);
        foreach(var client in converted.Specimens.Select(specimen=>specimen.ClientId).Distinct())
            if(await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_tenant_client_homes WHERE tenant_id=$1 AND client_id=$2)",tenant,client) is not true)
                throw new InvalidOperationException("A signature specimen has no owned client home.");
        var fingerprint=RuleAdoptionDefinition.Hash(JsonSerializer.Serialize(new {tenant,actor,actorName,retain,source,
            templates=templates.OrderBy(template=>template.Id).Select(template=>new {template.Id,template.ConfigurationKey})}));
        var counts=new {entities=converted.Entities.Length,activeEntities=converted.Entities.Count(entity=>entity.Active),
            kinds=converted.Entities.GroupBy(entity=>entity.Kind).OrderBy(group=>group.Key,StringComparer.Ordinal).ToDictionary(group=>group.Key,group=>group.Count()),
            formFields=converted.Fields.Length,specimens=converted.Specimens.Length,retainedForReview=converted.Reviews.Length};
        if(!apply)
        {await transaction.RollbackAsync();Console.WriteLine(JsonSerializer.Serialize(new {status="preview",tenantId=tenant,fingerprint,version=converted.Version,counts}));return 0;}
        if(options["expected"]!=fingerprint)throw new InvalidOperationException("Rule sources or selection changed since preview.");
        if(converted.Reviews.Length>0&&!retain)throw new InvalidOperationException("Explicitly retain unresolved specimens before applying.");
        foreach(var entity in converted.Entities)
        {
            await using var insert=db.CreateCommand("""
                INSERT INTO public.platform_audit_rule_entities(id,tenant_id,entity_kind,definition,is_active,created_by,updated_by,created_at)
                OVERRIDING SYSTEM VALUE VALUES($1,$2,$3,$4,$5,$6,$6,$7)
                """);
            insert.Parameters.AddWithValue(entity.Id);insert.Parameters.AddWithValue(tenant);insert.Parameters.AddWithValue(entity.Kind);
            insert.Parameters.AddWithValue(NpgsqlDbType.Jsonb,entity.Definition.ToJsonString());insert.Parameters.AddWithValue(entity.Active);
            insert.Parameters.AddWithValue(actor);insert.Parameters.AddWithValue(entity.Created);await insert.ExecuteNonQueryAsync();
            await Execute("INSERT INTO public.platform_audit_rule_adoption_map(tenant_id,entity_kind,source_id,entity_id,source_sha256,source_is_system) VALUES($1,$2,$3,$4,$5,$6)",
                tenant,entity.Kind,entity.SourceId,entity.Id,entity.SourceHash,entity.System);
        }
        foreach(var field in converted.Fields)
            await Execute("INSERT INTO public.platform_audit_rule_form_adoption_map(tenant_id,source_field_id,document_id,field_id) VALUES($1,$2,$3,$4)",tenant,field.SourceId,field.DocumentId,field.Id);
        foreach(var entity in converted.Entities.Where(entity=>entity.Active&&entity.Kind is "documents" or "cross-checks"))
            if(!await AuditRuleService.ValidateReferencesAsync(db,tenant,entity.Kind,entity.Definition,CancellationToken.None))
                throw new InvalidOperationException($"Native reference validation requires review for {entity.Kind} source ID {entity.SourceId}.");
        foreach(var specimen in converted.Specimens)
        {
            await using var insert=db.CreateCommand("""
                INSERT INTO public.platform_audit_rule_signature_specimens
                  (id,tenant_id,client_id,document_id,role_name,expected_signatory_name,authority,is_active,created_by,updated_by,created_at,updated_at)
                OVERRIDING SYSTEM VALUE VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$9,$10,$11)
                """);
            foreach(var value in new object[]{specimen.Id,tenant,specimen.ClientId,specimen.DocumentId,specimen.Role,specimen.Name,
                (object?)specimen.Authority??DBNull.Value,specimen.Active,actor,specimen.Created,specimen.Updated})insert.Parameters.AddWithValue(value);
            // Nullable authority needs an explicit type when represented by DBNull.
            insert.Parameters[6].NpgsqlDbType=NpgsqlDbType.Text;
            await insert.ExecuteNonQueryAsync();
            await Execute("INSERT INTO public.platform_audit_rule_specimen_adoption_map(tenant_id,source_id,specimen_id,source_sha256) VALUES($1,$2,$3,$4)",tenant,specimen.SourceId,specimen.Id,specimen.SourceHash);
        }
        foreach(var review in converted.Reviews)
            await Execute("INSERT INTO public.platform_audit_rule_adoption_review(tenant_id,source_kind,source_id,reason,source_sha256) VALUES($1,$2,$3,$4,$5)",tenant,review.Kind,review.SourceId,review.Reason,review.SourceHash);
        await Execute("INSERT INTO public.platform_audit_rule_state(tenant_id,config_version) VALUES($1,$2)",tenant,converted.Version);
        await Execute("INSERT INTO public.platform_audit_rule_changes(tenant_id,config_version,entity_kind,action,actor_user_id) VALUES($1,$2,'migration','created',$3)",tenant,converted.Version,actor);
        await AuditRuleService.PublishSnapshotsAsync(db,tenant,converted.Version,CancellationToken.None);
        long categories=converted.Entities.Count(entity=>entity.Kind=="categories");
        if(await Scalar("SELECT count(*) FROM public.platform_audit_rule_category_snapshots WHERE tenant_id=$1 AND config_version=$2",tenant,converted.Version) is not long published||published!=categories)
            throw new InvalidOperationException("Native Category snapshot coverage differs from the conversion.");
        var outcome=JsonSerializer.Serialize(new {status="completed",stage="audit-rules",operation,tenantId=tenant,actorUsername=actorName,
            retainUnresolved=retain,fingerprint,version=converted.Version,counts,historicalEvidenceAdopted=false});
        await Execute("""
            INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,actor_kind,source_origin)
            VALUES('System','RuleAdoption','Migration',$1,$2,$3,'system','owned_local_rehearsal')
            """,outcome,Guid.NewGuid(),operation);
        // These are new native sequences only. Keep any prior failure gaps;
        // PostgreSQL sequences are not transactional and must never be rewound.
        if(converted.Entities.Length>0)await Scalar("SELECT setval('public.platform_audit_rule_entities_id_seq',GREATEST((SELECT last_value FROM public.platform_audit_rule_entities_id_seq),$1),true)",converted.Entities.Max(entity=>entity.Id));
        if(converted.Fields.Length>0)await Scalar("SELECT setval('public.platform_audit_rule_form_field_id_seq',GREATEST((SELECT last_value FROM public.platform_audit_rule_form_field_id_seq),$1),true)",converted.Fields.Max(field=>field.Id));
        if(converted.Specimens.Length>0)await Scalar("SELECT setval('public.platform_audit_rule_signature_specimens_id_seq',GREATEST((SELECT last_value FROM public.platform_audit_rule_signature_specimens_id_seq),$1),true)",converted.Specimens.Max(specimen=>specimen.Id));
        try{await transaction.CommitAsync();}catch{Console.WriteLine("{\"status\":\"commit_outcome_unknown\"}");return 2;}
        Console.WriteLine(outcome);return 0;
    }
}
