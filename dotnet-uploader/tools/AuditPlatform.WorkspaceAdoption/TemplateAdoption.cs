using System.Data;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.Templates;
using Npgsql;

namespace AuditPlatform.WorkspaceAdoption;

public static class TemplateAdoption
{
    private static readonly JsonSerializerOptions Json = new() { PropertyNameCaseInsensitive = true };
    public sealed record Column(int Id,string Name,string Type,string Category,string? Notes,bool? Active);
    public sealed record Template(int Id,string Name,string? CreatedBy,DateTime CreatedDate,bool? System,bool? Subtotals,
        string Purpose,Dictionary<string,int> Ledger,string? RateSource,bool? Active);
    public sealed record Mapping(int Id,int TemplateId,int ColumnId,int Ordinal,string? Role,bool? Compare,bool Review,int? Tenant,string? ExistingKey);
    public sealed record Header(int Id,int ClientId,int TemplateId,int ColumnId,string HeaderValue,bool? LegacyCompare);
    public sealed record Client(int Id,bool? Active,int? Tenant);
    public sealed record StoredKeys(int TemplateId,string[] Keys);
    public sealed record Source(Column[] Columns,Template[] Templates,Mapping[] Mappings,Header[] Headers,Client[] Clients,StoredKeys[] StoredKeys,long InvalidRows);
    private sealed record ColumnBinding(int ColumnId,int TenantId,string DisplayName,string Category,string? Notes,string Origin);
    private sealed record TemplateBinding(int TemplateId,int TenantId,string DisplayName,string TemplateKind,string Origin);
    private sealed record ExistingCatalog(ColumnBinding[] Columns,TemplateBinding[] Templates);

    public static int ResolveTenantId(IEnumerable<int> activeAuditFirmTenantIds,int? requestedTenantId)
    {
        var ids=activeAuditFirmTenantIds.Distinct().Order().ToArray();
        if(requestedTenantId is int requested)
        {
            if(requested<=0||!ids.Contains(requested))
                throw new InvalidOperationException("The requested workspace is not an active audit-firm workspace.");
            return requested;
        }
        if(ids.Length!=1)
            throw new InvalidOperationException("A single active audit-firm workspace is required; specify its ID when more than one exists.");
        return ids[0];
    }

    public static string[] RequiredMigrationsForApply(bool savedMappingSchemaReady,bool reviewQueueSchemaReady,bool promotionSchemaReady=true)
        => new[]
        {
            savedMappingSchemaReady?null:"0010_tenant_saved_mappings.sql",
            reviewQueueSchemaReady?null:"0024_saved_mapping_adoption_review.sql",
            promotionSchemaReady?null:"0026_legacy_template_promotion.sql"
        }.Where(name=>name is not null).Select(name=>name!).ToArray();

    // Ledger Source Notes is already treated separately by both import/identity
    // implementations. Every other stored key must resolve to an existing field.
    public static void ValidateStoredKeys(string purpose,IEnumerable<string> declared,IEnumerable<string> stored)
    {
        var keys=declared.ToHashSet(StringComparer.Ordinal);
        if(stored.Any(key=>!keys.Contains(key) && !(purpose=="account_ledger" && key=="Ledger Source Notes")))
            throw new InvalidOperationException("Stored current/history keys require explicit migration review.");
    }

    public static string? ReviewReason(Header header,Source source,IReadOnlyDictionary<int,TemplateDefinition> definitions,int tenant)
    {
        var client=source.Clients.SingleOrDefault(client=>client.Id==header.ClientId);
        if(client is null||client.Tenant!=tenant||!definitions.TryGetValue(header.TemplateId,out var definition)
            ||source.Templates.Single(template=>template.Id==header.TemplateId).Active!=true
            ||string.IsNullOrWhiteSpace(header.HeaderValue)||header.HeaderValue.Contains('\0'))
            throw new InvalidOperationException("A saved mapping has an unsupported scope/header.");
        if(client.Active!=true)return "inactive_client";
        return definition.Columns.Any(column=>column.Id==header.ColumnId&&column.Active)?null:"missing_column_binding";
    }

    public static Dictionary<int,TemplateDefinition> Validate(Source source,int tenant)
    {
        if(source.InvalidRows!=0) throw new InvalidOperationException("Stored rows are not JSON objects.");
        if(source.Columns.Any(column=>string.IsNullOrWhiteSpace(column.Name)||column.Category is null||column.Type is not ("Text" or "Numeric" or "Date"))
            ||source.Columns.GroupBy(column=>column.Name,StringComparer.OrdinalIgnoreCase).Any(group=>group.Count()>1))
            throw new InvalidOperationException("Column identities/types/names require review.");
        if(source.Templates.Any(template=>string.IsNullOrWhiteSpace(template.Name)||template.Ledger is null)
            ||source.Templates.GroupBy(template=>template.Name,StringComparer.OrdinalIgnoreCase).Any(group=>group.Count()>1))
            throw new InvalidOperationException("Template identities/names require review.");
        if(source.Mappings.Any(mapping=>(mapping.Tenant is null)!=(mapping.ExistingKey is null)
                ||(mapping.Tenant is not null&&mapping.Tenant!=tenant))
            ||source.Mappings.GroupBy(mapping=>(mapping.TemplateId,mapping.ColumnId)).Any(group=>group.Count()>1)
            ||source.Mappings.GroupBy(mapping=>(mapping.TemplateId,mapping.Ordinal)).Any(group=>group.Count()>1))
            throw new InvalidOperationException("Existing or ambiguous template bindings require review.");
        var columns=source.Columns.ToDictionary(column=>column.Id);
        var definitions=new Dictionary<int,TemplateDefinition>();
        foreach(var template in source.Templates)
        {
            var fields=source.Mappings.Where(mapping=>mapping.TemplateId==template.Id).OrderBy(mapping=>mapping.Ordinal).Select(mapping=>
            {
                if(!columns.TryGetValue(mapping.ColumnId,out var column)) throw new InvalidOperationException("Template references an unknown column.");
                if(mapping.ExistingKey is not null&&mapping.ExistingKey!=column.Name)
                    throw new InvalidOperationException("An existing mapping key differs from its permanent column key.");
                return new TemplateColumn(column.Id,column.Name,column.Name,column.Type,column.Category,mapping.Role,mapping.Compare,mapping.Review,mapping.Ordinal,1,column.Active==true);
            }).ToArray();
            if(template.Active==true && (fields.Length==0||fields.Any(field=>!field.Active)))
                throw new InvalidOperationException("An active template requires active verified columns.");
            if(TenantTemplateService.Validate(fields,template.Purpose,template.Ledger) is not null)
                throw new InvalidOperationException("Template policy/ledger roles require migration review.");
            ValidateStoredKeys(template.Purpose,fields.Select(field=>field.StorageKey),
                source.StoredKeys.SingleOrDefault(item=>item.TemplateId==template.Id)?.Keys??[]);
            definitions.Add(template.Id,new(template.Id,template.Name,template.CreatedBy??"",template.CreatedDate,template.System==true,
                template.Subtotals==true,template.Purpose,template.Ledger,template.RateSource,TenantTemplateService.KindForNew(template.Name),"native",1,fields));
        }
        if(source.StoredKeys.Any(item=>!definitions.ContainsKey(item.TemplateId))||source.Mappings.Any(mapping=>!definitions.ContainsKey(mapping.TemplateId)))
            throw new InvalidOperationException("Stored/template rows reference an unknown template.");
        foreach(var header in source.Headers)
            ReviewReason(header,source,definitions,tenant);
        if(source.Headers.GroupBy(header=>(header.ClientId,header.TemplateId,header.ColumnId)).Any(group=>group.Count()>1))
            throw new InvalidOperationException("Duplicate saved header identities require review.");
        return definitions;
    }

    public static async Task<int> RunAsync(NpgsqlConnection connection,Dictionary<string,string> options,
        string sourceOrigin="owned_local_rehearsal")
    {
        if(sourceOrigin is not ("owned_local_rehearsal" or "supabase_development"))
            throw new InvalidOperationException("Unsupported template-adoption origin.");
        if(!options.TryGetValue("tenant-id",out var value)||!int.TryParse(value,out var tenant)||tenant<=0)
            throw new InvalidOperationException("Template adoption requires the rehearsed workspace ID.");
        var apply=options.ContainsKey("apply");
        var operation=options.TryGetValue("operation",out var operationText)?Guid.Parse(operationText):Guid.Empty;
        if(apply&&(operation==Guid.Empty||!options.ContainsKey("expected"))) throw new InvalidOperationException("Apply requires operation and preview fingerprint.");
        await using var transaction=await connection.BeginTransactionAsync(IsolationLevel.Serializable);
        async Task<object?> Scalar(string statement,params object[] values)
        {
            await using var command=new NpgsqlCommand(statement,connection,transaction);
            foreach(var parameter in values)command.Parameters.AddWithValue(parameter);
            return await command.ExecuteScalarAsync();
        }
        async Task Execute(string statement,params object[] values)
        {
            await using var command=new NpgsqlCommand(statement,connection,transaction);
            foreach(var parameter in values)command.Parameters.AddWithValue(parameter);
            await command.ExecuteNonQueryAsync();
        }
        if(apply)
        {
            await Scalar("SELECT pg_advisory_xact_lock(699006209)");
            await using var prior=new NpgsqlCommand("SELECT action,details FROM public.audit_log WHERE operation_id=$1 AND module='Migration'",connection,transaction);
            prior.Parameters.AddWithValue(operation);
            await using var reader=await prior.ExecuteReaderAsync();
            if(await reader.ReadAsync())
            {
                var receipt=reader.GetString(1);
                using var parsed=JsonDocument.Parse(receipt);
                if(reader.GetString(0)!="TemplateAdoption"||parsed.RootElement.GetProperty("fingerprint").GetString()!=options["expected"]
                    ||parsed.RootElement.GetProperty("tenantId").GetInt32()!=tenant)
                    throw new InvalidOperationException("Operation identity belongs to another selection.");
                await reader.DisposeAsync();
                Console.WriteLine(receipt);await transaction.RollbackAsync();return 0;
            }
        }
        else await Execute("SET TRANSACTION READ ONLY");
        await Execute("SET LOCAL TimeZone='UTC'; SET LOCAL extra_float_digits=3; SET LOCAL DateStyle='ISO, YMD'");
        if(await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_tenants WHERE id=$1 AND tenant_type='audit_firm' AND is_active)",tenant) is not true)
            throw new InvalidOperationException("An active rehearsed firm workspace is required.");
        var mappingSetsExist=await Scalar("SELECT to_regclass('public.platform_tenant_saved_mapping_sets') IS NOT NULL") is true;
        var mappingsExist=await Scalar("SELECT to_regclass('public.platform_tenant_saved_mappings') IS NOT NULL") is true;
        if(mappingSetsExist!=mappingsExist)
            throw new InvalidOperationException("Saved-mapping schema is only partially present; review migration 0010 before continuing.");
        var savedMappingSchemaReady=mappingSetsExist&&mappingsExist;
        if(apply&&!savedMappingSchemaReady)
            throw new InvalidOperationException("Apply requires reviewed migration 0010_tenant_saved_mappings.sql to be applied separately first.");
        if(savedMappingSchemaReady&&await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_tenant_saved_mapping_sets)") is true)
            throw new InvalidOperationException("Existing native saved-mapping sets require separate review before template adoption.");
        var reviewQueueSchemaReady=await Scalar("SELECT to_regclass('public.platform_saved_mapping_adoption_review') IS NOT NULL") is true;
        var promotionSchemaReady=await Scalar("SELECT to_regclass('public.platform_tenant_columns') IS NOT NULL AND to_regclass('public.platform_tenant_templates') IS NOT NULL AND to_regprocedure('public.platform_guard_column_binding()') IS NOT NULL AND to_regprocedure('public.guard_tenant_template_definition()') IS NOT NULL AND EXISTS(SELECT 1 FROM pg_proc WHERE oid='public.platform_guard_column_binding()'::regprocedure AND prosrc LIKE '%audit.template_adoption_operation%' AND prosrc LIKE '%current_user = ''postgres''%') AND EXISTS(SELECT 1 FROM pg_proc WHERE oid='public.guard_tenant_template_definition()'::regprocedure AND prosrc LIKE '%audit.template_adoption_operation%' AND prosrc LIKE '%current_user = ''postgres''%')") is true;
        if(apply&&!promotionSchemaReady)
            throw new InvalidOperationException("Apply requires reviewed migration 0026_legacy_template_promotion.sql to be applied separately first.");
        var legacyHeadersExist=await Scalar("SELECT to_regclass('public.saved_column_mappings') IS NOT NULL") is true;
        var legacyHeadersSql=legacyHeadersExist
            ? "(SELECT coalesce(jsonb_agg(to_jsonb(s) ORDER BY \"clientId\",\"templateId\",\"columnId\"),'[]') FROM (SELECT id,client_id AS \"clientId\",template_id AS \"templateId\",template_column_id AS \"columnId\",excel_column AS \"headerValue\",compare_for_changes AS \"legacyCompare\" FROM public.saved_column_mappings)s)"
            : "'[]'::jsonb";
        var sourceJson=(string)(await Scalar($"""
            SELECT jsonb_build_object(
              'columns',(SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY id),'[]') FROM
                (SELECT id,column_name AS name,data_type AS type,category,notes,is_active AS active FROM public.master_columns)c),
              'templates',(SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]') FROM
                (SELECT id,template_name AS name,created_by AS "createdBy",created_date AS "createdDate",is_system AS system,
                  has_subtotals_by_default AS subtotals,data_purpose AS purpose,ledger_columns::jsonb AS ledger,
                  rate_source AS "rateSource",is_active AS active FROM public.templates)t),
              'mappings',(SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY "templateId",ordinal,id),'[]') FROM
                (SELECT id,template_id AS "templateId",column_id AS "columnId",sort_order AS ordinal,reconciliation_role AS role,
                  compare_for_changes AS compare,comparison_review_required AS review,tenant_id AS tenant,storage_key AS "existingKey" FROM public.template_column_mappings)p),
              'headers',{legacyHeadersSql},
              'clients',(SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY id),'[]') FROM
                (SELECT c.id,c.is_active AS active,h.tenant_id AS tenant FROM public.clients c LEFT JOIN public.platform_tenant_client_homes h ON h.client_id=c.id)c),
              'storedKeys',(SELECT coalesce(jsonb_agg(to_jsonb(k) ORDER BY "templateId"),'[]') FROM
                (SELECT template_id AS "templateId",array_agg(DISTINCT key ORDER BY key) AS keys FROM
                  (SELECT template_id,jsonb_object_keys(row_data) AS key FROM public.client_data
                   UNION ALL SELECT template_id,jsonb_object_keys(row_data) AS key FROM public.client_data_history) keys GROUP BY template_id)k),
              'invalidRows',(SELECT count(*) FROM (SELECT row_data FROM public.client_data UNION ALL SELECT row_data FROM public.client_data_history) rows
                 WHERE jsonb_typeof(row_data) IS DISTINCT FROM 'object'))::text
            """)??throw new InvalidOperationException("Missing source inventory."));
        var source=JsonSerializer.Deserialize<Source>(sourceJson,Json)??throw new InvalidOperationException("Invalid inventory.");
        var proposed=Validate(source,tenant);
        var catalogJson=(string)(await Scalar("""
            SELECT jsonb_build_object(
              'columns',(SELECT coalesce(jsonb_agg(jsonb_build_object('columnId',column_id,'tenantId',tenant_id,'displayName',display_name,'category',category,'notes',notes,'origin',binding_origin) ORDER BY column_id),'[]') FROM public.platform_tenant_columns),
              'templates',(SELECT coalesce(jsonb_agg(jsonb_build_object('templateId',template_id,'tenantId',tenant_id,'displayName',display_name,'templateKind',template_kind,'origin',binding_origin) ORDER BY template_id),'[]') FROM public.platform_tenant_templates))::text
            """)??throw new InvalidOperationException("Missing destination catalog."));
        var existingCatalog=JsonSerializer.Deserialize<ExistingCatalog>(catalogJson,Json)??throw new InvalidOperationException("Invalid destination catalog.");
        var emptyCatalog=existingCatalog.Columns.Length==0&&existingCatalog.Templates.Length==0;
        var exactLegacyCatalog=MatchesLegacyCatalog(existingCatalog,source,tenant);
        if(!emptyCatalog&&!exactLegacyCatalog)
            throw new InvalidOperationException("The existing definition catalog is partial, changed, or already native; review it before adoption.");
        var reviews=source.Headers.Select(header=>(Header:header,Reason:ReviewReason(header,source,proposed,tenant))).Where(item=>item.Reason is not null).ToArray();
        var retained=options.ContainsKey("retain-unresolved");
        var fingerprint=Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(sourceJson+"\n"+tenant+"\n"+retained)));
        var groups=source.Headers.Where(header=>reviews.All(item=>item.Header.Id!=header.Id)).GroupBy(header=>(header.ClientId,header.TemplateId)).ToArray();
        var counts=new {columns=source.Columns.Length,templates=source.Templates.Length,activeTemplates=source.Templates.Count(template=>template.Active==true),
            templateColumns=source.Mappings.Length,legacySavedMappings=source.Headers.Length,savedMappings=groups.Sum(group=>group.Count()),
            retainedForReview=reviews.Length,savedMappingSets=groups.Length};
        if(!apply)
        {
            var applyPrerequisites=RequiredMigrationsForApply(savedMappingSchemaReady,reviewQueueSchemaReady,promotionSchemaReady);
            Console.WriteLine(JsonSerializer.Serialize(new{status="preview",tenantId=tenant,fingerprint,counts,applyPrerequisites}));
            await transaction.RollbackAsync();return 0;
        }
        if(options["expected"]!=fingerprint)throw new InvalidOperationException("Source or selection changed since preview.");
        if(reviews.Length>0&&!retained)throw new InvalidOperationException("Explicitly retain unresolved legacy mappings for review before applying.");
        if(!reviewQueueSchemaReady)
            throw new InvalidOperationException("The reviewed saved-mapping adoption queue schema is required.");
        if(emptyCatalog)
        {
            await Execute("INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,category,notes,binding_origin) SELECT id,$1,column_name,category,notes,'native' FROM public.master_columns",tenant);
            foreach(var template in source.Templates)
                await Execute("INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,template_kind,binding_origin) VALUES($1,$2,$3,$4,'native')",
                    template.Id,tenant,template.Name,TenantTemplateService.KindForNew(template.Name));
        }
        else
        {
            await Scalar("SELECT set_config('audit.template_adoption_operation',$1,true)",operation.ToString("D"));
            await Execute("UPDATE public.platform_tenant_columns SET binding_origin='native' WHERE tenant_id=$1 AND binding_origin='legacy'",tenant);
            await Execute("UPDATE public.platform_tenant_templates SET binding_origin='native' WHERE tenant_id=$1 AND binding_origin='legacy'",tenant);
        }
        await Execute("UPDATE public.template_column_mappings p SET tenant_id=$1,storage_key=m.column_name FROM public.master_columns m WHERE m.id=p.column_id",tenant);
        var context=new PostgresTransactionContext(connection,transaction);
        var definitions=(await TenantTemplateService.LoadAsync(context,tenant,null,CancellationToken.None)).ToDictionary(template=>template.Id);
        foreach(var group in groups)
        {
            var template=definitions[group.Key.TemplateId];
            var binding=SavedMappingDefinition.BindingKey(template);
            await Execute("INSERT INTO public.platform_tenant_saved_mapping_sets(tenant_id,client_id,template_id,template_binding_key) VALUES($1,$2,$3,$4)",
                tenant,group.Key.ClientId,group.Key.TemplateId,binding);
            foreach(var header in group)
                await Execute("INSERT INTO public.platform_tenant_saved_mappings(tenant_id,client_id,template_id,column_id,storage_key,excel_column) VALUES($1,$2,$3,$4,$5,$6)",
                    tenant,header.ClientId,header.TemplateId,header.ColumnId,template.Columns.Single(column=>column.Id==header.ColumnId).StorageKey,header.HeaderValue);
            var entries=new List<SavedHeader>();
            await using(var check=context.CreateCommand("SELECT column_id,excel_column,storage_key FROM public.platform_tenant_saved_mappings WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3 ORDER BY column_id"))
            {
                check.Parameters.AddWithValue(tenant);check.Parameters.AddWithValue(group.Key.ClientId);check.Parameters.AddWithValue(group.Key.TemplateId);
                await using var reader=await check.ExecuteReaderAsync();
                while(await reader.ReadAsync())entries.Add(new(reader.GetInt32(0),reader.GetString(1),reader.GetString(2)));
            }
            var storedBinding=(string)(await Scalar("SELECT template_binding_key FROM public.platform_tenant_saved_mapping_sets WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3",
                tenant,group.Key.ClientId,group.Key.TemplateId))!;
            if(entries.Count!=group.Count()||entries.Any(entry=>group.Single(header=>header.ColumnId==entry.ColumnId).HeaderValue!=entry.Header)
                ||SavedMappingDefinition.NeedsReview(template,storedBinding,entries))
                throw new InvalidOperationException("An adopted saved mapping still requires policy/header review.");
        }
        foreach(var item in reviews)
            await Execute("INSERT INTO public.platform_saved_mapping_adoption_review(tenant_id,source_mapping_id,client_id,template_id,reason) VALUES($1,$2,$3,$4,$5)",
                tenant,item.Header.Id,item.Header.ClientId,item.Header.TemplateId,item.Reason!);
        var outcome=JsonSerializer.Serialize(new{status="completed",stage="templates",operation,tenantId=tenant,fingerprint,counts});
        await Execute("""
            INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,actor_kind,source_origin)
            VALUES('System','TemplateAdoption','Migration',$1,$2,$3,'system',$4)
            """,outcome,Guid.NewGuid(),operation,sourceOrigin);
        try{await transaction.CommitAsync();}catch{Console.WriteLine("{\"status\":\"commit_outcome_unknown\"}");return 2;}
        Console.WriteLine(outcome);return 0;
    }

    private static bool MatchesLegacyCatalog(ExistingCatalog catalog,Source source,int tenant)
    {
        if(catalog.Columns.Length!=source.Columns.Length||catalog.Templates.Length!=source.Templates.Length)return false;
        var columns=catalog.Columns.ToDictionary(row=>row.ColumnId);
        foreach(var sourceColumn in source.Columns)
            if(!columns.TryGetValue(sourceColumn.Id,out var binding)||binding.TenantId!=tenant||binding.Origin!="legacy"
                ||binding.DisplayName!=sourceColumn.Name||binding.Category!=sourceColumn.Category||binding.Notes!=sourceColumn.Notes)return false;
        var templates=catalog.Templates.ToDictionary(row=>row.TemplateId);
        foreach(var sourceTemplate in source.Templates)
            if(!templates.TryGetValue(sourceTemplate.Id,out var binding)||binding.TenantId!=tenant||binding.Origin!="legacy"
                ||binding.DisplayName!=sourceTemplate.Name||binding.TemplateKind!=TenantTemplateService.KindForNew(sourceTemplate.Name))return false;
        return true;
    }
}
