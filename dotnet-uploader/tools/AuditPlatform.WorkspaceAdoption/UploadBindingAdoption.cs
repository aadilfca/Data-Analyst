using System.Data;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Uploads;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.WorkspaceAdoption;

// An explicit, private operator plan records legacy semantics. Runtime label
// inference and transaction/derived-field backfills are deliberately excluded.
public static class UploadBindingAdoption
{
    public sealed record Field(string Role,int ColumnId,string StorageKey);
    public sealed record Selection(int TemplateId,Field[] Fields,string? Review=null);
    public sealed record Plan(int Version,int TenantId,Selection[] Templates);
    public sealed record Reviewed(TemplateDefinition Template,Dictionary<string,int> Bindings,string? Review);
    private static readonly JsonSerializerOptions Json=new()
    {PropertyNameCaseInsensitive=true,UnmappedMemberHandling=JsonUnmappedMemberHandling.Disallow};

    public static string PlanPath(string owner,string supplied)
    {
        var path=Path.GetFullPath(Path.Combine(owner,supplied));
        if(!string.Equals(Path.GetDirectoryName(path),Path.GetFullPath(owner),OperatingSystem.IsWindows()?StringComparison.OrdinalIgnoreCase:StringComparison.Ordinal))
            throw new InvalidOperationException("The private binding plan must be directly inside the owned recovery directory.");
        return path;
    }

    public static Reviewed[] Validate(Plan plan,int tenant,IReadOnlyList<TemplateDefinition> templates)
    {
        if(plan.Version!=1||plan.TenantId!=tenant||plan.Templates is null||plan.Templates.Length is <1 or >500
            ||plan.Templates.Any(item=>item is null||item.Fields is null||item.Fields.Length is <1 or >9)
            ||plan.Templates.GroupBy(item=>item.TemplateId).Any(group=>group.Count()!=1))
            throw new InvalidOperationException("Invalid, foreign or duplicate binding-plan selection.");
        if(templates.Any(template=>template.Origin!="native"||template.Purpose is not ("standard" or "account_ledger")))
            throw new InvalidOperationException("Reviewed native template definitions are required.");
        var ordinary=templates.Where(template=>template.Purpose=="standard").OrderBy(template=>template.Id).ToArray();
        if(!ordinary.Select(template=>template.Id).SequenceEqual(plan.Templates.Select(item=>item.TemplateId).Order()))
            throw new InvalidOperationException("Explicitly cover every active ordinary template; ledger adoption is separate.");
        var result=new List<Reviewed>();
        foreach(var template in ordinary)
        {
            var selection=plan.Templates.Single(item=>item.TemplateId==template.Id);
            if(template.UploadBindings is {Count:>0}||template.Columns.Count==0||template.Columns.Any(column=>!column.Active||column.Review)
                ||selection.Fields.Any(field=>field is null||string.IsNullOrWhiteSpace(field.Role)||string.IsNullOrWhiteSpace(field.StorageKey))
                ||selection.Fields.GroupBy(field=>field.Role,StringComparer.Ordinal).Any(group=>group.Count()!=1))
                throw new InvalidOperationException("Existing or unresolved template bindings require review.");
            var bindings=selection.Fields.ToDictionary(field=>field.Role,field=>field.ColumnId,StringComparer.Ordinal);
            if(selection.Fields.Any(field=>!template.Columns.Any(column=>column.Id==field.ColumnId&&column.StorageKey==field.StorageKey))
                ||TenantTemplateService.UploadDerivedBindingError(template.Columns,bindings) is not null)
                throw new InvalidOperationException("The plan does not match permanent keys/types or shared native binding policy.");
            // Identity currently requires the configured document date to agree
            // with its primary Date field. Never silently change that policy.
            var configured=template.Columns.SingleOrDefault(column=>column.Role=="document_date");
            if(configured is not null&&configured.Id!=template.Columns.FirstOrDefault(column=>column.Type=="Date")?.Id)
                throw new InvalidOperationException("Comparison-date policy requires separate review.");
            try
            {
                if(UploadDerivedFieldService.Bind(template with {UploadBindings=bindings}) is null)
                    throw new InvalidOperationException("A nonempty native derivation profile is required.");
                if(selection.Review is not null)
                    throw new InvalidOperationException("A review exclusion must describe the actual unresolved condition.");
            }
            catch(IdentityReview error) when(selection.Review=="rate_quantity_missing"&&error.Code=="derived_rate_inputs_required"
                &&template.RateSource=="auto"&&bindings.ContainsKey("rate")&&bindings.ContainsKey("amount")&&!bindings.ContainsKey("quantity"))
            { /* Explicitly retain the legacy limitation; no binding is written. */ }
            result.Add(new(template,bindings,selection.Review));
        }
        return result.ToArray();
    }

    public static async Task<int> RunAsync(NpgsqlConnection connection,Dictionary<string,string> options)
    {
        if(!options.TryGetValue("tenant-id",out var tenantText)||!int.TryParse(tenantText,out int tenant)||tenant<=0
            ||!options.TryGetValue("binding-plan",out var supplied))
            throw new InvalidOperationException("Upload binding adoption requires a workspace ID and private plan.");
        var path=PlanPath(options["owner-dir"],supplied);
        if(new FileInfo(path).Length>65536)throw new InvalidOperationException("Binding plan is too large.");
        var bytes=await File.ReadAllBytesAsync(path);
        var plan=JsonSerializer.Deserialize<Plan>(bytes,Json)??throw new InvalidOperationException("Missing binding plan.");
        var planHash=Convert.ToHexStringLower(SHA256.HashData(bytes));
        var apply=options.ContainsKey("apply");
        var operation=options.TryGetValue("operation",out var text)?Guid.Parse(text):Guid.Empty;
        if(apply&&(operation==Guid.Empty||!options.ContainsKey("expected")))
            throw new InvalidOperationException("Apply requires the matching preview fingerprint and operation ID.");
        await using var transaction=await connection.BeginTransactionAsync(IsolationLevel.Serializable);
        var db=new PostgresTransactionContext(connection,transaction);
        async Task<object?> Scalar(string sql,params object[] parameters)
        {
            await using var command=db.CreateCommand(sql);
            foreach(var parameter in parameters)command.Parameters.AddWithValue(parameter);
            return await command.ExecuteScalarAsync();
        }
        if(apply)
        {
            await Scalar("SELECT pg_advisory_xact_lock(699006209)");
            await Scalar("SELECT pg_advisory_xact_lock(746102)");
            await using var prior=db.CreateCommand("SELECT action,details FROM public.audit_log WHERE operation_id=$1 AND module='Migration'");
            prior.Parameters.AddWithValue(operation);
            await using var reader=await prior.ExecuteReaderAsync();
            if(await reader.ReadAsync())
            {
                var receipt=reader.GetString(1);
                using var parsed=JsonDocument.Parse(receipt);
                if(reader.GetString(0)!="UploadBindingAdoption"||parsed.RootElement.GetProperty("fingerprint").GetString()!=options["expected"]
                    ||parsed.RootElement.GetProperty("tenantId").GetInt32()!=tenant||parsed.RootElement.GetProperty("planHash").GetString()!=planHash)
                    throw new InvalidOperationException("Operation identity belongs to another selection.");
                await reader.DisposeAsync();await transaction.RollbackAsync();Console.WriteLine(receipt);return 0;
            }
        }
        else await Scalar("SET TRANSACTION READ ONLY");
        if(await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_tenants WHERE id=$1 AND tenant_type='audit_firm' AND is_active)",tenant) is not true)
            throw new InvalidOperationException("An active rehearsed firm workspace is required.");
        if(await Scalar("SELECT EXISTS(SELECT 1 FROM public.platform_template_upload_bindings)") is true)
            throw new InvalidOperationException("First binding adoption requires an empty native binding catalogue.");
        var templates=await TenantTemplateService.LoadAsync(db,tenant,null,CancellationToken.None,lockRows:apply);
        var reviewed=Validate(plan,tenant,templates);
        var ready=reviewed.Where(item=>item.Review is null).ToArray();
        var deferred=reviewed.Where(item=>item.Review is not null).Select(item=>new {templateId=item.Template.Id,reason=item.Review}).ToArray();
        var fingerprint=Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new
            {planHash,tenant,definitions=templates.OrderBy(template=>template.Id).Select(template=>new {template.Id,template.ConfigurationKey})}))));
        var counts=new {boundTemplates=ready.Length,roles=ready.Sum(item=>item.Bindings.Count),reviewTemplates=deferred.Length,
            separateLedgerTemplates=templates.Count(template=>template.Purpose=="account_ledger")};
        if(!apply)
        {await transaction.RollbackAsync();Console.WriteLine(JsonSerializer.Serialize(new {status="preview",tenantId=tenant,fingerprint,planHash,counts,deferred}));return 0;}
        if(fingerprint!=options["expected"])throw new InvalidOperationException("Source or private selection changed since preview.");
        foreach(var item in ready)
        {
            await using var insert=db.CreateCommand("INSERT INTO public.platform_template_upload_bindings(template_id,tenant_id,bindings) VALUES($1,$2,$3)");
            insert.Parameters.AddWithValue(item.Template.Id);insert.Parameters.AddWithValue(tenant);
            insert.Parameters.AddWithValue(NpgsqlDbType.Jsonb,JsonSerializer.Serialize(item.Bindings));
            await insert.ExecuteNonQueryAsync();
        }
        // Reload actual persisted profiles through the native consumer before
        // committing. The saved-header signature intentionally excludes these roles.
        var persisted=await TenantTemplateService.LoadAsync(db,tenant,null,CancellationToken.None);
        foreach(var item in ready)
        {
            var actual=persisted.Single(template=>template.Id==item.Template.Id);
            if(actual.UploadBindings is null||actual.UploadBindings.Count!=item.Bindings.Count
                ||item.Bindings.Any(pair=>actual.UploadBindings.GetValueOrDefault(pair.Key)!=pair.Value)
                ||UploadDerivedFieldService.Bind(actual) is null)
                throw new InvalidOperationException("Persisted native binding profile differs from the reviewed plan.");
        }
        var outcome=JsonSerializer.Serialize(new {status="completed",stage="upload-bindings",operation,tenantId=tenant,fingerprint,planHash,counts,deferred});
        await using(var audit=db.CreateCommand("""
            INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,actor_kind,source_origin)
            VALUES('System','UploadBindingAdoption','Migration',$1,$2,$3,'system','owned_local_rehearsal')
            """))
        {
            audit.Parameters.AddWithValue(outcome);audit.Parameters.AddWithValue(Guid.NewGuid());audit.Parameters.AddWithValue(operation);
            await audit.ExecuteNonQueryAsync();
        }
        try{await transaction.CommitAsync();}catch{Console.WriteLine("{\"status\":\"commit_outcome_unknown\"}");return 2;}
        Console.WriteLine(outcome);return 0;
    }
}
