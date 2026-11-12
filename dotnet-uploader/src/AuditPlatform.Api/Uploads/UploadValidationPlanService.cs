using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;
namespace AuditPlatform.Api.Uploads;

public sealed class UploadValidationPlanService(UploadCorrectionService corrections,UploadValidationPlanStore store,
    UploadValidationPlanSigner signer,IOptions<UploadValidationPlanOptions> options,TimeProvider? clock=null)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    private DateTimeOffset Now=>(clock??TimeProvider.System).GetUtcNow();
    public Task<ValidationPlanResult> CreateAsync(VerifiedSession? session,int file,ValidationPlanRequest input,CancellationToken ct=default)=>
        Run(session,file,input.Review,input.RequestId,null,ct);
    public Task<ValidationPlanResult> CheckAsync(VerifiedSession? session,int file,ValidationPlanCheck input,CancellationToken ct=default)=>
        Run(session,file,input.Review,Guid.Empty,input,ct);

    private async Task<ValidationPlanResult> Run(VerifiedSession? session,int file,CorrectionRequest review,Guid requestId,ValidationPlanCheck? check,CancellationToken ct)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(session is null) return new("unauthenticated");
        if(review?.Coverage?.Footing?.Validation?.Source is null||file<1||check is null&&requestId==Guid.Empty
            ||check is not null&&(check.PlanId is not {Length:32}||check.Signature is not {Length:64})) return new("invalid",Code:"invalid_plan_request");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var caller=ct;using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            StoredValidationPlan? stored=null;
            if(check is not null)
            {
                stored=await store.FindAsync(session,file,review,check.PlanId,deadline.Token);
                UploadValidationPlanStore.Validate(stored,signer,Now);
                if(!signer.Verify(stored.Envelope,check.Signature)||stored.Envelope.File!=file||stored.Envelope.RequestHash!=UploadValidationPlanSigner.RequestHash(file,review))
                    throw new ValidationPlanRejected("plan_request_changed");
            }
            var captured=await corrections.CaptureAsync(session,file,review,deadline.Token);
            if(captured.Result.Status!="success") return new(captured.Result.Status,Code:captured.Result.Code);
            var evidence=captured.Evidence!;var output=evidence.Output;
            if(!output.CorrectionChecksPassed) return new("review",Code:output.Status);
            // Balance/coverage evidence is not yet certified by row comparison.
            if(evidence.Profile.AccountCode is not null) return new("review",Code:"ledger_plan_proof_required");
            if(evidence.Actions.Count>options.Value.MaximumRows||evidence.Existing.Count>options.Value.MaximumRows) return new("review",Code:"plan_row_capacity");
            if(stored is not null&&stored.Envelope.CorrectionKey!=output.CorrectionKey) throw new ValidationPlanRejected("plan_data_changed");
            var sourceRows=evidence.Incoming.ToDictionary(r=>r.Row);var oldRows=evidence.Existing.ToDictionary(r=>r.Id);
            var actions=new List<ValidationPlanAction>();
            foreach(var row in evidence.Actions)
            {
                deadline.Token.ThrowIfCancellationRequested();
                var original=row.ExistingId is {} id?oldRows[id]:null;
                var incoming=row.Row is {} position?sourceRows[position]:null;
                bool metadata=row.Action=="unchanged"&&(original!.SourceRow!=row.Row||original.OrderUpload is null
                    ||evidence.Profile.Columns.Where(c=>!c.Compare).Any(c=>UploadIdentityValue.RawComparison(original.Values.GetValueOrDefault(c.Key,new("blank")))
                        !=UploadIdentityValue.RawComparison(incoming!.Values.GetValueOrDefault(c.Key,new("blank")))));
                if(row.Action!="unchanged"||metadata)
                {
                    IReadOnlyDictionary<string,IdentityAtom>? values=incoming?.Values;
                    if(row.Action=="unchanged")
                    {
                        var updates=evidence.Profile.Columns.Where(c=>!c.Compare)
                            .Where(c=>UploadIdentityValue.RawComparison(original!.Values.GetValueOrDefault(c.Key,new("blank")))
                                !=UploadIdentityValue.RawComparison(incoming!.Values.GetValueOrDefault(c.Key,new("blank"))))
                            .ToDictionary(c=>c.Key,c=>incoming!.Values.GetValueOrDefault(c.Key,new("blank")),StringComparer.Ordinal);
                        values=updates.Count==0?null:updates;
                    }
                    actions.Add(new(row,values,row.Action=="unchanged"?null:incoming?.PrimaryDate));
                }
            }
            var payload=new ValidationPlanPayload(UploadValidationPlanSigner.Format,UploadValidationPlanSigner.Normalize(review),
                UploadValidationPlanSigner.RequestHash(file,review),output.CorrectionKey,output.StoreKey,output.Counts,actions,
                evidence.Existing.Select(r=>new ValidationPlanGuard(r.Id,UploadValidationPlanSigner.GuardHash(r))).ToArray(),
                evidence.Incoming.Select(r=>UploadIdentityValue.Group(evidence.Profile,r.Values)).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray(),
                evidence.Actions.Where(r=>r.Action!="unchanged").Select(r=>r.DocumentGroup).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray());
            // Never truncate an actionable plan. Lossless scalar atoms keep the
            // original numeric lexeme and validated calendar date separate.
            if(!output.NoOp) UploadPreparationService.JsonSize(payload,options.Value.MaximumPayloadBytes);
            string text=output.NoOp?"":JsonSerializer.Serialize(payload);
            if(Encoding.UTF8.GetByteCount(text)>options.Value.MaximumPayloadBytes) throw new ValidationPlanRejected("plan_payload_capacity");
            if(stored is not null)
            {
                await store.VerifyAsync(session,file,stored,payload,signer,deadline.Token);return Success(stored,"valid");
            }
            try {stored=await store.IssueAsync(session,file,requestId,payload,text,signer,output.NoOp,deadline.Token);}
            catch(PostgresCommitOutcomeUnknownException)
            {
                // Reconcile by the durable request ID; never repeat the write.
                using var reconcile=new CancellationTokenSource(TimeSpan.FromSeconds(5));
                try
                {
                    stored=await store.ReconcileAsync(session,file,requestId,review,reconcile.Token);
                    UploadValidationPlanStore.Validate(stored,signer,Now);
                    if(stored.Envelope.RequestHash!=payload.RequestHash||stored.Envelope.CorrectionKey!=payload.CorrectionKey)
                        return new("conflict",Code:"plan_request_reused");
                }
                catch(Exception error) when(error is ValidationPlanRejected or NpgsqlException or OperationCanceledException or PostgresCommitOutcomeUnknownException)
                {return new("conflict",Code:"plan_outcome_unknown_retry_same_request");}
            }
            return Success(stored,stored.Envelope.NoOp?"no_changes":"ready");
        }
        catch(ValidationPlanRejected error) {return new(error.Code=="plan_access_denied"?"forbidden":"conflict",Code:error.Code);}
        catch(UploadPreparationService.InvalidPreparation) {return new("review",Code:"plan_payload_capacity");}
        catch(IdentityReview error) {return new("review",Code:error.Code);}
        catch(JsonException) {return new("review",Code:"plan_payload_invalid");}
        catch(PostgresException error) when(error.SqlState is "42P01" or "42703") {return new("review",Code:"plan_schema_review_required");}
        catch(PostgresException error) when(error.SqlState=="23505") {return new("conflict",Code:"plan_request_reused");}
        catch(PostgresException error) when(error.SqlState=="55P03") {return new("busy",Code:"plan_lock_busy");}
        catch(PostgresException error) when(error.SqlState=="57014") {return new("timeout",Code:"plan_database_timed_out");}
        catch(OperationCanceledException) when(!caller.IsCancellationRequested&&deadline.IsCancellationRequested) {return new("timeout",Code:"plan_timed_out");}
        finally {requests.Release();}
    }
    private static ValidationPlanResult Success(StoredValidationPlan stored,string status)=>new("success",new(status,stored.Envelope.RequestId,
        stored.Envelope.NoOp?null:stored.Envelope.PlanId,stored.Envelope.NoOp?null:stored.Signature,stored.Envelope.ExpiresAt,stored.Envelope.Counts,stored.Envelope.CorrectionKey));
}
