using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.TrialBalance;

internal sealed class TrialBalanceStorageStore(IOptions<TrialBalanceStorageOptions> options)
{
    internal static string Quote(string text)=>"\""+text.Replace("\"","\"\"",StringComparison.Ordinal)+"\"";
    internal static string Hash(string text)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    internal static readonly string[] AmountColumns=["opening_dr","opening_cr","period_dr","period_cr","closing_dr","closing_cr"];
    internal static readonly string[] DeleteTables=["trial_balance_rollforward_adjustments","trial_balance_profit_status","trial_balance_accounts","trial_balance_uploads"];
    private static readonly Dictionary<string,string> Guards=new(StringComparer.Ordinal){
        ["platform_trial_balance_operations"]="guard_trial_balance_operation",["platform_trial_balance_uploads"]="guard_trial_balance_ownership",
        ["trial_balance_uploads"]="guard_native_trial_balance_upload",["trial_balance_accounts"]="guard_native_trial_balance_account",
        ["trial_balance_rollforward_adjustments"]="guard_native_trial_balance_adjustment"};

    internal static async Task<TrialBalanceStorageActor> Authorize(PostgresTransactionContext db,VerifiedSession? session,int client,bool writing,CancellationToken ct)
    {
        if(session is null||!session.TenantContextRequired||session.TenantId is not >0||client<1||!AuthenticationService.ValidToken(session.Token))
            throw new TrialBalanceStorageRejected(session is null?"unauthenticated":"forbidden","tb_access_denied");
        if(writing)
        {
            await using(var gate=db.CreateCommand("SELECT pg_advisory_xact_lock(746102)")) await gate.ExecuteNonQueryAsync(ct);
            await using(var gate=db.CreateCommand("SELECT pg_advisory_xact_lock(hashtext($1))")) {gate.Parameters.AddWithValue("trial-balance-storage:"+client);await gate.ExecuteNonQueryAsync(ct);}
            await using var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE");
            tenant.Parameters.AddWithValue(session.TenantId.Value);if(await tenant.ExecuteScalarAsync(ct) is null) throw new TrialBalanceStorageRejected("forbidden","tb_access_denied");
        }
        string locks=writing?" FOR SHARE OF s,u,m,t,c,h":"";
        await using var command=db.CreateCommand("""
            SELECT u.id,u.username FROM public.sessions s JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.tenant_id=$3
            JOIN public.platform_tenants t ON t.id=m.tenant_id
            JOIN public.clients c ON c.id=s.selected_client_id
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=t.id
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
              AND m.is_active IS TRUE AND m.tenant_role='admin' AND t.is_active IS TRUE AND c.is_active IS TRUE
              AND s.selected_tenant_id=$3 AND s.context_version=$4 AND s.selected_client_id=$5
            """+locks);
        command.Parameters.AddWithValue(session.Token);command.Parameters.AddWithValue(session.UserId);command.Parameters.AddWithValue(session.TenantId.Value);
        command.Parameters.AddWithValue(session.ContextVersion);command.Parameters.AddWithValue(client);await using var reader=await command.ExecuteReaderAsync(ct);
        if(!await reader.ReadAsync(ct)) throw new TrialBalanceStorageRejected("forbidden","tb_access_denied");
        return new(session.TenantId.Value,reader.GetInt32(0),reader.GetString(1));
    }

    internal async Task<TrialBalanceStorageSnapshot> Capture(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,
        string kind,TrialBalancePeriod? period,int? upload,string[] codes,CancellationToken ct)
    {
        await using(var settings=db.CreateCommand("SET LOCAL timezone='UTC'; SET LOCAL datestyle='ISO,YMD'; SET LOCAL extra_float_digits=1")) await settings.ExecuteNonQueryAsync(ct);
        var tables=new HashSet<string>(StringComparer.Ordinal);var blockers=new List<string>();var uploads=new List<TrialBalanceStoredUpload>();var accountIds=new List<int>();
        var owned=new HashSet<int>();int accountCount=0;int[] ids=[];long rows=0,bytes=0;
        using var digest=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        void Add(string text)
        {byte[] value=Encoding.UTF8.GetBytes(text);bytes+=value.Length;if(bytes>options.Value.MaximumSnapshotBytes) throw new TrialBalanceStorageRejected("too_large","tb_snapshot_capacity");digest.AppendData(BitConverter.GetBytes(value.Length));digest.AppendData(value);}
        async Task Scan(string table,string key,string predicate,Action<JsonElement>? inspect=null,bool optional=false)
        {
            if(!await Exists(db,table,ct)) {if(optional)return;throw new TrialBalanceStorageRejected("review","tb_schema_review_required");}
            tables.Add(table);Add(table);
            await using var command=Scoped(db,$"SELECT t.{Quote(key)}::text,left(to_jsonb(t)::text,@cellcap) FROM public.{Quote(table)} t WHERE {predicate} ORDER BY t.{Quote(key)} LIMIT @rowcap",client,actor.Tenant,ids,codes,period,upload);
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                if(++rows>options.Value.MaximumSnapshotRows) throw new TrialBalanceStorageRejected("too_large","tb_snapshot_row_capacity");
                string raw=reader.GetString(1);if(raw.Length>Math.Min(1048576,options.Value.MaximumSnapshotBytes)) throw new TrialBalanceStorageRejected("too_large","tb_snapshot_cell_capacity");
                Add(reader.GetString(0));Add(raw);using var json=JsonDocument.Parse(raw);inspect?.Invoke(json.RootElement);
            }
        }
        await Scan("trial_balance_uploads","id",kind=="import"?"t.client_id=@client AND t.period_start=CAST(@start AS date) AND t.period_end=CAST(@end AS date)":"t.client_id=@client AND t.id=@upload",r=>{
            if(uploads.Count>=options.Value.MaximumUploadsPerClient) throw new TrialBalanceStorageRejected("too_large","tb_upload_capacity");
            string? Text(string name)=>r.GetProperty(name).ValueKind==JsonValueKind.Null?null:r.GetProperty(name).GetString();
            uploads.Add(new(r.GetProperty("id").GetInt32(),Text("file_name")??"",Text("status")??"",r.GetProperty("row_count").ValueKind==JsonValueKind.Null?0:r.GetProperty("row_count").GetInt32(),Text("period_start"),Text("period_end"),false));
        });
        if(kind=="delete"&&uploads.Count!=1) throw new TrialBalanceStorageRejected("not_found","tb_upload_not_found");
        ids=uploads.Select(u=>u.Id).ToArray();
        await Scan("platform_trial_balance_uploads","upload_id","t.upload_id=ANY(@ids)",r=>{
            if(r.GetProperty("tenant_id").GetInt32()!=actor.Tenant||r.GetProperty("client_id").GetInt32()!=client) blockers.Add("tb_ownership_review_required");
            else owned.Add(r.GetProperty("upload_id").GetInt32());
        });
        uploads=uploads.Select(u=>u with {NativeOwned=owned.Contains(u.Id)}).ToList();
        if(uploads.Any(u=>!u.NativeOwned)) blockers.Add("tb_legacy_ownership_review_required");
        await Scan("trial_balance_accounts","id","t.upload_id=ANY(@ids)",r=>{accountIds.Add(r.GetProperty("id").GetInt32());accountCount++;});
        var accountSet=accountIds.ToHashSet();
        await Scan("trial_balance_rollforward_adjustments","id","t.upload_id=ANY(@ids)",r=>{
            if(!accountSet.Contains(r.GetProperty("account_id").GetInt32())) blockers.Add("tb_adjustment_scope_review_required");
        });
        await Scan("trial_balance_profit_status","upload_id","t.upload_id=ANY(@ids)",optional:true);
        foreach(var mapping in TrialBalanceAssignmentStore.Tables)
        {
            bool lastDelete=kind=="delete"&&mapping.Name=="account_category_assignments";
            await Scan(mapping.Name,"id",lastDelete?"t.client_id=@client":"t.client_id=@client AND t.account_code=ANY(@codes)");
        }
        int clientUploads=checked((int)await Count(db,"SELECT count(*) FROM public.trial_balance_uploads WHERE client_id=$1",client,ct));
        int operations=checked((int)await Count(db,"SELECT count(*) FROM public.platform_trial_balance_operations WHERE client_id=$1",client,ct));
        Add("client_uploads:"+clientUploads);Add("operations:"+operations);tables.Add("platform_trial_balance_operations");tables.Add("audit_log");
        if(kind=="import"&&clientUploads-ids.Length+1>options.Value.MaximumUploadsPerClient) blockers.Add("tb_upload_capacity");
        if(operations>=options.Value.MaximumOperationsPerClient) blockers.Add("tb_operation_capacity");
        if(ids.Length>0)
        {
            if(!await Exists(db,"account_set_versions",ct)) blockers.Add("tb_protection_schema_review_required");
            else await Scan("account_set_versions","id","t.source_upload_id=ANY(@ids)",_=>blockers.Add("tb_protected_account_set_version"));
        }
        var amountTypes=new Dictionary<string,string>(StringComparer.Ordinal);
        await using(var types=db.CreateCommand("SELECT a.attname::text,format_type(a.atttypid,a.atttypmod) FROM pg_attribute a WHERE a.attrelid='public.trial_balance_accounts'::regclass AND a.attnum>0 AND NOT a.attisdropped AND a.attname=ANY($1) ORDER BY a.attname"))
        {
            types.Parameters.AddWithValue(AmountColumns);await using var reader=await types.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                string type=reader.GetString(1);
                if(type is not("real" or "double precision" or "numeric")&&!Regex.IsMatch(type,@"^numeric\([0-9]{1,4},-?[0-9]{1,4}\)$",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100)))
                    blockers.Add("tb_amount_type_review_required");
                amountTypes[reader.GetString(0)]=type;Add("type:"+reader.GetString(0)+":"+type);
            }
        }
        if(amountTypes.Count!=6) throw new TrialBalanceStorageRejected("review","tb_amount_schema_review_required");
        // Actual incoming references protect even unknown cascading dependencies.
        var targets=new[]{"trial_balance_uploads","trial_balance_accounts","trial_balance_rollforward_adjustments","platform_trial_balance_uploads","trial_balance_profit_status"}
            .Concat(kind=="delete"&&clientUploads==ids.Length?new[]{"account_category_assignments"}:[]).Where(tables.Contains).ToArray();
        await using(var catalog=db.CreateCommand("""
            SELECT n.nspname::text,r.relname::text,t.relname::text,
              ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(num,pos) JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=k.num ORDER BY pos),
              ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY k(num,pos) JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.num ORDER BY pos),pg_get_constraintdef(c.oid)
            FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
            JOIN pg_class t ON t.oid=c.confrelid WHERE c.contype='f' AND c.confrelid IN(SELECT to_regclass(name) FROM unnest($1::text[]) name)
            ORDER BY c.oid LIMIT 257
            """))
        {
            catalog.Parameters.AddWithValue(targets.Select(t=>"public."+t).ToArray());
            var references=new List<(string Schema,string Table,string Target,string[] Keys,string[] Targets)>();
            await using(var reader=await catalog.ExecuteReaderAsync(ct))
                while(await reader.ReadAsync(ct))
                {if(references.Count>=options.Value.MaximumCatalogEntries) throw new TrialBalanceStorageRejected("review","tb_catalog_capacity");Add(reader.GetString(0)+reader.GetString(1)+reader.GetString(5));references.Add((reader.GetString(0),reader.GetString(1),reader.GetString(2),reader.GetFieldValue<string[]>(3),reader.GetFieldValue<string[]>(4)));}
            foreach(var reference in references)
            {
                if(reference.Schema!="public") throw new TrialBalanceStorageRejected("review","tb_external_reference_review_required");
                tables.Add(reference.Table);
                string join=string.Join(" AND ",reference.Keys.Zip(reference.Targets,(a,b)=>"r."+Quote(a)+"=t."+Quote(b)));
                string outside=targets.Contains(reference.Table)?"("+Predicate(reference.Table,"r")+") IS NOT TRUE":"TRUE";
                await using var check=Scoped(db,$"SELECT count(*) FROM public.{Quote(reference.Table)} r JOIN public.{Quote(reference.Target)} t ON {join} WHERE {Predicate(reference.Target,"t")} AND ({outside})",client,actor.Tenant,ids,codes,period,upload);
                long count=Convert.ToInt64(await check.ExecuteScalarAsync(ct));Add(reference.Table+":"+count);
                if(count>0) blockers.Add("tb_protected_or_cross_scope_reference");
            }
        }
        foreach(string table in tables.Order())
        {
            await using(var security=db.CreateCommand("SELECT row_security_active($1::regclass)"))
            {security.Parameters.AddWithValue("public."+Quote(table));if((bool)(await security.ExecuteScalarAsync(ct))!) blockers.Add("tb_row_security_review_required");}
            await using var triggers=db.CreateCommand("SELECT tgname::text,tgenabled::text,pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid=$1::regclass AND NOT tgisinternal ORDER BY tgname LIMIT 257");
            triggers.Parameters.AddWithValue("public."+Quote(table));var found=new HashSet<string>();int triggerCount=0;
            await using var reader=await triggers.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                if(++triggerCount>options.Value.MaximumCatalogEntries) throw new TrialBalanceStorageRejected("review","tb_catalog_capacity");
                string name=reader.GetString(0),enabled=reader.GetString(1);Add(table+":"+reader.GetString(2)+":"+enabled);
                if(enabled is "O" or "A") found.Add(name);
                if(enabled!="D"&&(!Guards.TryGetValue(table,out var expected)||name!=expected)) blockers.Add("tb_trigger_review_required");
            }
            if(Guards.TryGetValue(table,out var guard)&&!found.Contains(guard)) blockers.Add("tb_native_guard_review_required");
        }
        // Require the canonical identity constraint; row locks alone are not a substitute.
        await using(var unique=db.CreateCommand("""
            SELECT EXISTS(SELECT 1 FROM pg_constraint c WHERE c.conrelid='public.trial_balance_accounts'::regclass
              AND c.contype='u' AND c.convalidated AND NOT c.condeferrable
              AND ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(num,pos)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY pos)=ARRAY['upload_id','account_code'])
            """))
        {bool present=(bool)(await unique.ExecuteScalarAsync(ct))!;Add("identity:"+present);if(!present) blockers.Add("tb_identity_constraint_review_required");}
        return new(Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant(),uploads,blockers.Distinct().ToArray(),tables.Order().ToArray(),ids,accountIds.ToArray(),clientUploads,operations,accountCount,amountTypes);
    }
    internal static string Predicate(string table,string alias)=>table switch {
        "trial_balance_uploads"=>$"{alias}.id=ANY(@ids)","platform_trial_balance_uploads"=>$"{alias}.upload_id=ANY(@ids)",
        "account_category_assignments"=>$"{alias}.client_id=@client",
        "trial_balance_accounts" or "trial_balance_rollforward_adjustments" or "trial_balance_profit_status"=>$"{alias}.upload_id=ANY(@ids)",_=>"FALSE"};
    internal NpgsqlCommand Scoped(PostgresTransactionContext db,string sql,int client,int tenant,int[] ids,string[] codes,TrialBalancePeriod? period,int? upload)
    {
        var command=db.CreateCommand(sql);command.Parameters.AddWithValue("client",client);command.Parameters.AddWithValue("tenant",tenant);command.Parameters.AddWithValue("ids",ids);command.Parameters.AddWithValue("codes",codes);
        command.Parameters.AddWithValue("start",period?.Start??"");command.Parameters.AddWithValue("end",period?.End??"");command.Parameters.AddWithValue("upload",upload??0);
        command.Parameters.AddWithValue("rowcap",options.Value.MaximumSnapshotRows+1);command.Parameters.AddWithValue("cellcap",Math.Min(1048576,options.Value.MaximumSnapshotBytes)+1);return command;
    }
    internal static async Task<bool> Exists(PostgresTransactionContext db,string table,CancellationToken ct)
    {await using var command=db.CreateCommand("SELECT to_regclass($1) IS NOT NULL");command.Parameters.AddWithValue("public."+Quote(table));return (bool)(await command.ExecuteScalarAsync(ct))!;}
    internal static async Task<long> Count(PostgresTransactionContext db,string sql,int client,CancellationToken ct)
    {await using var command=db.CreateCommand(sql);command.Parameters.AddWithValue(client);return Convert.ToInt64(await command.ExecuteScalarAsync(ct));}
    internal static async Task<TrialBalanceStorageOutcome?> Outcome(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,Guid operation,string? requestHash,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT tenant_id,user_id,client_id,request_hash,state,outcome::text FROM public.platform_trial_balance_operations WHERE operation_id=$1");
        command.Parameters.AddWithValue(operation);await using var reader=await command.ExecuteReaderAsync(ct);if(!await reader.ReadAsync(ct)) return null;
        if(reader.GetInt32(0)!=actor.Tenant||reader.GetInt32(2)!=client||requestHash is not null&&(reader.GetInt32(1)!=actor.User||reader.GetString(3)!=requestHash))
            throw new TrialBalanceStorageRejected("conflict","tb_operation_reused");
        if(reader.GetString(4)!="completed") throw new TrialBalanceStorageRejected("review","tb_operation_incomplete");
        string json=reader.GetString(5);if(json.Length>16384) throw new TrialBalanceStorageRejected("review","tb_operation_outcome_review_required");
        return JsonSerializer.Deserialize<TrialBalanceStorageOutcome>(json,TrialBalanceStorageJson.Options)??throw new TrialBalanceStorageRejected("review","tb_operation_outcome_review_required");
    }
}
