using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.TrialBalance;

public sealed class TrialBalanceReviewService(PostgresTransactions writes,PostgresReadSnapshot reads,PostgresAuditWriter audit,
    IOptions<TrialBalanceReviewOptions> options,long capacityLimitBytes=500000000,TimeProvider? timeProvider=null)
{
    private static readonly SemaphoreSlim Readers=new(2,2),ApplyGate=new(1,1);
    private readonly TrialBalanceReviewStore store=new(options);
    private readonly TimeProvider clock=timeProvider??TimeProvider.System;
    private static readonly JsonSerializerOptions Json=TrialBalanceStorageJson.Options;
    private static string Hash<T>(T value)=>TrialBalanceStorageStore.Hash(JsonSerializer.Serialize(value,Json));
    private static JsonElement Element<T>(T value)=>JsonSerializer.SerializeToElement(value,Json);

    public Task<TrialBalanceReviewResult> ListAsync(VerifiedSession? session,int client,CancellationToken ct=default)=>Run(async token=>
    {
        return await reads.ExecuteAsync(async(db,t)=>{
            await TrialBalanceStorageStore.Authorize(db,session,client,false,t);var uploads=new List<JsonElement>();
            await using var get=db.CreateCommand("SELECT left(to_jsonb(u)::text,1048577) FROM public.trial_balance_uploads u WHERE client_id=$1 ORDER BY period_end DESC NULLS LAST,uploaded_at DESC,id DESC LIMIT 257");get.Parameters.AddWithValue(client);
            await using var reader=await get.ExecuteReaderAsync(t);while(await reader.ReadAsync(t))
            {
                if(uploads.Count>=256)throw new TrialBalanceStorageRejected("too_large","tb_upload_capacity");string raw=reader.GetString(0);if(raw.Length>1048576)throw new TrialBalanceStorageRejected("too_large","tb_review_cell_capacity");
                using var doc=JsonDocument.Parse(raw);uploads.Add(Element(doc.RootElement.EnumerateObject().Where(p=>p.Name!="validation_controls").ToDictionary(p=>p.Name,p=>p.Value)));
            }
            return new TrialBalanceReviewResult("success",Element(new {client_id=client,uploads}));
        },token);
    },ct);

    public Task<TrialBalanceReviewResult> ReadAsync(VerifiedSession? session,TrialBalanceReviewQuery query,CancellationToken ct=default)=>Run(async token=>
    {
        Validate(query);int requestedUpload=query.UploadId;return await reads.ExecuteAsync(async(db,t)=>{
            // A repeated read must choose latest again in its new snapshot.
            query=query with {UploadId=requestedUpload};
            var actor=await TrialBalanceStorageStore.Authorize(db,session,query.ClientId,false,t);
            if(query.UploadId==0)
            {
                await using var latest=db.CreateCommand("SELECT id FROM public.trial_balance_uploads WHERE client_id=$1 AND status='parsed' ORDER BY period_end DESC NULLS LAST,uploaded_at DESC,id DESC LIMIT 1");latest.Parameters.AddWithValue(query.ClientId);
                if(await latest.ExecuteScalarAsync(t) is not int id)throw new TrialBalanceStorageRejected("not_found","tb_upload_not_found");query=query with {UploadId=id};
            }
            var snapshot=await store.Capture(db,actor,query.ClientId,query.UploadId,null,t);
            var hierarchy=TrialBalanceReviewSummary.Hierarchy(snapshot,t);
            var filtered=snapshot.Rows.Where(r=>(query.IsLeaf is null||TrialBalanceReviewSummary.Leaf(r,hierarchy)==query.IsLeaf)
                &&(query.Search.Length==0||r.Code.Contains(query.Search,StringComparison.OrdinalIgnoreCase)||(r.Name??"").Contains(query.Search,StringComparison.OrdinalIgnoreCase)))
                .OrderBy(r=>r.Sort).ThenBy(r=>r.Id).ToArray();
            int pages=Math.Max(1,(filtered.Length+query.Limit-1)/query.Limit);if(query.Page>pages)throw new TrialBalanceStorageRejected("invalid","tb_review_page_out_of_range");
            var page=filtered.Skip((query.Page-1)*query.Limit).Take(query.Limit).ToArray();var pageIds=page.Select(r=>r.Id).ToHashSet();
            var byId=snapshot.Rows.ToDictionary(r=>r.Id);var leaves=snapshot.Rows.Where(r=>TrialBalanceReviewSummary.Leaf(r,hierarchy)).Select(r=>r.Id).ToHashSet();
            var mappings=snapshot.Assignments.ToDictionary(p=>p.Key,p=>p.Value.ToDictionary(r=>TrialBalanceReviewStore.Text(r,"account_code"),StringComparer.Ordinal));
            async Task<Dictionary<int,string>> Names(string table,IEnumerable<int> identifiers)
            {
                var names=new Dictionary<int,string>();if(!await TrialBalanceStorageStore.Exists(db,table,t))return names;
                int[] ids=identifiers.Distinct().ToArray();if(ids.Length==0)return names;
                await using var get=db.CreateCommand($"SELECT id,left(label,65537) FROM public.{TrialBalanceStorageStore.Quote(table)} WHERE client_id=$1 AND id=ANY($2)");
                get.Parameters.AddWithValue(query.ClientId);get.Parameters.AddWithValue(ids);await using var reader=await get.ExecuteReaderAsync(t);
                while(await reader.ReadAsync(t)){string label=reader.GetString(1);if(label.Length>65536)throw new TrialBalanceStorageRejected("too_large","tb_review_label_capacity");names.Add(reader.GetInt32(0),label);}return names;
            }
            IEnumerable<int> LabelIds(string table,string key)=>page.Where(r=>mappings[table].ContainsKey(r.Code)).Select(r=>mappings[table][r.Code].GetProperty(key)).Where(v=>v.ValueKind==JsonValueKind.Number).Select(v=>v.GetInt32());
            var categories=await Names("account_control_categories",LabelIds("account_control_assignments","financial_statement_category_id").Concat(LabelIds("account_control_assignments","note_category_id")));
            var cashflow=await Names("account_control_cashflow_categories",LabelIds("account_control_cashflow_category_assignments","category_id"));
            var accounts=page.Select(row=>{
                var values=new Dictionary<string,object?> { ["id"]=row.Id,["account_code"]=row.Code,["source_account_code"]=row.SourceCode,["account_name"]=row.Name,
                    ["parent_account_code"]=hierarchy[row.Code].Parent,["level"]=row.Level,["is_leaf"]=leaves.Contains(row.Id),["manually_overridden"]=row.Manual };
                foreach(string amount in TrialBalanceStorageStore.AmountColumns)values[amount]=row.Values.GetProperty(amount);
                values["assignment_values"]=mappings.ToDictionary(p=>p.Key,p=>(object?)(p.Value.TryGetValue(row.Code,out var mapping)?mapping:null));
                object? Field(string table,string key)=>mappings[table].TryGetValue(row.Code,out var assignment)&&assignment.TryGetProperty(key,out var v)&&v.ValueKind!=JsonValueKind.Null?v:null;
                string Setting(string key,string fallback)=>mappings["account_control_cashflow_assignments"].TryGetValue(row.Code,out var assignment)&&TrialBalanceReviewStore.Text(assignment,key) is {Length:>0} setting?setting:fallback;
                values["assignment"]=new {verification_workstream=Field("account_control_assignments","verification_workstream"),financial_statement_category_id=Field("account_control_assignments","financial_statement_category_id"),note_category_id=Field("account_control_assignments","note_category_id")};
                values["cashflow"]=new {section=Setting("section","unassigned"),category_label=Field("account_control_cashflow_assignments","category_label"),cash_bank_role=Setting("cash_bank_role","none"),treatment_type=Setting("treatment_type","standard")};return values;
            }).ToArray();
            var adjustments=snapshot.Adjustments.Where(r=>pageIds.Contains(r.GetProperty("account_id").GetInt32())).Select(r=>{
                int id=r.GetProperty("account_id").GetInt32();var values=r.EnumerateObject().ToDictionary(p=>p.Name,p=>(object?)p.Value);
                values["account_code"]=byId[id].Code;values["account_name"]=byId[id].Name;values["included_in_footing"]=leaves.Contains(id);return values;
            }).ToArray();
            IReadOnlyList<JsonElement> globals=[];try{globals=query.Page==1?TrialBalanceReviewSummary.Globals(snapshot):[];}catch(FormatException){ }
            var proof=Proof(actor,session!,query.ClientId,query.UploadId,"read",snapshot.Fingerprint,"");
            var uploads=new List<JsonElement>();
            await using(var get=db.CreateCommand("SELECT jsonb_build_object('id',id,'period_start',period_start,'period_end',period_end,'period_type',period_type,'status',status,'row_count',row_count,'as_of_date',as_of_date)::text FROM public.trial_balance_uploads WHERE client_id=$1 ORDER BY period_end DESC NULLS LAST,uploaded_at DESC,id DESC LIMIT 257"))
            {get.Parameters.AddWithValue(query.ClientId);await using var reader=await get.ExecuteReaderAsync(t);while(await reader.ReadAsync(t)){if(uploads.Count>=256)throw new TrialBalanceStorageRejected("too_large","tb_upload_capacity");using var doc=JsonDocument.Parse(reader.GetString(0));uploads.Add(doc.RootElement.Clone());}}
            return new TrialBalanceReviewResult("success",Element(new {client_id=query.ClientId,upload_id=query.UploadId,upload=snapshot.Upload.EnumerateObject().Where(p=>p.Name!="validation_controls").ToDictionary(p=>p.Name,p=>p.Value),
                accounts,adjustments,global_adjustments=globals,validation_controls=TrialBalanceReviewSummary.Controls(snapshot,hierarchy,t),
                summary=new {upload_id=query.UploadId,total_count=snapshot.Rows.Count,leaf_count=leaves.Count,parent_count=snapshot.Rows.Count-leaves.Count},
                pagination=new {page=query.Page,limit=query.Limit,total=filtered.Length,pages,search=query.Search},
                uploads,verification_workstreams=new[]{"bank","cash","contractor","export_customer","import_vendor","local_vendor","payroll"},
                sections=new[]{"excluded","internal_transfer","money_in","money_out","unassigned"},cash_bank_roles=new[]{"bank","both","cash","none"},treatment_types=new[]{"bank_charge","customer_receipt","non_cash","other_deduction","standard","withholding_tax"},
                assignment_labels=new {categories,cashflow},can_edit=snapshot.Blockers.Count==0,write_blockers=snapshot.Blockers,review_token=Sign(proof),fingerprint=snapshot.Fingerprint}));
        },token);
    },ct);

    // The preview binds target assignment rows as well as the displayed source.
    // Merely typing a draft never sends a mutation to this service.
    public Task<TrialBalanceReviewResult> PreviewAsync(VerifiedSession? session,int client,int upload,string kind,TrialBalanceReviewRequest request,CancellationToken ct=default)=>Run(async token=>
    {
        Validate(request,kind);var frozen=Freeze(request);var readProof=Verify(frozen.ReviewToken,session,client,upload,"read");
        return await reads.ExecuteAsync(async(db,t)=>{
            var actor=await TrialBalanceStorageStore.Authorize(db,session,client,false,t);
            var displayed=await store.Capture(db,actor,client,upload,null,t);if(displayed.Fingerprint!=readProof.Fingerprint)throw new TrialBalanceStorageRejected("conflict","tb_review_snapshot_changed");
            var current=await store.Capture(db,actor,client,upload,Targets(frozen),t);Writable(current);
            var plan=TrialBalanceReviewWriter.Plan(current,kind,frozen.Changes,t);
            await TrialBalanceAssignmentStore.CarryCodesAsync(db,actor,client,plan.Pairs,false,t);
            var proof=Proof(actor,session!,client,upload,kind,current.Fingerprint,Hash(frozen.Changes));
            return new TrialBalanceReviewResult("success",Element(new {client_id=client,upload_id=upload,review_token=Sign(proof),corrected_count=plan.Codes,classification_count=plan.Classifications}));
        },token);
    },ct);

    public Task<TrialBalanceReviewResult> SaveAsync(VerifiedSession? session,int client,int upload,string kind,TrialBalanceReviewRequest request,CancellationToken ct=default)=>Run(async token=>
    {
        Validate(request,kind);var frozen=Freeze(request);string requestHash=Hash(new {client,upload,kind,request=frozen});
        var recovered=await Recover(session,client,upload,frozen.OperationId,requestHash,token);if(recovered is not null)return new("success",Outcome:recovered);
        var proof=Verify(frozen.ReviewToken,session,client,upload,kind);if(proof.ChangesHash!=Hash(frozen.Changes))throw new TrialBalanceStorageRejected("conflict","tb_review_draft_changed");
        if(!await ApplyGate.WaitAsync(0,token))throw new TrialBalanceStorageRejected("busy","tb_review_apply_busy");
        try
        {
            try{return await writes.ExecuteAsync(async(db,t)=>{
                var actor=await TrialBalanceStorageStore.Authorize(db,session,client,true,t);
                var done=await TrialBalanceReviewStore.Outcome(db,actor,client,upload,frozen.OperationId,requestHash,t);if(done is not null)return new TrialBalanceReviewResult("success",Outcome:done);
                var before=await store.Capture(db,actor,client,upload,Targets(frozen),t);
                foreach(string table in before.Storage.Tables.Append(TrialBalanceReviewStore.Ledger).Distinct().Order(StringComparer.Ordinal))
                {await using var gate=db.CreateCommand("LOCK TABLE public."+TrialBalanceStorageStore.Quote(table)+" IN SHARE ROW EXCLUSIVE MODE NOWAIT");await gate.ExecuteNonQueryAsync(t);}
                var current=await store.Capture(db,actor,client,upload,Targets(frozen),t);Writable(current);
                if(current.Fingerprint!=proof.Fingerprint||!current.Storage.Tables.SequenceEqual(before.Storage.Tables))throw new TrialBalanceStorageRejected("conflict","tb_review_snapshot_changed");
                var plan=TrialBalanceReviewWriter.Plan(current,kind,frozen.Changes,t);
                await TrialBalanceAssignmentStore.CarryCodesAsync(db,actor,client,plan.Pairs,false,t);
                await using(var size=db.CreateCommand("SELECT pg_database_size(current_database())"))
                {long allocated=Convert.ToInt64(await size.ExecuteScalarAsync(t));if(allocated+TrialBalanceBudget.Size(frozen,1048576)*4+plan.Pairs.Count*4096L+2097152>=Math.Min(500000000,capacityLimitBytes))throw new TrialBalanceStorageRejected("too_large","tb_database_capacity");}
                var originalById=current.Rows.ToDictionary(r=>r.Id);
                var evidence=frozen.Changes.Select(c=>new {c.AccountId,c.ExpectedAccountCode,c.AccountCode,c.IsLeaf,
                    old_classification=originalById[c.AccountId].Classification,old_manual=originalById[c.AccountId].Manual}).ToArray();
                // Only explicit code/classification intent is retained, never amounts or raw rows.
                await using(var begin=db.CreateCommand("INSERT INTO public.platform_trial_balance_review_operations(operation_id,tenant_id,user_id,client_id,upload_id,kind,request_hash,fingerprint,changes) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)"))
                {begin.Parameters.AddWithValue(frozen.OperationId);begin.Parameters.AddWithValue(actor.Tenant);begin.Parameters.AddWithValue(actor.User);begin.Parameters.AddWithValue(client);begin.Parameters.AddWithValue(upload);begin.Parameters.AddWithValue(kind);begin.Parameters.AddWithValue(requestHash);begin.Parameters.AddWithValue(current.Fingerprint);begin.Parameters.AddWithValue(JsonSerializer.Serialize(evidence,Json));await begin.ExecuteNonQueryAsync(t);}
                await using(var permit=db.CreateCommand("SELECT set_config('auditplatform.trial_balance_review_operation',$1,true)")){permit.Parameters.AddWithValue(frozen.OperationId.ToString());await permit.ExecuteNonQueryAsync(t);}
                await TrialBalanceReviewWriter.Write(db,actor,client,upload,plan,t);
                var after=await store.Capture(db,actor,client,upload,Targets(frozen),t);TrialBalanceReviewWriter.Verify(current,after,plan);
                var outcome=new TrialBalanceReviewOutcome(frozen.OperationId,client,upload,kind,plan.Rows.Count>0,plan.Codes,plan.Classifications,plan.Hierarchy,current.Storage.Fingerprint,after.Storage.Fingerprint,clock.GetUtcNow());
                await audit.WriteAsync(db,new(Guid.NewGuid(),frozen.OperationId,new("authenticated",actor.User,actor.Name),client,"TRIAL_BALANCE_REVIEW","Trial Balance",Element(new {operation_id=frozen.OperationId,row_count=plan.Rows.Count,source_hash=current.Storage.Fingerprint})),t);
                await using var complete=db.CreateCommand("UPDATE public.platform_trial_balance_review_operations SET state='completed',outcome=$1::jsonb,completed_at=now() WHERE operation_id=$2");complete.Parameters.AddWithValue(JsonSerializer.Serialize(outcome,Json));complete.Parameters.AddWithValue(frozen.OperationId);await complete.ExecuteNonQueryAsync(t);
                return new TrialBalanceReviewResult("success",Outcome:outcome);
            },token);}
            catch(PostgresCommitOutcomeUnknownException)
            {
                using var recovery=new CancellationTokenSource(TimeSpan.FromSeconds(5));
                try{var done=await Recover(session,client,upload,frozen.OperationId,requestHash,recovery.Token);if(done is not null)return new("success",Outcome:done);}
                catch(Exception e)when(e is NpgsqlException or OperationCanceledException or TimeoutException or TrialBalanceStorageRejected){ }
                return new("unknown",Code:"tb_review_reconcile_operation");
            }
        }finally{ApplyGate.Release();}
    },ct,false);

    public Task<TrialBalanceReviewResult> StatusAsync(VerifiedSession? session,int client,int upload,Guid operation,CancellationToken ct=default)=>Run(async token=>
    {
        if(operation==Guid.Empty)throw new TrialBalanceStorageRejected("invalid","tb_review_invalid_operation");
        var outcome=await Recover(session,client,upload,operation,null,token);return outcome is null?new("not_found",Code:"tb_review_operation_not_found"):new("success",Outcome:outcome);
    },ct);
    private Task<TrialBalanceReviewOutcome?> Recover(VerifiedSession? session,int client,int upload,Guid operation,string? hash,CancellationToken ct)
        =>reads.ExecuteAsync(async(db,t)=>{var actor=await TrialBalanceStorageStore.Authorize(db,session,client,false,t);return await TrialBalanceReviewStore.Outcome(db,actor,client,upload,operation,hash,t);},ct);
    private static string[] Targets(TrialBalanceReviewRequest request)=>request.Changes.Where(c=>c.AccountCode is not null).Select(c=>c.AccountCode!).ToArray();
    private void Validate(TrialBalanceReviewRequest request,string kind)
    {
        if(kind is not("codes" or "classification")||request.OperationId==Guid.Empty||request.ReviewToken is null||request.ReviewToken.Length>16384||request.Changes is null||request.Changes.Count is <1||request.Changes.Count>options.Value.MaximumChanges||request.Changes.Any(c=>c is null))
            throw new TrialBalanceStorageRejected("invalid","tb_review_invalid_request");
        TrialBalanceBudget.Size(request,1048576);
    }
    private static void Validate(TrialBalanceReviewQuery query)
    {if(query.UploadId<0||query.Page<1||query.Limit is <1 or >500||query.Search is null||query.Search.Length>200)throw new TrialBalanceStorageRejected("invalid","tb_review_invalid_query");}
    private static TrialBalanceReviewRequest Freeze(TrialBalanceReviewRequest request)=>request with {Changes=request.Changes.ToArray()};
    private static void Writable(TrialBalanceReviewSnapshot source)
    {if(source.Blockers.Count>0)throw new TrialBalanceStorageRejected("review",source.Blockers[0]);}
    private TrialBalanceReviewProof Proof(TrialBalanceStorageActor actor,VerifiedSession session,int client,int upload,string purpose,string fingerprint,string changes)
        =>new(1,actor.Tenant,actor.User,session.ContextVersion,client,upload,purpose,fingerprint,changes,clock.GetUtcNow().AddMinutes(options.Value.ProofMinutes));
    private string Sign(TrialBalanceReviewProof proof)
    {byte[] raw=JsonSerializer.SerializeToUtf8Bytes(proof,Json);return Convert.ToBase64String(raw)+"."+Convert.ToBase64String(HMACSHA256.HashData(Convert.FromBase64String(options.Value.SigningKey),raw));}
    private TrialBalanceReviewProof Verify(string value,VerifiedSession? session,int client,int upload,string purpose)
    {
        try
        {
            string[] pieces=value.Split('.');if(pieces.Length!=2||value.Length>16384)throw new FormatException();byte[] raw=Convert.FromBase64String(pieces[0]);
            if(!CryptographicOperations.FixedTimeEquals(Convert.FromBase64String(pieces[1]),HMACSHA256.HashData(Convert.FromBase64String(options.Value.SigningKey),raw)))throw new FormatException();
            var proof=JsonSerializer.Deserialize<TrialBalanceReviewProof>(raw,Json)??throw new FormatException();
            if(session is null||proof.Version!=1||proof.Tenant!=session.TenantId||proof.User!=session.UserId||proof.Context!=session.ContextVersion||proof.Client!=client||proof.Upload!=upload||proof.Purpose!=purpose||proof.Expires<clock.GetUtcNow())throw new TrialBalanceStorageRejected("conflict","tb_review_confirmation_stale");return proof;
        }catch(Exception e)when(e is FormatException or JsonException){throw new TrialBalanceStorageRejected("invalid","tb_review_invalid_confirmation");}
    }
    private async Task<TrialBalanceReviewResult> Run(Func<CancellationToken,Task<TrialBalanceReviewResult>> operation,CancellationToken ct,bool reader=true)
    {
        if(!options.Value.Enabled)return new("disabled",Code:"tb_review_disabled");if(!options.Value.Valid())return new("unavailable",Code:"tb_review_configuration");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));bool entered=false;
        try
        {
            if(reader){entered=await Readers.WaitAsync(0,deadline.Token);if(!entered)return new("busy",Code:"tb_review_readers_busy");}
            var result=await operation(deadline.Token);TrialBalanceBudget.Size(result,options.Value.MaximumResponseBytes);return result;
        }
        catch(TrialBalanceStorageRejected e){return new(e.Status,Code:e.Code);}
        catch(TrialBalanceRejected){return new("too_large",Code:"tb_review_capacity");}
        catch(OperationCanceledException){return new(ct.IsCancellationRequested?"cancelled":"timeout",Code:"tb_review_cancelled");}
        catch(PostgresException e)when(e.SqlState is "40001" or "40P01" or "55P03" or "23505"){return new("conflict",Code:"tb_review_concurrent_change");}
        catch(PostgresException e)when(e.SqlState is "23503" or "23514" or "42P01" or "42703" or "42501"){return new("review",Code:"tb_review_schema_or_dependency");}
        catch(Exception e)when(e is NpgsqlException or TimeoutException or JsonException){return new("unavailable",Code:"tb_review_unavailable");}
        finally{if(entered)Readers.Release();}
    }
}
