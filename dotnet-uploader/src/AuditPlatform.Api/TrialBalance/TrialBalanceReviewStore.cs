using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.TrialBalance;

internal sealed class TrialBalanceReviewStore(IOptions<TrialBalanceReviewOptions> options)
{
    private readonly TrialBalanceStorageStore storage=new(Options.Create(new TrialBalanceStorageOptions {
        MaximumSnapshotRows=options.Value.MaximumSnapshotRows,MaximumSnapshotBytes=options.Value.MaximumSnapshotBytes}));
    internal const string Ledger="platform_trial_balance_review_operations";
    internal static readonly string[] Mutable=["account_code","parent_account_code","level","classification","manually_overridden"];
    internal static string Text(JsonElement row,string key)=>row.GetProperty(key).ValueKind==JsonValueKind.Null?"":row.GetProperty(key).ToString();
    internal async Task<TrialBalanceReviewSnapshot> Capture(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,int upload,
        IEnumerable<string>? proposed,CancellationToken ct)
    {
        await using(var settings=db.CreateCommand("SET LOCAL timezone='UTC'; SET LOCAL datestyle='ISO,YMD'; SET LOCAL extra_float_digits=1"))await settings.ExecuteNonQueryAsync(ct);
        long bytes=0;int count=0;
        void Budget(string raw){bytes+=Encoding.UTF8.GetByteCount(raw);if(raw.Length>1048576||bytes>options.Value.MaximumSnapshotBytes||++count>options.Value.MaximumSnapshotRows)throw new TrialBalanceStorageRejected("too_large","tb_review_snapshot_capacity");}
        JsonElement owner;
        await using(var get=db.CreateCommand("SELECT left(to_jsonb(u)::text,1048577) FROM public.trial_balance_uploads u WHERE id=$1 AND client_id=$2"))
        {get.Parameters.AddWithValue(upload);get.Parameters.AddWithValue(client);if(await get.ExecuteScalarAsync(ct) is not string raw)throw new TrialBalanceStorageRejected("not_found","tb_upload_not_found");Budget(raw);using var doc=JsonDocument.Parse(raw);owner=doc.RootElement.Clone();}
        if(Text(owner,"status")!="parsed")throw new TrialBalanceStorageRejected("review","tb_parsed_upload_required");
        using var protectedDigest=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        void Protect(string value){byte[] data=Encoding.UTF8.GetBytes(value);protectedDigest.AppendData(BitConverter.GetBytes(data.Length));protectedDigest.AppendData(data);}
        Protect(owner.GetRawText());var rows=new List<TrialBalanceReviewRow>();
        await using(var get=db.CreateCommand("SELECT left((to_jsonb(a)-'raw_row_data')::text,1048577),left((to_jsonb(a)-$2::text[])::text,1048577) FROM public.trial_balance_accounts a WHERE upload_id=$1 ORDER BY id LIMIT $3"))
        {
            get.Parameters.AddWithValue(upload);get.Parameters.AddWithValue(Mutable);get.Parameters.AddWithValue(options.Value.MaximumSnapshotRows+1);
            await using var reader=await get.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                string raw=reader.GetString(0),immutable=reader.GetString(1);Budget(raw);if(immutable.Length>1048576)throw new TrialBalanceStorageRejected("too_large","tb_review_cell_capacity");Protect(immutable);
                using var doc=JsonDocument.Parse(raw);var r=doc.RootElement;
                rows.Add(new(r.GetProperty("id").GetInt32(),Text(r,"account_code"),NullText(r,"source_account_code"),NullText(r,"account_name"),NullText(r,"parent_account_code"),r.GetProperty("level").GetInt32(),Text(r,"classification"),r.GetProperty("manually_overridden").GetBoolean(),r.GetProperty("sort_order").GetInt32(),r.Clone()));
            }
        }
        string[] codes=rows.Select(r=>r.Code).Concat(proposed??[]).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        var evidence=await storage.Capture(db,actor,client,"delete",null,upload,codes,ct);
        var adjustments=new List<JsonElement>();
        await using(var get=db.CreateCommand("SELECT left(to_jsonb(x)::text,1048577) FROM public.trial_balance_rollforward_adjustments x WHERE upload_id=$1 ORDER BY id LIMIT $2"))
        {get.Parameters.AddWithValue(upload);get.Parameters.AddWithValue(options.Value.MaximumSnapshotRows+1);await using var reader=await get.ExecuteReaderAsync(ct);while(await reader.ReadAsync(ct)){string raw=reader.GetString(0);Budget(raw);Protect(raw);using var doc=JsonDocument.Parse(raw);adjustments.Add(doc.RootElement.Clone());}}
        var assignments=new Dictionary<string,IReadOnlyList<JsonElement>>(StringComparer.Ordinal);
        foreach(var table in TrialBalanceAssignmentStore.Tables)
        {
            var values=new List<JsonElement>();
            await using var get=db.CreateCommand($"SELECT left(to_jsonb(a)::text,1048577) FROM public.{TrialBalanceStorageStore.Quote(table.Name)} a WHERE client_id=$1 AND account_code=ANY($2) ORDER BY id LIMIT $3");
            get.Parameters.AddWithValue(client);get.Parameters.AddWithValue(codes);get.Parameters.AddWithValue(options.Value.MaximumSnapshotRows+1);
            await using var reader=await get.ExecuteReaderAsync(ct);while(await reader.ReadAsync(ct)){string raw=reader.GetString(0);Budget(raw);using var doc=JsonDocument.Parse(raw);values.Add(doc.RootElement.Clone());}
            assignments[table.Name]=values;
        }
        JsonElement controls;
        try {using var doc=JsonDocument.Parse(Text(owner,"validation_controls") is {Length:>0} c?c:"{}");controls=doc.RootElement.ValueKind==JsonValueKind.Object?doc.RootElement.Clone():JsonSerializer.SerializeToElement(new {});}
        catch(JsonException){controls=JsonSerializer.SerializeToElement(new {});}
        var blockers=evidence.Blockers.ToList();
        int operations=checked((int)await TrialBalanceStorageStore.Count(db,"SELECT count(*) FROM public.platform_trial_balance_review_operations WHERE client_id=$1",client,ct));
        if(operations>=options.Value.MaximumOperationsPerClient)blockers.Add("tb_review_operation_capacity");
        // Fingerprint the actual guard definitions, not just their names.
        var definitions=new List<string>();
        await using(var guard=db.CreateCommand("""
            SELECT p.proname::text,left(pg_get_functiondef(p.oid),1048577) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND p.proname IN('guard_native_trial_balance_row','guard_trial_balance_review_operation') ORDER BY p.proname
            """))
        {await using var reader=await guard.ExecuteReaderAsync(ct);while(await reader.ReadAsync(ct)){string raw=reader.GetString(1);Budget(raw);definitions.Add(raw);}}
        if(definitions.Count!=2)blockers.Add("tb_review_guard_required");
        await using(var guard=db.CreateCommand("""
            SELECT tgname::text,tgenabled::text FROM pg_trigger WHERE tgrelid='public.platform_trial_balance_review_operations'::regclass AND NOT tgisinternal ORDER BY tgname
            """))
        {int guards=0;await using var reader=await guard.ExecuteReaderAsync(ct);while(await reader.ReadAsync(ct)){guards++;if(reader.GetString(0)!="guard_trial_balance_review_operation"||reader.GetString(1) is not("O" or "A"))blockers.Add("tb_review_guard_required");}if(guards!=1)blockers.Add("tb_review_guard_required");}
        await using(var security=db.CreateCommand("SELECT row_security_active('public.platform_trial_balance_review_operations'::regclass)"))
            if((bool)(await security.ExecuteScalarAsync(ct))!)blockers.Add("tb_review_row_security");
        string fingerprint=TrialBalanceStorageStore.Hash(evidence.Fingerprint+":"+operations+":"+string.Join('\n',definitions));
        return new(evidence,rows,adjustments,controls,owner,Convert.ToHexString(protectedDigest.GetHashAndReset()).ToLowerInvariant(),operations,assignments,fingerprint,blockers.Distinct().ToArray());
    }
    private static string? NullText(JsonElement row,string key)=>row.GetProperty(key).ValueKind==JsonValueKind.Null?null:row.GetProperty(key).GetString();
    internal static async Task<TrialBalanceReviewOutcome?> Outcome(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,int upload,Guid operation,string? hash,CancellationToken ct)
    {
        await using var get=db.CreateCommand("SELECT tenant_id,user_id,client_id,upload_id,request_hash,state,outcome::text FROM public.platform_trial_balance_review_operations WHERE operation_id=$1");get.Parameters.AddWithValue(operation);
        await using var reader=await get.ExecuteReaderAsync(ct);if(!await reader.ReadAsync(ct))return null;
        if(reader.GetInt32(0)!=actor.Tenant||reader.GetInt32(1)!=actor.User||reader.GetInt32(2)!=client||reader.GetInt32(3)!=upload||hash is not null&&reader.GetString(4)!=hash)throw new TrialBalanceStorageRejected("conflict","tb_review_operation_reused");
        if(reader.GetString(5)!="completed")throw new TrialBalanceStorageRejected("review","tb_review_operation_incomplete");
        string raw=reader.GetString(6);if(raw.Length>16384)throw new TrialBalanceStorageRejected("review","tb_review_outcome_invalid");
        return JsonSerializer.Deserialize<TrialBalanceReviewOutcome>(raw,TrialBalanceStorageJson.Options)??throw new TrialBalanceStorageRejected("review","tb_review_outcome_invalid");
    }
}
