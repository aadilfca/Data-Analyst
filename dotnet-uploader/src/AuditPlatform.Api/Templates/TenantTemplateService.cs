using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Templates;

public sealed class TenantTemplateService(PostgresTransactions transactions,PostgresAuditWriter audit,IOptions<SavedMappingOptions>? savedMappings=null)
{
    public const string ReviewMessage="This configuration has existing data or unverified dependencies. Review a separate migration; no source rows were changed.";
    private sealed record Actor(int Tenant,int User,string Name);
    private static readonly (string Role,string Name,string Type)[] LedgerRecipe=[
        ("voucher","Voucher ID","Text"),("date","Transaction Date","Date"),("description","Voucher Detail","Text"),
        ("debit","Debit Amount","Numeric"),("credit","Credit Amount","Numeric"),("balance","Balance","Text")];

    public Task<TemplateResult> ListAsync(VerifiedSession? session,CancellationToken ct=default)=>
        RunAsync(session,false,async(db,actor,token)=>new(TemplateStatus.Success,await LoadAsync(db,actor.Tenant,null,token)),ct);
    public Task<TemplateResult> GetAsync(VerifiedSession? session,int id,CancellationToken ct=default)=>
        RunAsync(session,false,async(db,actor,token)=>
        {
            var rows=await LoadAsync(db,actor.Tenant,id,token);
            return rows.Count==0 ? new(TemplateStatus.NotFound) : new(TemplateStatus.Success,rows);
        },ct);

    public async Task<TemplateResult> SaveAsync(VerifiedSession? session,int? id,TemplateInput input,CancellationToken ct=default)
    {
        try { return await RunAsync(session,true,(db,actor,token)=>SaveCoreAsync(db,actor,id,input,token),ct); }
        catch(PostgresException e) when(e.SqlState==PostgresErrorCodes.UniqueViolation)
        { return new(TemplateStatus.Conflict,Detail:"Template name already exists"); }
    }

    private async Task<TemplateResult> SaveCoreAsync(PostgresTransactionContext db,Actor actor,int? id,TemplateInput input,CancellationToken ct)
    {
        var before=id is null ? null : (await LoadAsync(db,actor.Tenant,id,ct)).SingleOrDefault();
        if(id is not null && before is null) return new(TemplateStatus.NotFound);
        if(before is not null && before.Origin!="native") return Review();
        if(before is not null && input.Revision!=before.ConfigurationKey) return Stale();
        if(string.IsNullOrWhiteSpace(input.Name) || input.Name.Trim().Length>200) return Invalid("Supply a template name of at most 200 characters.");
        if(input.Columns.Count==0 || input.Columns.Distinct().Count()!=input.Columns.Count) return Invalid("Select distinct active template columns.");
        var selected=await SelectColumnsAsync(db,actor.Tenant,input.Columns,ct);
        if(selected.Count!=input.Columns.Count) return Invalid("Select distinct active native columns from this workspace.");
        if(input.ColumnVersions is not null && (input.ColumnVersions.Keys.Except(input.Columns).Any()
            || selected.Any(c=>!input.ColumnVersions.TryGetValue(c.Id,out var version)||version!=c.Version))) return Stale();
        if(input.Roles?.Keys.Except(input.Columns).Any()==true) return Invalid("Reconciliation roles must belong to selected template columns.");
        if(input.Comparisons?.Keys.Except(input.Columns).Any()==true) return Invalid("Comparison settings must belong to selected template columns.");
        var old=before?.Columns.ToDictionary(c=>c.Id)??[];
        var ordered=input.Columns.Select((cid,index)=>
        {
            var column=selected.Single(c=>c.Id==cid); old.TryGetValue(cid,out var previous);
            return column with {Order=index,Role=input.Roles is not null&&input.Roles.TryGetValue(cid,out var role) ? role:previous?.Role,
                Compare=input.Comparisons is not null&&input.Comparisons.TryGetValue(cid,out var compare) ? compare:previous?.Compare,
                Review=input.Comparisons?.ContainsKey(cid)==true ? false:previous?.Review??false};
        }).ToArray();
        var purpose=input.Purpose??before?.Purpose??"standard";
        var ledger=input.Ledger??before?.Ledger??[];
        var error=Validate(ordered,purpose,ledger);
        if(error is not null) return Invalid(error);
        var kind=before?.Kind??KindForNew(input.Name);
        var rate=kind=="standard" ? null : input.RateSource??before?.RateSource??"auto";
        if(rate is not (null or "auto" or "imported" or "derived")) return Invalid("rate_source must be auto, imported or derived");
        if(purpose=="account_ledger") rate=null;
        var uploadBindings=input.UploadBindings??before?.UploadBindings??new Dictionary<string,int>();
        if(UploadDerivedBindingError(ordered,uploadBindings) is {} bindingError) return Invalid(bindingError);
        if(await UploadBindingReviewAsync(db,before,uploadBindings,ct) is {} bindingReview) return new(TemplateStatus.Review,Detail:bindingReview);
        if(before is not null)
        {
            bool structure=before.Purpose!=purpose || !SameLedger(before.Ledger,ledger)
                || !before.Columns.Select(c=>c.Id).SequenceEqual(input.Columns)
                || before.Columns.Where((c,index)=>index<ordered.Length && (c.Role!=ordered[index].Role || c.Compare!=ordered[index].Compare || c.Review!=ordered[index].Review)).Any();
            if(structure && await HasExternalReferencesAsync(db,before.Id,ct)) return Review();
            var hasData=await HasDataAsync(db,before.Id,ct);
            if((before.Purpose!=purpose || !SameLedger(before.Ledger,ledger))
                && (hasData || await HasUploadsAsync(db,before.Id,ct))) return Review();
            if(hasData)
            {
                // Keep bindings for historical fields; removal is a separate
                // reviewed migration even when active current rows are empty.
                if(before.Columns.Select(c=>c.Id).Except(input.Columns).Any()) return Review();
                var oldDate=DateColumn(before.Columns)?.StorageKey; var newDate=DateColumn(ordered)?.StorageKey;
                if(oldDate!=newDate) return Review();
                var oldKey=DocumentColumn(before.Columns)?.StorageKey; var newKey=DocumentColumn(ordered)?.StorageKey;
                if(oldKey!=newKey && await ChangesIdentitiesAsync(db,before.Id,oldKey,newKey,ct)) return Review();
            }
        }
        int target;
        if(before is null)
        {
            await using(var command=db.CreateCommand("""
                INSERT INTO public.templates(template_name,created_by,rate_source,has_subtotals_by_default,data_purpose,ledger_columns)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING id
                """))
            {
                command.Parameters.AddWithValue("tpl_"+Guid.NewGuid().ToString("N")); command.Parameters.AddWithValue(actor.Name);
                AddText(command,rate); command.Parameters.AddWithValue(input.Subtotals); command.Parameters.AddWithValue(purpose);
                command.Parameters.AddWithValue(JsonSerializer.Serialize(ledger)); target=(int)(await command.ExecuteScalarAsync(ct))!;
            }
            await using var definition=db.CreateCommand("""
                INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,template_kind,binding_origin)
                VALUES($1,$2,$3,$4,'native')
                """);
            definition.Parameters.AddWithValue(target); definition.Parameters.AddWithValue(actor.Tenant);
            definition.Parameters.AddWithValue(input.Name.Trim()); definition.Parameters.AddWithValue(kind); await definition.ExecuteNonQueryAsync(ct);
        }
        else
        {
            target=before.Id;
            await using(var command=db.CreateCommand("""
                UPDATE public.templates SET rate_source=$1,has_subtotals_by_default=$2,data_purpose=$3,ledger_columns=$4 WHERE id=$5
                """))
            {
                AddText(command,rate); command.Parameters.AddWithValue(input.Subtotals); command.Parameters.AddWithValue(purpose);
                command.Parameters.AddWithValue(JsonSerializer.Serialize(ledger)); command.Parameters.AddWithValue(target); await command.ExecuteNonQueryAsync(ct);
            }
            await using(var name=db.CreateCommand("UPDATE public.platform_tenant_templates SET display_name=$1 WHERE template_id=$2 AND tenant_id=$3"))
            {
                name.Parameters.AddWithValue(input.Name.Trim()); name.Parameters.AddWithValue(target); name.Parameters.AddWithValue(actor.Tenant); await name.ExecuteNonQueryAsync(ct);
            }
            await using var clear=db.CreateCommand("DELETE FROM public.template_column_mappings WHERE template_id=$1 AND tenant_id=$2");
            clear.Parameters.AddWithValue(target); clear.Parameters.AddWithValue(actor.Tenant); await clear.ExecuteNonQueryAsync(ct);
        }
        foreach(var column in ordered)
        {
            await using var mapping=db.CreateCommand("""
                INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,compare_for_changes,comparison_review_required,tenant_id,storage_key)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                """);
            mapping.Parameters.AddWithValue(target); mapping.Parameters.AddWithValue(column.Id); mapping.Parameters.AddWithValue(column.Order);
            AddText(mapping,column.Role); mapping.Parameters.AddWithValue(NpgsqlDbType.Boolean,(object?)column.Compare??DBNull.Value);
            mapping.Parameters.AddWithValue(column.Review); mapping.Parameters.AddWithValue(actor.Tenant); mapping.Parameters.AddWithValue(column.StorageKey);
            await mapping.ExecuteNonQueryAsync(ct);
        }
        if(uploadBindings.Count>0 || before?.UploadBindings?.Count>0)
        {
            await using var binding=db.CreateCommand("INSERT INTO public.platform_template_upload_bindings(template_id,tenant_id,bindings) VALUES($1,$2,$3::jsonb) ON CONFLICT(template_id) DO UPDATE SET bindings=EXCLUDED.bindings");
            binding.Parameters.AddWithValue(target);binding.Parameters.AddWithValue(actor.Tenant);binding.Parameters.AddWithValue(JsonSerializer.Serialize(uploadBindings));await binding.ExecuteNonQueryAsync(ct);
        }
        await SupersedeAsync(db,target,ct); await AuditAsync(db,actor,target,before is null ? "Create":"Update",ct);
        return new(TemplateStatus.Success,Id:target);
    }

    // Reuse ordinary validation inside the restore transaction.
    internal Task<TemplateResult> RestoreAsync(PostgresTransactionContext db,int tenant,int user,string name,int id,TemplateInput input,CancellationToken ct)
        => SaveCoreAsync(db,new Actor(tenant,user,name),id,input,ct);

    public Task<TemplateResult> DeleteAsync(VerifiedSession? session,int id,string? revision,CancellationToken ct=default)=>
        RunAsync(session,true,async(db,actor,token)=>
        {
            var target=(await LoadAsync(db,actor.Tenant,id,token)).SingleOrDefault();
            if(target is null) return new(TemplateStatus.NotFound);
            if(target.System) return new(TemplateStatus.Forbidden,Detail:"Cannot delete a system template. It is required for the application to function.");
            if(target.Origin!="native") return Review();
            if(revision!=target.ConfigurationKey) return Stale();
            if(await HasDataAsync(db,id,token)||await HasUploadsAsync(db,id,token)||await HasExternalReferencesAsync(db,id,token)
                || ((savedMappings?.Value.Enabled==true || await AuditPlatform.Api.Mappings.TenantMappingService.StoreExistsAsync(db,token))
                    && await ExistsAsync(db,"SELECT EXISTS(SELECT 1 FROM public.platform_tenant_saved_mappings WHERE template_id=$1)",id,token))) return Review();
            await using var command=db.CreateCommand("UPDATE public.templates SET is_active=FALSE WHERE id=$1");
            command.Parameters.AddWithValue(id); await command.ExecuteNonQueryAsync(token);
            // Retain mappings and permanent bindings on soft deletion.
            await SupersedeAsync(db,id,token); await AuditAsync(db,actor,id,"Delete",token);
            return new(TemplateStatus.Success,Id:id);
        },ct);

    public Task<TemplateResult> ReorderAsync(VerifiedSession? session,int id,IReadOnlyList<int> columns,string? revision,CancellationToken ct=default)=>
        ChangeMappingsAsync(session,id,revision,t=>columns.Count==t.Columns.Count&&columns.Order().SequenceEqual(t.Columns.Select(c=>c.Id).Order()) ? columns:null,ct);
    public Task<TemplateResult> RemoveAsync(VerifiedSession? session,int id,int column,string? revision,CancellationToken ct=default)=>
        ChangeMappingsAsync(session,id,revision,t=>t.Columns.Any(c=>c.Id==column) ? t.Columns.Where(c=>c.Id!=column).Select(c=>c.Id).ToArray():null,ct);
    private Task<TemplateResult> ChangeMappingsAsync(VerifiedSession? session,int id,string? revision,
        Func<TemplateDefinition,IReadOnlyList<int>?> change,CancellationToken ct)=>RunAsync(session,true,async(db,actor,token)=>
        {
            var before=(await LoadAsync(db,actor.Tenant,id,token)).SingleOrDefault();
            if(before is null) return new(TemplateStatus.NotFound);
            if(revision!=before.ConfigurationKey) return Stale();
            var columns=change(before); if(columns is null) return Invalid("Supply the existing template column IDs exactly once.");
            return await SaveCoreAsync(db,actor,id,new(before.Name,columns,before.Purpose,before.Ledger,RateSource:before.RateSource,
                Subtotals:before.Subtotals,Revision:revision),token);
        },ct);

    public Task<TemplateResult> LedgerPreviewAsync(VerifiedSession? session,CancellationToken ct=default)=>
        RunAsync(session,false,async(db,actor,token)=>
        {
            var columns=await SelectColumnsAsync(db,actor.Tenant,null,token,true);
            var choices=new List<TemplateColumn>();
            foreach(var (role,name,type) in LedgerRecipe)
            {
                var matches=columns.Where(c=>c.Name.Trim().Equals(name,StringComparison.OrdinalIgnoreCase)).ToArray();
                if(matches.Length>1 || matches.Any(c=>!c.Active||!LedgerType(role,c.Type))) return Review();
                choices.Add(matches.SingleOrDefault()??new(0,name,"",type,"Financial",null,null,false,choices.Count,0,true));
            }
            return new(TemplateStatus.Success,[new(0,"Account Ledger",actor.Name,DateTime.UtcNow,false,false,"account_ledger",[],null,"standard","native",0,choices)]);
        },ct);

    public async Task<TemplateResult> PrepareLedgerAsync(VerifiedSession? session,string name,Dictionary<string,int?> roles,
        Dictionary<string,bool>? comparisons,bool subtotals,CancellationToken ct=default)
    {
        try { return await RunAsync(session,true,async(db,actor,token)=>
        {
            if(!roles.Keys.Order().SequenceEqual(LedgerRecipe.Select(r=>r.Role).Order())
                || comparisons?.Keys.Except(roles.Keys).Any()==true || string.IsNullOrWhiteSpace(name)||name.Trim().Length>200)
                return Invalid("Supply a template name and all six ledger column roles.");
            var available=await SelectColumnsAsync(db,actor.Tenant,null,token,true);
            var selected=new Dictionary<string,int>(); var missing=new List<(string Role,string Name,string Type)>();
            foreach(var recipe in LedgerRecipe)
            {
                var explicitId=roles[recipe.Role];
                var matches=explicitId is null ? available.Where(c=>c.Name.Trim().Equals(recipe.Name,StringComparison.OrdinalIgnoreCase)).ToArray()
                    : available.Where(c=>c.Id==explicitId).ToArray();
                if(matches.Length>1 || matches.Any(c=>!c.Active||!LedgerType(recipe.Role,c.Type)) || (explicitId is not null && matches.Length!=1))
                    return Invalid("Select compatible active columns for all ledger roles.");
                if(matches.Length==1) selected[recipe.Role]=matches[0].Id;
                else missing.Add(recipe);
            }
            if(selected.Values.Distinct().Count()!=selected.Count) return Invalid("Configure six distinct ledger column roles.");
            var existing=(await LoadAsync(db,actor.Tenant,null,token)).SingleOrDefault(t=>t.Name.Equals(name.Trim(),StringComparison.OrdinalIgnoreCase));
            if(existing is not null)
            {
                if(missing.Count!=0 || existing.Purpose!="account_ledger" || !SameLedger(existing.Ledger,selected)
                    || existing.Columns.Count!=6 || existing.Columns.Any(c=>c.Review || !selected.ContainsValue(c.Id)
                        || (comparisons?.TryGetValue(selected.FirstOrDefault(k=>k.Value==c.Id).Key??"",out var chosen)==true && chosen!=EffectiveComparison(c,existing))))
                    return new(TemplateStatus.Review,Detail:"This template already exists with different settings. Open it for review.");
                return new(TemplateStatus.Success,Id:existing.Id,Reused:true);
            }
            // Validate everything possible before inserting; all new columns,
            // mappings, template and mandatory audit share this transaction.
            foreach(var (role,label,type) in missing)
            {
                await using var column=db.CreateCommand("INSERT INTO public.master_columns(column_name,data_type,category) VALUES($1,$2,'General') RETURNING id");
                column.Parameters.AddWithValue("col_"+Guid.NewGuid().ToString("N")); column.Parameters.AddWithValue(type);
                int cid=(int)(await column.ExecuteScalarAsync(token))!;
                await using var definition=db.CreateCommand("INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,category,binding_origin) VALUES($1,$2,$3,'Financial','native')");
                definition.Parameters.AddWithValue(cid); definition.Parameters.AddWithValue(actor.Tenant); definition.Parameters.AddWithValue(label); await definition.ExecuteNonQueryAsync(token);
                selected[role]=cid;
            }
            var comparisonIds=comparisons?.ToDictionary(k=>selected[k.Key],v=>v.Value);
            var result=await SaveCoreAsync(db,actor,null,new(name,LedgerRecipe.Select(r=>selected[r.Role]).ToArray(),"account_ledger",selected,
                Comparisons:comparisonIds,Subtotals:subtotals),token);
            if(result.Status!=TemplateStatus.Success) throw new InvalidOperationException("Prepared ledger did not pass its prevalidated configuration.");
            return result;
        },ct); }
        catch(PostgresException e) when(e.SqlState==PostgresErrorCodes.UniqueViolation)
        { return new(TemplateStatus.Conflict,Detail:"A template or column with this name already exists; review the existing definition."); }
    }

    public static bool EffectiveComparison(TemplateColumn column,TemplateDefinition template)=>column.Compare
        ??!(template.Purpose=="account_ledger"&&template.Ledger.GetValueOrDefault("balance")==column.Id);
    internal static string? Validate(IReadOnlyList<TemplateColumn> columns,string purpose,Dictionary<string,int> ledger)
    {
        foreach(var c in columns)
        {
            if(c.Role is not (null or "document_id" or "document_date" or "footing")) return "Unknown reconciliation role.";
            if(c.Role=="document_id"&&c.Type!="Text" || c.Role=="document_date"&&c.Type!="Date" || c.Role=="footing"&&c.Type!="Numeric")
                return "Document ID requires Text, Document Date requires Date, and Footing requires Numeric.";
        }
        if(columns.Count(c=>c.Role=="document_id")>1) return "Use one Document ID column per template; it can contain many document values.";
        if(columns.Count(c=>c.Role=="document_date")>1) return "Use one Document Date column per template.";
        if(purpose=="standard") return ledger.Count==0 ? null:"Ledger column roles require an Account Ledger template";
        if(purpose!="account_ledger") return "Invalid template data purpose";
        if(!ledger.Keys.Order().SequenceEqual(LedgerRecipe.Select(r=>r.Role).Order())||ledger.Values.Distinct().Count()!=6)
            return "Configure distinct Voucher, Date, Description, Debit, Credit and Balance columns";
        if(columns[0].Id!=ledger["voucher"]) return "The ledger Voucher column must remain the first template column";
        if(ledger.Values.Except(columns.Select(c=>c.Id)).Any()) return "Ledger column roles must belong to the selected template columns";
        if(ledger.Any(r=>!LedgerType(r.Key,columns.Single(c=>c.Id==r.Value).Type))) return "Ledger column data types are incompatible; do not change shared master columns";
        return DocumentColumn(columns)?.Id!=ledger["voucher"] ? "The Document ID must match the configured ledger Voucher column.":null;
    }
    private static bool LedgerType(string role,string type)=>role switch {
        "voucher" or "description"=>type=="Text","date"=>type=="Date","balance"=>type is "Text" or "Numeric",_=>type=="Numeric"};
    private static TemplateColumn? DocumentColumn(IReadOnlyList<TemplateColumn> columns)=>columns.SingleOrDefault(c=>c.Role=="document_id")??columns.FirstOrDefault();
    private static TemplateColumn? DateColumn(IReadOnlyList<TemplateColumn> columns)=>columns.SingleOrDefault(c=>c.Role=="document_date")??columns.FirstOrDefault(c=>c.Type=="Date");
    internal static string KindForNew(string name)=>name.Trim().ToLowerInvariant() switch {"purchase order"=>"purchase_order","store receiving"=>"store_receiving","vendor bill"=>"vendor_bill",_=>"standard"};
    private static bool SameLedger(Dictionary<string,int> a,Dictionary<string,int> b)=>a.Count==b.Count&&a.All(k=>b.TryGetValue(k.Key,out var value)&&value==k.Value);
    private static TemplateResult Review()=>new(TemplateStatus.Review,Detail:ReviewMessage);
    private static TemplateResult Stale()=>new(TemplateStatus.Review,Detail:"Template or column definitions changed. Reload the template before saving.");
    private static TemplateResult Invalid(string detail)=>new(TemplateStatus.Invalid,Detail:detail);
    private static void AddText(NpgsqlCommand command,string? value)=>command.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)value??DBNull.Value);

    private Task<TemplateResult> RunAsync(VerifiedSession? session,bool admin,
        Func<PostgresTransactionContext,Actor,CancellationToken,Task<TemplateResult>> operation,CancellationToken ct)
    {
        if(session is null||!session.TenantContextRequired||session.TenantId is not >0||!AuthenticationService.ValidToken(session.Token))
            return Task.FromResult(new TemplateResult(TemplateStatus.Forbidden));
        return transactions.ExecuteAsync(async(db,token)=>
        {
            await using(var config=db.CreateCommand("SELECT pg_advisory_xact_lock(746102)")) await config.ExecuteNonQueryAsync(token);
            await using(var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE"))
            {
                tenant.Parameters.AddWithValue(session.TenantId.Value); if(await tenant.ExecuteScalarAsync(token) is null) return new TemplateResult(TemplateStatus.Forbidden);
            }
            Actor actor;
            await using(var command=db.CreateCommand("""
                SELECT u.id,u.username,m.tenant_role FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                    AND s.selected_tenant_id=$3 AND s.context_version=$4 AND m.tenant_id=$3 AND m.is_active IS TRUE FOR SHARE OF s,u,m
                """))
            {
                command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
                command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion);
                await using var reader=await command.ExecuteReaderAsync(token);
                if(!await reader.ReadAsync(token)||(admin&&reader.GetString(2)!="admin")) return new TemplateResult(TemplateStatus.Forbidden);
                actor=new(session.TenantId.Value,reader.GetInt32(0),reader.GetString(1));
            }
            return await operation(db,actor,token);
        },ct);
    }

    internal static string? UploadDerivedBindingError(IReadOnlyList<TemplateColumn> columns,Dictionary<string,int> bindings)
    {
        string[] roles=["amount","quantity","rate","account_code","account_name","debit","credit","description","line_id"];
        if(bindings.Keys.Except(roles,StringComparer.Ordinal).Any()) return "Unknown upload field role.";
        foreach(var pair in bindings)
        {
            var column=columns.SingleOrDefault(c=>c.Id==pair.Value);
            if(column is null||!column.Active||column.Review) return "Upload bindings require active selected columns.";
            if(new[]{"amount","quantity","rate","debit","credit"}.Contains(pair.Key)&&column.Type!="Numeric") return "Amount/quantity/rate/debit/credit bindings require Numeric columns.";
            if(new[]{"account_code","line_id"}.Contains(pair.Key)&&column.Type!="Text") return "Identifiers require Text columns.";
        }
        if(bindings.ContainsKey("account_code")!=bindings.ContainsKey("debit")||bindings.ContainsKey("debit")!=bindings.ContainsKey("credit")) return "Configure account, debit and credit together.";
        if(bindings.TryGetValue("rate",out int rate)&&new[]{"amount","quantity"}.Any(r=>bindings.GetValueOrDefault(r)==rate)) return "Rate must have its own column.";
        if(bindings.TryGetValue("debit",out int debit)&&bindings.GetValueOrDefault("credit")==debit) return "Debit and credit require distinct columns.";
        return null;
    }

    // Shared by ordinary saves and backup preview/restore. Preview must not
    // promise a binding change which existing current/history/uploads protect.
    internal static async Task<string?> UploadBindingReviewAsync(PostgresTransactionContext db,TemplateDefinition? before,Dictionary<string,int> bindings,CancellationToken ct)
    {
        if(bindings.Count>0)
        {
            await using var catalog=db.CreateCommand("SELECT to_regclass('public.platform_template_upload_bindings') IS NOT NULL");
            if(await catalog.ExecuteScalarAsync(ct) is not true) return "Accounting field bindings require the reviewed native schema.";
        }
        if(before is not null&&!JsonSerializer.Serialize(bindings.OrderBy(p=>p.Key,StringComparer.Ordinal)).Equals(
            JsonSerializer.Serialize((before.UploadBindings??[]).OrderBy(p=>p.Key,StringComparer.Ordinal)),StringComparison.Ordinal)
            &&(await HasDataAsync(db,before.Id,ct)||await HasUploadsAsync(db,before.Id,ct))) return ReviewMessage;
        return null;
    }

    // Operator previews use a read-only snapshot; ordinary runtime callers keep
    // the existing row locks that protect their following configuration writes.
    internal static async Task<List<TemplateDefinition>> LoadAsync(PostgresTransactionContext db,int tenant,int? id,CancellationToken ct,bool lockRows=true)
    {
        var rows=new List<TemplateDefinition>();
        await using(var command=db.CreateCommand("""
            SELECT t.id,d.display_name,t.created_by,t.created_date,t.is_system,t.has_subtotals_by_default,t.data_purpose,t.ledger_columns,
                t.rate_source,d.template_kind,d.binding_origin,d.definition_version
            FROM public.templates t JOIN public.platform_tenant_templates d ON d.template_id=t.id
            WHERE d.tenant_id=$1 AND t.is_active IS TRUE AND ($2::integer IS NULL OR t.id=$2) ORDER BY d.display_name
            """+(lockRows?" FOR SHARE OF t,d":"")))
        {
            command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(NpgsqlDbType.Integer,(object?)id??DBNull.Value);
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct)) rows.Add(new(reader.GetInt32(0),reader.GetString(1),reader.IsDBNull(2)?"":reader.GetString(2),reader.GetDateTime(3),
                !reader.IsDBNull(4)&&reader.GetBoolean(4),!reader.IsDBNull(5)&&reader.GetBoolean(5),reader.GetString(6),JsonSerializer.Deserialize<Dictionary<string,int>>(reader.GetString(7))??[],
                reader.IsDBNull(8)?null:reader.GetString(8),reader.GetString(9),reader.GetString(10),reader.GetInt64(11),[]));
        }
        var columns=new Dictionary<int,List<TemplateColumn>>();
        await using(var command=db.CreateCommand("""
            SELECT p.template_id,m.id,c.display_name,p.storage_key,m.data_type,c.category,p.reconciliation_role,p.compare_for_changes,
                p.comparison_review_required,p.sort_order,c.definition_version,m.is_active
            FROM public.template_column_mappings p JOIN public.platform_tenant_templates d ON d.template_id=p.template_id AND d.tenant_id=p.tenant_id
            JOIN public.platform_tenant_columns c ON c.tenant_id=p.tenant_id AND c.column_id=p.column_id
            JOIN public.master_columns m ON m.id=p.column_id AND m.column_name=p.storage_key
            WHERE d.tenant_id=$1 AND ($2::integer IS NULL OR p.template_id=$2) ORDER BY p.template_id,p.sort_order
            """+(lockRows?" FOR SHARE OF p,c,m":"")))
        {
            command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(NpgsqlDbType.Integer,(object?)id??DBNull.Value);
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                int tid=reader.GetInt32(0); if(!columns.TryGetValue(tid,out var list)) columns[tid]=list=[];
                list.Add(new(reader.GetInt32(1),reader.GetString(2),reader.GetString(3),reader.GetString(4),reader.GetString(5),reader.IsDBNull(6)?null:reader.GetString(6),
                    reader.IsDBNull(7)?null:reader.GetBoolean(7),reader.GetBoolean(8),reader.GetInt32(9),reader.GetInt64(10),!reader.IsDBNull(11)&&reader.GetBoolean(11)));
            }
        }
        var bindings=new Dictionary<int,Dictionary<string,int>>();
        await using(var catalog=db.CreateCommand("SELECT to_regclass('public.platform_template_upload_bindings') IS NOT NULL"))
        if(await catalog.ExecuteScalarAsync(ct) is true)
        {
            await using var command=db.CreateCommand("SELECT template_id,bindings::text FROM public.platform_template_upload_bindings WHERE tenant_id=$1 AND ($2::integer IS NULL OR template_id=$2)"+(lockRows?" FOR SHARE":""));
            command.Parameters.AddWithValue(tenant);command.Parameters.AddWithValue(NpgsqlDbType.Integer,(object?)id??DBNull.Value);
            await using var reader=await command.ExecuteReaderAsync(ct);while(await reader.ReadAsync(ct)) bindings.Add(reader.GetInt32(0),JsonSerializer.Deserialize<Dictionary<string,int>>(reader.GetString(1))!);
        }
        return rows.Select(t=>t with {Columns=columns.GetValueOrDefault(t.Id)??[],UploadBindings=bindings.GetValueOrDefault(t.Id)}).ToList();
    }
    private static async Task<List<TemplateColumn>> SelectColumnsAsync(PostgresTransactionContext db,int tenant,IReadOnlyList<int>? ids,CancellationToken ct,bool includeInactive=false)
    {
        await using var command=db.CreateCommand("""
            SELECT m.id,c.display_name,m.column_name,m.data_type,c.category,c.definition_version,m.is_active
            FROM public.platform_tenant_columns c JOIN public.master_columns m ON m.id=c.column_id
            WHERE c.tenant_id=$1 AND c.binding_origin='native' AND (m.is_active IS TRUE OR $3) AND ($2::integer[] IS NULL OR m.id=ANY($2)) ORDER BY m.id FOR SHARE OF c,m
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Integer,(object?)ids?.ToArray()??DBNull.Value);
        command.Parameters.AddWithValue(includeInactive);
        var rows=new List<TemplateColumn>(); await using var reader=await command.ExecuteReaderAsync(ct);
        while(await reader.ReadAsync(ct)) rows.Add(new(reader.GetInt32(0),reader.GetString(1),reader.GetString(2),reader.GetString(3),reader.GetString(4),null,null,false,0,reader.GetInt64(5),!reader.IsDBNull(6)&&reader.GetBoolean(6)));
        return rows;
    }
    private static Task<bool> HasDataAsync(PostgresTransactionContext db,int id,CancellationToken ct)=>ExistsAsync(db,"SELECT EXISTS(SELECT 1 FROM public.client_data WHERE template_id=$1) OR EXISTS(SELECT 1 FROM public.client_data_history WHERE template_id=$1)",id,ct);
    private static Task<bool> HasUploadsAsync(PostgresTransactionContext db,int id,CancellationToken ct)=>ExistsAsync(db,"SELECT EXISTS(SELECT 1 FROM public.uploaded_files WHERE template_id=$1)",id,ct);
    private static Task<bool> HasExternalReferencesAsync(PostgresTransactionContext db,int id,CancellationToken ct)=>ExistsAsync(db,"SELECT EXISTS(SELECT 1 FROM public.audit_rule_documents WHERE template_id=$1) OR EXISTS(SELECT 1 FROM public.saved_column_mappings WHERE template_id=$1)",id,ct);
    private static async Task<bool> ExistsAsync(PostgresTransactionContext db,string sql,int id,CancellationToken ct)
    { await using var command=db.CreateCommand(sql); command.Parameters.AddWithValue(id); return (bool)(await command.ExecuteScalarAsync(ct))!; }
    private static async Task<bool> ChangesIdentitiesAsync(PostgresTransactionContext db,int id,string? oldKey,string? newKey,CancellationToken ct)
    {
        await using var command=db.CreateCommand("""
            SELECT EXISTS(SELECT 1 FROM public.client_data WHERE template_id=$1 AND
                (NULLIF(btrim(row_data->>$2),'') IS DISTINCT FROM NULLIF(btrim(row_data->>$3),'') OR NULLIF(btrim(row_data->>$3),'') IS NULL))
            OR EXISTS(SELECT 1 FROM public.client_data_history WHERE template_id=$1 AND
                (NULLIF(btrim(row_data->>$2),'') IS DISTINCT FROM NULLIF(btrim(row_data->>$3),'') OR NULLIF(btrim(row_data->>$3),'') IS NULL))
            """);
        command.Parameters.AddWithValue(id); AddText(command,oldKey); AddText(command,newKey); return (bool)(await command.ExecuteScalarAsync(ct))!;
    }
    private static async Task SupersedeAsync(PostgresTransactionContext db,int id,CancellationToken ct)
    { await using var command=db.CreateCommand("UPDATE public.upload_validation_plans SET status='superseded' WHERE template_id=$1 AND status='ready'"); command.Parameters.AddWithValue(id); await command.ExecuteNonQueryAsync(ct); }
    private Task<int> AuditAsync(PostgresTransactionContext db,Actor actor,int id,string action,CancellationToken ct)=>audit.WriteAsync(db,
        new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),new LogActor("authenticated",actor.User,actor.Name),null,action,"TenantTemplates",
            JsonSerializer.SerializeToElement(new {tenant_id=actor.Tenant,record_id=id})),ct);
}
