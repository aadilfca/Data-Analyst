using System.Text;
using System.Text.Json;
using System.Security.Cryptography;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;
namespace AuditPlatform.Api.Uploads;

public sealed class UploadImportReceiptService(PostgresTransactions transactions,UploadValidationPlanStore plans,UploadCorrectionService corrections,
    UploadStagingService staging,IOptions<UploadImportOptions> options,TimeProvider? clock=null,IConfiguration? configuration=null)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    private DateTimeOffset Now=>(clock??TimeProvider.System).GetUtcNow();

    public async Task<ReceiptResult> GetAsync(VerifiedSession? session,int file,Guid id,CancellationToken ct=default)=>
        await Run(session,async(actor,token)=>new("success",await ReadAuthorized(actor,file,id,token)),ct);

    public async Task<ReceiptResult> CheckNoChangesAsync(VerifiedSession? session,int file,ValidationPlanRequest input,CancellationToken ct=default)=>
        await Run(session,async(actor,token)=>
        {
            if(input.RequestId==Guid.Empty||file<1||input.Review?.Coverage?.Footing?.Validation?.Source is null) return new("invalid",Code:"invalid_receipt_request");
            string requestHash=UploadValidationPlanSigner.RequestHash(file,input.Review);
            var prior=await transactions.ExecuteAsync(async(db,cancel)=>
            {
                var found=await Read(db,actor,file,input.RequestId,cancel);if(found is null) return null;
                if(found.Value.Hash!=requestHash||found.Value.Receipt.Kind!="no_changes") throw new ValidationPlanRejected("receipt_request_reused");
                return await plans.AuthorizeScopeAsync(db,actor,found.Value.Receipt.ClientId,found.Value.Receipt.TemplateId,(_,__)=>Task.FromResult(found.Value.Receipt),cancel);
            },token);
            // Retrying the same request recovers its original checked-at evidence.
            // A new request ID is required for another fresh check.
            if(prior is not null) return new("success",prior);
            var captured=await corrections.CaptureAsync(actor,file,input.Review,token);
            if(captured.Result.Status!="success") return new(captured.Result.Status,Code:captured.Result.Code);
            var evidence=captured.Evidence!;var output=evidence.Output;
            if(evidence.Profile.AccountCode is not null) return new("review",Code:"ledger_receipt_proof_required");
            if(!output.NoOp||!output.CorrectionChecksPassed) return new("review",Code:"receipt_requires_unchanged_data");
            var incomingGroups=evidence.Incoming.Select(r=>UploadIdentityValue.Group(evidence.Profile,r.Values)).ToHashSet(StringComparer.Ordinal);
            if(evidence.Profile.Derivation is {} derived&&evidence.Existing.Any(r=>r.DocumentGroup is {} group&&incomingGroups.Contains(group)&&r.DerivedHash!=derived.ContextKey))
                return new("review",Code:"receipt_requires_derivation_refresh");
            var payload=new ValidationPlanPayload(UploadValidationPlanSigner.Format,UploadValidationPlanSigner.Normalize(input.Review),requestHash,output.CorrectionKey,output.StoreKey,output.Counts,[],
                evidence.Existing.Select(r=>new ValidationPlanGuard(r.Id,UploadValidationPlanSigner.GuardHash(r),r.DerivedHash)).ToArray(),incomingGroups.ToArray(),[],evidence.Profile,
                SourceControls:UploadStoredControlService.Capture(output.Coverage,evidence.Profile));
            var source=input.Review.Coverage.Footing.Validation.Source;
            var result=await staging.ReadOwnedAsync(actor,source.ClientId,source.TemplateId,file,source.Sha256,source.ConfigurationKey,
                async(path,sourceHash,_,_,cancel)=>await transactions.ExecuteAsync((db,transactionToken)=>plans.AuthorizeAsync(db,actor,file,input.Review,true,async(owned,expiry,ownedToken)=>
                {
                    var retried=await Read(owned,actor,file,input.RequestId,ownedToken);
                    if(retried is not null)
                    {
                        if(retried.Value.Hash!=requestHash||retried.Value.Receipt.Kind!="no_changes") throw new ValidationPlanRejected("receipt_request_reused");
                        return retried.Value.Receipt;
                    }
                    await using(var current=owned.CreateCommand("LOCK TABLE public.client_data IN SHARE MODE")) await current.ExecuteNonQueryAsync(ownedToken);
                    await plans.VerifyCurrent(owned,payload,ownedToken);
                    var receipt=await CreateWithinAsync(owned,actor,file,input.RequestId,payload,"no_changes",Now,expiry,options.Value,ownedToken,configuration?.GetValue<long?>("Platform:DatabaseCapacityLimitBytes")??500000000);
                    await using var bytes=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,65536,FileOptions.Asynchronous);
                    if(Convert.ToHexString(await SHA256.HashDataAsync(bytes,ownedToken)).ToLowerInvariant()!=sourceHash||expiry<=Now)
                        throw new ValidationPlanRejected("receipt_source_changed");
                    return receipt;
                },transactionToken),cancel),token,recheckAfterRead:false);
            return result.Status=="success"?new("success",result.Value):new(result.Status,Code:result.Code);
        },ct);

    public async Task<ReceiptWorkbookResult> WorkbookAsync(VerifiedSession? session,int file,Guid id,CancellationToken ct=default)
    {
        ReceiptWorkbookData? data=null;byte[]? bytes=null;
        var result=await Run(session,async(actor,token)=>
        {
            await transactions.ExecuteAsync(async(db,cancel)=>
            {
                var found=await Read(db,actor,file,id,cancel)??throw new ValidationPlanRejected("receipt_not_found");var receipt=found.Receipt;
                return await plans.AuthorizeScopeAsync(db,actor,receipt.ClientId,receipt.TemplateId,async(owned,ownedToken)=>
                {
                    if(receipt.EvidenceExpiresAt<=Now) throw new ValidationPlanRejected("receipt_evidence_expired");
                    if(receipt.EvidenceStatus!="available") throw new ValidationPlanRejected("receipt_evidence_too_large");
                    await using(var current=owned.CreateCommand("LOCK TABLE public.client_data IN SHARE MODE")) await current.ExecuteNonQueryAsync(ownedToken);
                    var snapshot=await Snapshot(owned,receipt.ClientId,receipt.TemplateId,receipt.Id,receipt.Groups.Select(g=>g.Group).ToArray(),options.Value,ownedToken);
                    if(snapshot.Limited) throw new ValidationPlanRejected("receipt_evidence_too_large");
                    var previous=new List<ReceiptEvidenceRow>();var incoming=new List<ReceiptEvidenceRow>();
                    var currentByGroup=snapshot.Current.ToLookup(r=>r.Group,StringComparer.Ordinal);
                    var historyByGroup=snapshot.History.ToLookup(r=>r.Group,StringComparer.Ordinal);
                    foreach(var group in receipt.Groups)
                    {
                        var after=currentByGroup[group.Group].ToArray();
                        var before=Before(after,historyByGroup[group.Group],group.AddedIds);
                        if(Digest(after)!=group.AfterHash||Digest(before)!=group.BeforeHash) throw new ValidationPlanRejected("receipt_evidence_changed");
                        var afterById=after.ToDictionary(r=>r.Id);incoming.AddRange(group.IncomingIds.Select(i=>afterById[i]));previous.AddRange(before);
                    }
                    data=new(receipt,previous,incoming);return true;
                },cancel);
            },token);
            // Keep the request slot and deadline through encoding, but release
            // the database snapshot before the potentially slower ZIP work.
            try {bytes=UploadIssueWorkbookWriter.Write(data!,options.Value,token);return new ReceiptResult("success");}
            catch(InvalidDataException) {return new ReceiptResult("review",Code:"receipt_evidence_too_large_or_invalid");}
        },ct);
        if(result.Status!="success") return new(result.Status,Code:result.Code);
        return new("success",bytes);
    }

    private async Task<ReceiptResult> Run(VerifiedSession? session,Func<VerifiedSession,CancellationToken,Task<ReceiptResult>> action,CancellationToken ct)
    {
        if(!options.Value.Enabled) return new("unavailable");if(session is null) return new("unauthenticated");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try {return await action(session,deadline.Token);}
        catch(ValidationPlanRejected error) {return new(error.Code=="plan_access_denied"?"forbidden":error.Code=="receipt_not_found"?"not_found":error.Code is "receipt_evidence_expired" or "receipt_evidence_changed"?"gone":"conflict",Code:error.Code);}
        catch(IdentityReview error) {return new("review",Code:error.Code);}
        catch(PostgresCommitOutcomeUnknownException) {return new("conflict",Code:"receipt_outcome_unknown_retry_same_request");}
        catch(PostgresException error) when(error.SqlState is "42P01" or "42703") {return new("review",Code:"receipt_schema_review_required");}
        catch(PostgresException error) when(error.SqlState=="23505") {return new("conflict",Code:"receipt_request_reused");}
        catch(PostgresException error) when(error.SqlState=="55P03") {return new("busy",Code:"receipt_lock_busy");}
        catch(PostgresException error) when(error.SqlState=="57014") {return new("timeout",Code:"receipt_database_timeout");}
        catch(OperationCanceledException) when(!ct.IsCancellationRequested&&deadline.IsCancellationRequested) {return new("timeout",Code:"receipt_timeout");}
        finally {requests.Release();}
    }

    private Task<UploadImportReceipt> ReadAuthorized(VerifiedSession actor,int file,Guid id,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        var found=await Read(db,actor,file,id,token)??throw new ValidationPlanRejected("receipt_not_found");
        return await plans.AuthorizeScopeAsync(db,actor,found.Receipt.ClientId,found.Receipt.TemplateId,(_,__)=>Task.FromResult(found.Receipt),token);
    },ct);

    private static async Task<(UploadImportReceipt Receipt,string Hash)?> Read(PostgresTransactionContext db,VerifiedSession actor,int file,Guid id,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT receipt,request_hash,client_id,template_id,kind FROM public.platform_upload_import_receipts WHERE receipt_id=$1 AND file_id=$2 AND tenant_id=$3 AND user_id=$4 FOR SHARE");
        command.Parameters.AddWithValue(id);command.Parameters.AddWithValue(file);command.Parameters.AddWithValue(actor.TenantId??0);command.Parameters.AddWithValue(actor.UserId);
        await using var reader=await command.ExecuteReaderAsync(ct);if(!await reader.ReadAsync(ct)) return null;
        var receipt=JsonSerializer.Deserialize<UploadImportReceipt>(reader.GetString(0))??throw new ValidationPlanRejected("receipt_invalid");
        if(receipt.Id!=id||receipt.FileId!=file||receipt.ClientId!=reader.GetInt32(2)||receipt.TemplateId!=reader.GetInt32(3)||receipt.Kind!=reader.GetString(4)) throw new ValidationPlanRejected("receipt_invalid");
        return (receipt,reader.GetString(1));
    }

    internal static async Task<UploadImportReceipt> CreateWithinAsync(PostgresTransactionContext db,VerifiedSession actor,int file,Guid id,
        ValidationPlanPayload payload,string kind,DateTimeOffset now,DateTimeOffset sourceExpiry,UploadImportOptions options,CancellationToken ct,long databaseLimit=500000000)
    {
        var source=payload.Review.Coverage.Footing.Validation.Source;
        var controls=payload.SourceControls??throw new ValidationPlanRejected("receipt_source_evidence_required");
        var definition=(await TenantTemplateService.LoadAsync(db,actor.TenantId!.Value,source.TemplateId,ct)).Single();
        if(controls.DocumentKey!=payload.ImportProfile!.DocumentKey||controls.Footings.Any(c=>!definition.Columns.Any(d=>d.Id==c.Id&&d.StorageKey==c.Key&&d.Role=="footing")))
            throw new ValidationPlanRejected("receipt_configuration_changed");
        var reconciliation=await UploadStoredControlService.VerifyAsync(db,controls,source.ClientId,source.TemplateId,now,ct);
        var groups=payload.Actions.Select(a=>a.Action.DocumentGroup).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        var evidence=await Snapshot(db,source.ClientId,source.TemplateId,id,groups,options,ct);
        var descriptors=new List<ReceiptGroup>();
        var currentByGroup=evidence.Current.ToLookup(r=>r.Group,StringComparer.Ordinal);
        var historyByGroup=evidence.History.ToLookup(r=>r.Group,StringComparer.Ordinal);
        var sourceByGroup=(payload.SourceRows??[]).ToLookup(r=>r.Group,StringComparer.Ordinal);
        int workbookRows=0;bool limited=evidence.Limited;
        if(!evidence.Limited)
        foreach(string group in groups)
        {
            var after=currentByGroup[group].ToArray();var byId=after.ToDictionary(r=>r.Id);
            var bySource=after.ToLookup(r=>(r.UploadId,r.OrderUpload,r.SourceRow));
            var incoming=new List<int>();var added=new List<int>();
            foreach(var reference in sourceByGroup[group])
            {
                int rowId;
                if(reference.ExistingId is {} oldId) rowId=checked((int)oldId);
                else
                {
                    var matches=bySource[(file,file,reference.Row)].ToArray();
                    if(matches.Length!=1) throw new ValidationPlanRejected("receipt_row_reference_changed");rowId=matches[0].Id;added.Add(rowId);
                }
                if(!byId.ContainsKey(rowId)) throw new ValidationPlanRejected("receipt_row_reference_changed");incoming.Add(rowId);
            }
            var before=Before(after,historyByGroup[group],added);
            workbookRows+=before.Count+incoming.Count;
            if(workbookRows>options.MaximumWorkbookRows) {limited=true;descriptors.Clear();break;}
            descriptors.Add(new(group,Digest(before),Digest(after),incoming,added));
        }
        var expiry=now.AddMinutes(options.EvidenceMinutes);if(sourceExpiry<expiry) expiry=sourceExpiry;
        var receipt=new UploadImportReceipt(id,kind,file,source.ClientId,source.TemplateId,source.Sha256!,source.Sheet,now,expiry,payload.Counts,controls,reconciliation,
            definition.Columns.Select(c=>new ReceiptColumn(c.Id,c.StorageKey,c.Name,c.Type,c.Order)).ToArray(),descriptors,limited?"too_large":"available");
        string text=JsonSerializer.Serialize(receipt);int size=Encoding.UTF8.GetByteCount(text);
        if(size>options.MaximumReceiptBytes) throw new ValidationPlanRejected("receipt_capacity_required");
        await using(var budget=db.CreateCommand("SELECT pg_database_size(current_database())"))
            if((long)(await budget.ExecuteScalarAsync(ct))!+size*4L+1048576>=Math.Min(500000000,databaseLimit)) throw new ValidationPlanRejected("receipt_storage_capacity");
        if(kind=="no_changes")
        {
            await using var count=db.CreateCommand("SELECT count(*) FROM public.platform_upload_import_receipts WHERE tenant_id=$1 AND user_id=$2 AND file_id=$3 AND kind='no_changes'");
            count.Parameters.AddWithValue(actor.TenantId.Value);count.Parameters.AddWithValue(actor.UserId);count.Parameters.AddWithValue(file);
            if((long)(await count.ExecuteScalarAsync(ct))!>=options.MaximumNoChangeReceiptsPerFile) throw new ValidationPlanRejected("receipt_count_capacity");
        }
        await using(var insert=db.CreateCommand("""
            INSERT INTO public.platform_upload_import_receipts(receipt_id,operation_id,tenant_id,user_id,client_id,template_id,file_id,kind,request_hash,receipt,completed_at,evidence_expires_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """))
        {
            insert.Parameters.AddWithValue(id);insert.Parameters.AddWithValue(NpgsqlDbType.Uuid,kind=="imported"?id:DBNull.Value);
            insert.Parameters.AddWithValue(actor.TenantId.Value);insert.Parameters.AddWithValue(actor.UserId);insert.Parameters.AddWithValue(source.ClientId);insert.Parameters.AddWithValue(source.TemplateId);
            insert.Parameters.AddWithValue(file);insert.Parameters.AddWithValue(kind);insert.Parameters.AddWithValue(payload.RequestHash);insert.Parameters.AddWithValue(text);insert.Parameters.AddWithValue(now);insert.Parameters.AddWithValue(expiry);
            await insert.ExecuteNonQueryAsync(ct);
        }
        return receipt;
    }

    private static IReadOnlyList<ReceiptEvidenceRow> Before(IEnumerable<ReceiptEvidenceRow> current,IEnumerable<ReceiptEvidenceRow> history,IEnumerable<int> added)
    {
        var excluded=added.ToHashSet();var rows=current.Where(r=>!excluded.Contains(r.Id)).ToDictionary(r=>r.Id);
        foreach(var old in history) if(!rows.TryAdd(old.Id,old)) rows[old.Id]=old;
        return rows.Values.OrderBy(r=>r.Id).ToArray();
    }
    private static string Digest(IEnumerable<ReceiptEvidenceRow> rows)=>UploadIdentityValue.Hash(string.Join("",rows.OrderBy(r=>r.Id).Select(r=>r.Guard)));
    private static async Task<(List<ReceiptEvidenceRow> Current,List<ReceiptEvidenceRow> History,bool Limited)> Snapshot(PostgresTransactionContext db,int client,int template,Guid operation,string[] groups,UploadImportOptions options,CancellationToken ct)
    {
        var current=new List<ReceiptEvidenceRow>();var history=new List<ReceiptEvidenceRow>();long bytes=0;
        if(groups.Length==0) return (current,history,false);
        foreach(bool old in new[]{false,true})
        {
            await using var command=db.CreateCommand(old?"""
                SELECT client_data_id,source_metadata->>'document_group_id',source_metadata->>'extract_date',row_data::text,(source_metadata->>'source_row_number')::int,(source_metadata->>'upload_id')::int,(source_metadata->>'source_order_upload_id')::int FROM public.client_data_history
                WHERE client_id=$1 AND template_id=$2 AND import_operation_id=$3 AND source_metadata->>'document_group_id'=ANY($4) ORDER BY client_data_id LIMIT $5 FOR SHARE
                """:"""
                SELECT id,document_group_id,extract_date::text,row_data::text,source_row_number,upload_id,source_order_upload_id FROM public.client_data
                WHERE client_id=$1 AND template_id=$2 AND $3::uuid IS NOT NULL AND document_group_id=ANY($4) AND is_deleted IS FALSE AND change_detection_status<>'deleted'
                ORDER BY id LIMIT $5 FOR SHARE
                """);
            command.Parameters.AddWithValue(client);command.Parameters.AddWithValue(template);command.Parameters.AddWithValue(operation);command.Parameters.AddWithValue(groups);command.Parameters.AddWithValue(options.MaximumWorkbookRows+1);
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                string raw=reader.GetString(3);bytes+=Encoding.UTF8.GetByteCount(raw);
                if(current.Count+history.Count>=options.MaximumWorkbookRows||bytes>67108864) return(current,history,true);
                int id=reader.GetInt32(0);string group=reader.GetString(1);string? date=reader.IsDBNull(2)?null:reader.GetString(2);
                var row=new ReceiptEvidenceRow(id,group,date,raw,UploadIdentityValue.Hash(JsonSerializer.Serialize(new {id,group,date,raw})),reader.IsDBNull(4)?null:reader.GetInt32(4),reader.IsDBNull(5)?null:reader.GetInt32(5),reader.IsDBNull(6)?null:reader.GetInt32(6));
                (old?history:current).Add(row);
            }
        }
        return(current,history,false);
    }
}
