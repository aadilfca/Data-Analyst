using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.TrialBalance;

// Unhosted P35 kernel. No startup registration, route, source adoption or DDL.
public sealed class TrialBalanceStorageService(PostgresTransactions writes,PostgresReadSnapshot reads,
    PostgresAuditWriter audit,IOptions<TrialBalanceStorageOptions> options,long capacityLimitBytes=500000000,
    TimeProvider? timeProvider=null)
{
    private static readonly SemaphoreSlim ApplyGate=new(1,1);
    private static readonly SemaphoreSlim ParseGate=new(2,2);
    private readonly TrialBalanceStorageStore store=new(options);
    private readonly TimeProvider clock=timeProvider??TimeProvider.System;
    private static readonly JsonSerializerOptions Json=TrialBalanceStorageJson.Options;
    public Task<TrialBalanceStorageResult> PreviewImportAsync(VerifiedSession? session,int client,TrialBalancePeriod period,TrialBalanceStorageSource source,CancellationToken ct=default)
        =>Preview(session,client,"import",period,null,source,ct);
    public Task<TrialBalanceStorageResult> PreviewDeleteAsync(VerifiedSession? session,int client,int upload,CancellationToken ct=default)
        =>Preview(session,client,"delete",null,upload,null,ct);
    public Task<TrialBalanceStorageResult> ImportAsync(VerifiedSession? session,int client,TrialBalancePeriod period,TrialBalanceStorageSource source,TrialBalanceStorageRequest request,CancellationToken ct=default)
        =>Apply(session,client,"import",period,null,source,request,ct);
    public Task<TrialBalanceStorageResult> DeleteAsync(VerifiedSession? session,int client,int upload,TrialBalanceStorageRequest request,CancellationToken ct=default)
        =>Apply(session,client,"delete",null,upload,null,request,ct);

    public Task<TrialBalanceStorageResult> StatusAsync(VerifiedSession? session,int client,Guid operation,CancellationToken ct=default)=>Run(async token=>
    {
        if(operation==Guid.Empty) throw new TrialBalanceStorageRejected("invalid","tb_invalid_operation");
        return await reads.ExecuteAsync(async(db,t)=>{
            var actor=await TrialBalanceStorageStore.Authorize(db,session,client,false,t);
            var outcome=await TrialBalanceStorageStore.Outcome(db,actor,client,operation,null,t);
            return outcome is null?new("not_found",Code:"tb_operation_not_found"):new TrialBalanceStorageResult("success",Outcome:outcome);
        },token);
    },ct);
    public Task<TrialBalanceStorageResult> ListAsync(VerifiedSession? session,int client,CancellationToken ct=default)=>Run(token=>reads.ExecuteAsync(async(db,t)=>
    {
        await TrialBalanceStorageStore.Authorize(db,session,client,false,t);var result=new List<TrialBalanceStoredUpload>();
        await using var command=db.CreateCommand("""
            SELECT u.id,left(u.file_name,257),u.status,u.row_count,u.period_start::text,u.period_end::text,n.upload_id IS NOT NULL
            FROM public.trial_balance_uploads u LEFT JOIN public.platform_trial_balance_uploads n ON n.upload_id=u.id
            WHERE u.client_id=$1 ORDER BY u.period_end DESC NULLS LAST,u.id DESC LIMIT $2
            """);command.Parameters.AddWithValue(client);command.Parameters.AddWithValue(options.Value.MaximumUploadsPerClient+1);
        await using var reader=await command.ExecuteReaderAsync(t);
        while(await reader.ReadAsync(t))
        {
            if(result.Count>=options.Value.MaximumUploadsPerClient) throw new TrialBalanceStorageRejected("too_large","tb_upload_capacity");
            string? Text(int i)=>reader.IsDBNull(i)?null:reader.GetString(i);
            if(Text(1)?.Length>256) throw new TrialBalanceStorageRejected("review","tb_filename_review_required");
            result.Add(new(reader.GetInt32(0),Text(1)??"",Text(2)??"",reader.IsDBNull(3)?0:reader.GetInt32(3),Text(4),Text(5),reader.GetBoolean(6)));
        }
        return new TrialBalanceStorageResult("success",Uploads:result);
    },token),ct);

    private Task<TrialBalanceStorageResult> Preview(VerifiedSession? session,int client,string kind,TrialBalancePeriod? period,int? upload,TrialBalanceStorageSource? source,CancellationToken ct)=>Run(async token=>
    {
        Validate(kind,period,upload,source);source=Freeze(source);
        await reads.ExecuteAsync((db,t)=>TrialBalanceStorageStore.Authorize(db,session,client,false,t),token);
        var review=await Parse(source,token);var originals=review is null?null:TrialBalanceAssignmentStore.Originals(review,source!.Review);
        var rows=review is null?[]:TrialBalanceStorageWriter.Rows(review,source!.Review,originals!,token);
        string[] codes=Codes(review,originals);
        return await reads.ExecuteAsync(async(db,t)=>{
            var actor=await TrialBalanceStorageStore.Authorize(db,session,client,false,t);
            var snapshot=await store.Capture(db,actor,client,kind,period,upload,codes,t);
            if(review is not null) await TrialBalanceStorageWriter.Precision(db,rows,snapshot.AmountTypes,t);
            var proof=new TrialBalanceStorageProof(1,actor.Tenant,actor.User,session!.ContextVersion,client,kind,period,upload,
                source?.FileName??"",source?.Extension??"",review?.Sha256??"",review?.InputFingerprint??"",snapshot.Fingerprint,clock.GetUtcNow().AddMinutes(options.Value.ProofMinutes));
            var preview=new TrialBalanceStoragePreview(client,kind,period,upload,proof.Sha256,proof.Review,proof.Fingerprint,
                snapshot.Blockers.Count==0?Sign(proof):"",snapshot.Blockers.Count==0,snapshot.Blockers,snapshot.Uploads,
                review?.AccountCount??snapshot.AccountCount,review?.LeafCount??0,review?.ParentCount??0,snapshot.AccountCount,clock.GetUtcNow());
            return new TrialBalanceStorageResult("success",Preview:preview);
        },token);
    },ct);

    private Task<TrialBalanceStorageResult> Apply(VerifiedSession? session,int client,string kind,TrialBalancePeriod? period,int? upload,
        TrialBalanceStorageSource? source,TrialBalanceStorageRequest request,CancellationToken ct)=>Run(async token=>
    {
        Validate(kind,period,upload,source);source=Freeze(source);
        if(request.OperationId==Guid.Empty||request.PreviewToken.Length>16384||request.Confirmation!=(kind=="import"?"STORE TRIAL BALANCE":"DELETE TRIAL BALANCE"))
            throw new TrialBalanceStorageRejected("invalid","tb_confirmation_required");
        string requestHash=TrialBalanceStorageStore.Hash(JsonSerializer.Serialize(new {client,kind,period,upload,source=source is null?null:new {source.Extension,source.FileName,source.Review},request},Json));
        // Recovery precedes reading the source or expiring its proof. It never
        // writes and cannot replay a completed replacement/deletion.
        var recovered=await Recover(session,client,request.OperationId,requestHash,token);
        if(recovered is not null) return new("success",Outcome:recovered);
        var proof=Verify(request.PreviewToken);
        if(session is null||proof.Tenant!=session.TenantId||proof.User!=session.UserId||proof.Context!=session.ContextVersion||proof.Client!=client
            ||proof.Kind!=kind||proof.Period!=period||proof.Upload!=upload||proof.SourceName!=(source?.FileName??"")||proof.SourceExtension!=(source?.Extension??"")||proof.Expires<clock.GetUtcNow())
            throw new TrialBalanceStorageRejected("conflict","tb_confirmation_stale");
        if(!await ApplyGate.WaitAsync(0,token)) throw new TrialBalanceStorageRejected("busy","tb_apply_busy");
        try
        {
            var review=await Parse(source,token);
            if(proof.Sha256!=(review?.Sha256??"")||proof.Review!=(review?.InputFingerprint??"")) throw new TrialBalanceStorageRejected("conflict","tb_source_changed");
            var originals=review is null?null:TrialBalanceAssignmentStore.Originals(review,source!.Review);
            var rows=review is null?[]:TrialBalanceStorageWriter.Rows(review,source!.Review,originals!,token);
            var codes=Codes(review,originals);
            try
            {
                return await writes.ExecuteAsync(async(db,t)=>{
                    var actor=await TrialBalanceStorageStore.Authorize(db,session,client,true,t);
                    var done=await TrialBalanceStorageStore.Outcome(db,actor,client,request.OperationId,requestHash,t);
                    if(done is not null) return new TrialBalanceStorageResult("success",Outcome:done);
                    // Lock the captured tables before the final read: DDL,
                    // legacy writes and new references cannot pass this point.
                    var before=await store.Capture(db,actor,client,kind,period,upload,codes,t);
                    foreach(string table in before.Tables.Order())
                    {await using var gate=db.CreateCommand("LOCK TABLE public."+TrialBalanceStorageStore.Quote(table)+" IN SHARE ROW EXCLUSIVE MODE NOWAIT");await gate.ExecuteNonQueryAsync(t);}
                    var current=await store.Capture(db,actor,client,kind,period,upload,codes,t);
                    if(current.Fingerprint!=proof.Fingerprint||!current.Tables.SequenceEqual(before.Tables)||current.Blockers.Count!=0)
                        throw new TrialBalanceStorageRejected("conflict","tb_confirmation_stale");
                    if(review is not null) await TrialBalanceStorageWriter.Precision(db,rows,current.AmountTypes,t);
                    long incoming=TrialBalanceBudget.Size(rows,33554432);
                    await using(var size=db.CreateCommand("SELECT pg_database_size(current_database())"))
                    {
                        long allocated=Convert.ToInt64(await size.ExecuteScalarAsync(t));long projected=checked(allocated+incoming*3+rows.Length*1024L+2097152);
                        if(projected>=Math.Min(500000000,capacityLimitBytes)) throw new TrialBalanceStorageRejected("too_large","tb_database_capacity");
                    }
                    await using(var begin=db.CreateCommand("""
                        INSERT INTO public.platform_trial_balance_operations(operation_id,tenant_id,user_id,client_id,kind,request_hash,fingerprint,source_hash,target_upload_ids)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                        """))
                    {begin.Parameters.AddWithValue(request.OperationId);begin.Parameters.AddWithValue(actor.Tenant);begin.Parameters.AddWithValue(actor.User);begin.Parameters.AddWithValue(client);begin.Parameters.AddWithValue(kind);begin.Parameters.AddWithValue(requestHash);begin.Parameters.AddWithValue(proof.Fingerprint);begin.Parameters.AddWithValue(proof.Sha256);begin.Parameters.AddWithValue(current.UploadIds);await begin.ExecuteNonQueryAsync(t);}
                    await using(var permit=db.CreateCommand("SELECT set_config('auditplatform.trial_balance_operation',$1,true)"))
                    {permit.Parameters.AddWithValue(request.OperationId.ToString());await permit.ExecuteNonQueryAsync(t);}
                    int? saved=null;bool cleared=false;
                    if(review is not null) saved=await TrialBalanceStorageWriter.Import(db,actor,client,period!,source!,review,request.OperationId,rows,current,t);
                    else
                    {
                        await TrialBalanceStorageWriter.Remove(db,current,t);
                        if(current.ClientUploadCount==current.UploadIds.Length)
                        {await using var mirror=db.CreateCommand("DELETE FROM public.account_category_assignments WHERE client_id=$1");mirror.Parameters.AddWithValue(client);await mirror.ExecuteNonQueryAsync(t);cleared=true;}
                    }
                    var outcome=new TrialBalanceStorageOutcome(request.OperationId,client,kind,saved,current.UploadIds.Length,review?.AccountCount??0,review?.LeafCount??0,review?.ParentCount??0,
                        review?.RollforwardDifferences.Count??0,review?.GlobalAdjustments.Count??0,cleared,proof.Sha256,clock.GetUtcNow());
                    var metadata=JsonSerializer.SerializeToElement(new {operation_id=request.OperationId,row_count=outcome.AccountCount,removed_count=outcome.RemovedUploads,source_hash=proof.Sha256});
                    await audit.WriteAsync(db,new AuditEvent(Guid.NewGuid(),request.OperationId,new LogActor("authenticated",actor.User,actor.Name),client,
                        kind=="import"?"TRIAL_BALANCE_IMPORT":"TRIAL_BALANCE_DELETE","Trial Balance",metadata),t);
                    await using var complete=db.CreateCommand("UPDATE public.platform_trial_balance_operations SET state='completed',outcome=$1::jsonb,completed_at=now() WHERE operation_id=$2");
                    complete.Parameters.AddWithValue(JsonSerializer.Serialize(outcome,Json));complete.Parameters.AddWithValue(request.OperationId);await complete.ExecuteNonQueryAsync(t);
                    return new TrialBalanceStorageResult("success",Outcome:outcome);
                },token);
            }
            catch(PostgresCommitOutcomeUnknownException)
            {
                using var recovery=new CancellationTokenSource(TimeSpan.FromSeconds(5));
                try {var done=await Recover(session,client,request.OperationId,requestHash,recovery.Token);if(done is not null)return new("success",Outcome:done);}
                catch(Exception e) when(e is NpgsqlException or TimeoutException or OperationCanceledException or TrialBalanceStorageRejected) { }
                return new("unknown",Code:"tb_reconcile_operation");
            }
        }
        finally {ApplyGate.Release();}
    },ct);

    private Task<TrialBalanceStorageOutcome?> Recover(VerifiedSession? session,int client,Guid operation,string hash,CancellationToken ct)
        =>reads.ExecuteAsync(async(db,t)=>{var actor=await TrialBalanceStorageStore.Authorize(db,session,client,false,t);return await TrialBalanceStorageStore.Outcome(db,actor,client,operation,hash,t);},ct);
    private static string[] Codes(TrialBalanceReview? review,Dictionary<int,string>? originals)
        =>review is null?[]:review.Accounts.Select(a=>a.AccountCode).Concat(originals!.Values).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
    private static TrialBalanceStorageSource? Freeze(TrialBalanceStorageSource? source)
    {if(source is null)return null;TrialBalanceBudget.Size(source.Review,33554432);return source with {Review=JsonSerializer.Deserialize<TrialBalanceInput>(JsonSerializer.Serialize(source.Review))!};}
    private static async Task<TrialBalanceReview?> Parse(TrialBalanceStorageSource? source,CancellationToken ct)
    {
        if(source is null)return null;
        if(!await ParseGate.WaitAsync(0,ct)) throw new TrialBalanceStorageRejected("busy","tb_parser_busy");
        try {return await Task.Run<TrialBalanceReview?>(()=>{var review=TrialBalanceParser.Parse(source.Path,source.Extension,source.Review,cancellationToken:ct);
            if(!review.Valid||review.AccountCount==0)throw new TrialBalanceStorageRejected("review","tb_source_review_required");return review;},ct);}
        finally {ParseGate.Release();}
    }
    private static void Validate(string kind,TrialBalancePeriod? period,int? upload,TrialBalanceStorageSource? source)
    {
        if(kind=="delete") {if(upload is not >0)throw new TrialBalanceStorageRejected("invalid","tb_invalid_upload");return;}
        if(period is null||!DateOnly.TryParseExact(period.Start,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var start)
            ||!DateOnly.TryParseExact(period.End,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var end)||start>end
            ||period.Type is not("monthly" or "quarterly" or "half_yearly" or "annual" or "custom")) throw new TrialBalanceStorageRejected("invalid","tb_invalid_period");
        if(source is null||string.IsNullOrWhiteSpace(source.FileName)||source.FileName.Length>256||source.FileName.IndexOfAny(['/', '\\', '\0'])>=0||source.Extension is not(".xlsx" or ".xls"))
            throw new TrialBalanceStorageRejected("invalid","tb_invalid_source");
    }
    private string Sign(TrialBalanceStorageProof proof)
    {byte[] data=JsonSerializer.SerializeToUtf8Bytes(proof,Json);return Convert.ToBase64String(data)+"."+Convert.ToBase64String(HMACSHA256.HashData(Convert.FromBase64String(options.Value.SigningKey),data));}
    private TrialBalanceStorageProof Verify(string token)
    {
        try {var pieces=token.Split('.');if(pieces.Length!=2||token.Length>16384)throw new FormatException();byte[] data=Convert.FromBase64String(pieces[0]);byte[] sig=Convert.FromBase64String(pieces[1]);
            if(!CryptographicOperations.FixedTimeEquals(sig,HMACSHA256.HashData(Convert.FromBase64String(options.Value.SigningKey),data)))throw new FormatException();
            var proof=JsonSerializer.Deserialize<TrialBalanceStorageProof>(data,Json);if(proof is null||proof.Version!=1)throw new FormatException();return proof;}
        catch(Exception e) when(e is FormatException or JsonException) {throw new TrialBalanceStorageRejected("invalid","tb_invalid_confirmation");}
    }
    private async Task<TrialBalanceStorageResult> Run(Func<CancellationToken,Task<TrialBalanceStorageResult>> operation,CancellationToken ct)
    {
        if(!options.Value.Enabled)return new("disabled",Code:"tb_storage_disabled");
        if(!options.Value.Valid())return new("unavailable",Code:"tb_storage_configuration");
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try {var result=await operation(timeout.Token);TrialBalanceBudget.Size(result,options.Value.MaximumResponseBytes);return result;}
        catch(TrialBalanceStorageRejected e) {return new(e.Status,Code:e.Code);}
        catch(TrialBalanceRejected e) {return new(e.Code=="evidence_capacity"?"too_large":"review",Code:"tb_"+e.Code);}
        catch(AuditPlatform.Api.Workbooks.WorkbookReadException) {return new("review",Code:"tb_workbook_review_required");}
        catch(Exception e) when(e is InvalidDataException or System.Xml.XmlException) {return new("review",Code:"tb_workbook_review_required");}
        catch(OperationCanceledException) {return new(ct.IsCancellationRequested?"cancelled":"timeout",Code:"tb_operation_cancelled");}
        catch(PostgresException e) when(e.SqlState is "55P03" or "40001" or "40P01" or "23505") {return new("conflict",Code:"tb_concurrent_change");}
        catch(PostgresException e) when(e.SqlState is "22003" or "22P02") {return new("review",Code:"tb_storage_precision_review");}
        catch(PostgresException e) when(e.SqlState is "23503" or "23514" or "42P01" or "42703" or "42501") {return new("review",Code:"tb_schema_or_dependency_review");}
        catch(Exception e) when(e is NpgsqlException or TimeoutException or IOException or JsonException) {return new("unavailable",Code:"tb_storage_unavailable");}
    }
}
