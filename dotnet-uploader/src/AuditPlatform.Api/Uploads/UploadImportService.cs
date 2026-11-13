using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
namespace AuditPlatform.Api.Uploads;

public sealed class UploadImportService(PostgresTransactions transactions,UploadValidationPlanStore plans,UploadValidationPlanSigner signer,
    UploadStagingService staging,UploadImportWriter writer,PostgresAuditWriter audit,IOptions<UploadImportOptions> options,TimeProvider? clock=null,IConfiguration? configuration=null)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    private DateTimeOffset Now=>(clock??TimeProvider.System).GetUtcNow();

    public Task<UploadImportResult> ImportAsync(VerifiedSession? session,int file,UploadImportRequest input,CancellationToken ct=default)=>Run(session,file,input,null,ct);
    internal Task<UploadImportResult> ImportJobAsync(VerifiedSession session,int file,UploadImportRequest input,UploadJobExecution job,CancellationToken ct)=>Run(session,file,input,job,ct);
    private async Task<UploadImportResult> Run(VerifiedSession? session,int file,UploadImportRequest input,UploadJobExecution? job,CancellationToken ct)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(session is null) return new("unauthenticated");
        if(input?.Review?.Coverage?.Footing?.Validation?.Source is null||file<1||input.OperationId==Guid.Empty
            ||input.PlanId is not {Length:32}||input.Signature is not {Length:64}) return new("invalid",Code:"invalid_import_request");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var caller=ct;using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var prior=await Recover(session,file,input,deadline.Token);if(prior is not null) return new("success",prior);
            var source=input.Review.Coverage.Footing.Validation.Source;
            var result=await staging.ReadOwnedAsync(session,source.ClientId,source.TemplateId,file,source.Sha256,source.ConfigurationKey,
                async(path,hash,_,_,token)=>await transactions.ExecuteAsync(async(db,transactionToken)=>
                    await plans.AuthorizeAsync(db,session,file,input.Review,false,async(context,_,cancellation)=>
                    {
                        var recovered=await ReadOutcome(context,session,file,input,cancellation);if(recovered is not null) return recovered;
                        if(job is not null) await job.GuardAsync(context,cancellation);
                        return await plans.AuthorizeAsync(context,session,file,input.Review,true,async(owned,sourceExpiry,cancel)=>
                        {
                            var plan=await plans.Read(owned,session,null,input.PlanId,cancel)??throw new ValidationPlanRejected("plan_not_found");
                            UploadValidationPlanStore.Validate(plan,signer,Now);
                            if(!signer.Verify(plan.Envelope,input.Signature)||plan.Envelope.File!=file||plan.Envelope.Client!=source.ClientId
                                ||plan.Envelope.Template!=source.TemplateId||plan.Envelope.RequestHash!=UploadValidationPlanSigner.RequestHash(file,input.Review))
                                throw new ValidationPlanRejected("plan_request_changed");
                            if(plan.Envelope.NoOp) throw new ValidationPlanRejected("import_no_changes");
                            var payload=JsonSerializer.Deserialize<ValidationPlanPayload>(plan.Payload)??throw new ValidationPlanRejected("plan_payload_invalid");
                            if(payload.Format!=UploadValidationPlanSigner.Format||payload.RequestHash!=plan.Envelope.RequestHash||payload.Counts!=plan.Envelope.Counts)
                                throw new ValidationPlanRejected("plan_payload_invalid");
                            // Common plan row is locked separately from its immutable native receipt.
                            await using(var common=owned.CreateCommand("SELECT plan_id FROM public.upload_validation_plans WHERE plan_id=$1 AND status='ready' FOR UPDATE"))
                            {common.Parameters.AddWithValue(input.PlanId);if(await common.ExecuteScalarAsync(cancel) is null) throw new ValidationPlanRejected("plan_not_ready");}
                            await using(var lockRows=owned.CreateCommand("LOCK TABLE public.client_data IN SHARE ROW EXCLUSIVE MODE")) await lockRows.ExecuteNonQueryAsync(cancel);
                            await plans.VerifyCurrent(owned,payload,cancel);
                            await UploadDerivedFieldWriter.GuardAsync(owned,payload,session.TenantId!.Value,cancel);
                            async Task CheckBytes()
                            {
                                await using var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,65536,FileOptions.Asynchronous);
                                if(Convert.ToHexString(await SHA256.HashDataAsync(stream,cancel)).ToLowerInvariant()!=hash) throw new ValidationPlanRejected("import_source_changed");
                            }
                            await CheckBytes();await writer.ApplyAsync(owned,payload,file,input.OperationId,session.Username,cancel);
                            await UploadDerivedFieldWriter.ApplyAsync(owned,payload,file,session.Username,cancel);
                            await using(var status=owned.CreateCommand("UPDATE public.uploaded_files SET status='Imported',record_count=$2 WHERE id=$1 AND status IN('Pending','Validated')"))
                            {status.Parameters.AddWithValue(file);status.Parameters.AddWithValue(payload.Counts.Incoming);if(await status.ExecuteNonQueryAsync(cancel)!=1) throw new ValidationPlanRejected("import_source_changed");}
                            await using(var consume=owned.CreateCommand("DELETE FROM public.upload_validation_plans WHERE plan_id=$1 AND status='ready'"))
                            {consume.Parameters.AddWithValue(input.PlanId);if(await consume.ExecuteNonQueryAsync(cancel)!=1) throw new ValidationPlanRejected("plan_not_ready");}
                            var completion=Now;
                            var completionReceipt=await UploadImportReceiptService.CreateWithinAsync(owned,session,file,input.OperationId,payload,"imported",completion,sourceExpiry,options.Value,cancel,
                                configuration?.GetValue<long?>("Platform:DatabaseCapacityLimitBytes")??500000000);
                            var outcome=new UploadImportOutput(input.OperationId,input.PlanId,file,payload.Counts,completion,completionReceipt.Id,completionReceipt.Reconciliation.Status);
                            await audit.WriteAsync(owned,new AuditEvent(Guid.NewGuid(),input.OperationId,new LogActor("authenticated",session.UserId,session.Username),source.ClientId,
                                "UploadImported","UploadImport",JsonSerializer.SerializeToElement(new {tenant_id=session.TenantId,template_id=source.TemplateId,record_id=file,
                                    added=payload.Counts.Added,changed=payload.Counts.Changed,removed=payload.Counts.Removed})),cancel);
                            await using(var receipt=owned.CreateCommand("""
                                INSERT INTO public.platform_upload_imports(operation_id,plan_id,tenant_id,user_id,client_id,template_id,file_id,request_hash,signature,outcome,imported_at)
                                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
                                """))
                            {
                                receipt.Parameters.AddWithValue(input.OperationId);receipt.Parameters.AddWithValue(input.PlanId);receipt.Parameters.AddWithValue(session.TenantId!.Value);
                                receipt.Parameters.AddWithValue(session.UserId);receipt.Parameters.AddWithValue(source.ClientId);receipt.Parameters.AddWithValue(source.TemplateId);
                                receipt.Parameters.AddWithValue(file);receipt.Parameters.AddWithValue(plan.Envelope.RequestHash);receipt.Parameters.AddWithValue(input.Signature);
                                receipt.Parameters.AddWithValue(JsonSerializer.Serialize(outcome));receipt.Parameters.AddWithValue(outcome.ImportedAt);await receipt.ExecuteNonQueryAsync(cancel);
                            }
                            await CheckBytes();
                            if(plan.Envelope.ExpiresAt<=Now) throw new ValidationPlanRejected("plan_expired");
                            if(job is not null) await job.CompleteAsync(owned,new(outcome.ReceiptId!.Value,outcome.ControlStatus!,outcome),cancel);
                            return outcome;
                        },cancellation);
                    },transactionToken),token),deadline.Token,recheckAfterRead:false);
            return result.Status=="success"?new("success",result.Value):new(result.Status,Code:result.Code);
        }
        catch(PostgresCommitOutcomeUnknownException)
        {
            // A dropped COMMIT acknowledgement is never permission to replay.
            using var reconciliation=new CancellationTokenSource(TimeSpan.FromSeconds(5));
            try {var outcome=await Recover(session,file,input,reconciliation.Token);if(outcome is not null) return new("success",outcome);}
            catch(Exception error) when(error is NpgsqlException or OperationCanceledException or ValidationPlanRejected or PostgresCommitOutcomeUnknownException) { }
            return new("conflict",Code:"import_outcome_unknown_retry_same_operation");
        }
        catch(ValidationPlanRejected error) {return new(error.Code=="plan_access_denied"?"forbidden":"conflict",Code:error.Code);}
        catch(JsonException) {return new("review",Code:"import_payload_invalid");}
        catch(IdentityReview error) {return new("review",Code:error.Code);}
        catch(PostgresException error) when(error.SqlState is "42P01" or "42703") {return new("review",Code:"import_schema_review_required");}
        catch(PostgresException error) when(error.SqlState=="23505") {return new("conflict",Code:"import_identity_or_operation_conflict");}
        catch(PostgresException error) when(error.SqlState=="55P03") {return new("busy",Code:"import_lock_busy");}
        catch(PostgresException error) when(error.SqlState=="57014") {return new("timeout",Code:"import_database_timed_out");}
        catch(OperationCanceledException) when(!caller.IsCancellationRequested&&deadline.IsCancellationRequested) {return new("timeout",Code:"import_timed_out");}
        finally {requests.Release();}
    }

    private Task<UploadImportOutput?> Recover(VerifiedSession session,int file,UploadImportRequest input,CancellationToken ct)=>
        transactions.ExecuteAsync((db,token)=>plans.AuthorizeAsync(db,session,file,input.Review,false,(owned,_,cancel)=>ReadOutcome(owned,session,file,input,cancel),token),ct);

    private static async Task<UploadImportOutput?> ReadOutcome(PostgresTransactionContext db,VerifiedSession session,int file,UploadImportRequest input,CancellationToken ct)
    {
        // Check both identifiers globally to detect reusing an operation ID for
        // different input, but never return another owner's receipt.
        await using var command=db.CreateCommand("SELECT operation_id,plan_id,tenant_id,user_id,client_id,template_id,file_id,request_hash,signature,outcome::text FROM public.platform_upload_imports WHERE operation_id=$1 OR plan_id=$2 FOR SHARE");
        command.Parameters.AddWithValue(input.OperationId);command.Parameters.AddWithValue(input.PlanId);
        await using var reader=await command.ExecuteReaderAsync(ct);UploadImportOutput? result=null;var source=input.Review.Coverage.Footing.Validation.Source;
        while(await reader.ReadAsync(ct))
        {
            if(reader.GetString(1)!=input.PlanId||reader.GetInt32(2)!=session.TenantId||reader.GetInt32(3)!=session.UserId
                ||reader.GetInt32(4)!=source.ClientId||reader.GetInt32(5)!=source.TemplateId||reader.GetInt32(6)!=file
                ||reader.GetString(7)!=UploadValidationPlanSigner.RequestHash(file,input.Review)||reader.GetString(8)!=input.Signature)
                throw new ValidationPlanRejected("import_operation_reused");
            result=JsonSerializer.Deserialize<UploadImportOutput>(reader.GetString(9))??throw new ValidationPlanRejected("import_receipt_invalid");
        }
        return result;
    }
}
