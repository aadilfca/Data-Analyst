using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Uploads;

public sealed class UploadJobService(UploadJobStore store,UploadStagingService staging,UploadJobSourceStore sources,
    UploadImportService imports,UploadImportReceiptService receipts,IOptions<UploadJobOptions> options,ILogger<UploadJobService> logger)
{
    private readonly Guid worker=Guid.NewGuid();
    public Task<UploadJobResult> SubmitAsync(VerifiedSession? session,int file,UploadJobRequest input,CancellationToken ct=default)=>Public(session,async (actor,token)=>
    {
        if(input?.Review?.Coverage?.Footing?.Validation?.Source is null||file<1||input.RequestId==Guid.Empty||input.Format!=1||input.Kind is not ("import" or "no_changes")
            ||input.Kind=="import"&&(input.PlanId is not {Length:32}||input.Signature is not {Length:64})
            ||input.Kind=="no_changes"&&(input.PlanId is not null||input.Signature is not null)) return new("invalid",Code:"invalid_job_request");
        return new("success",store.Output(await store.CreateAsync(actor,file,input,token)));
    },ct);
    public Task<UploadJobResult> GetAsync(VerifiedSession? session,int file,Guid id,CancellationToken ct=default)=>Access(session,file,id,"status",ct);
    public Task<UploadJobResult> CancelAsync(VerifiedSession? session,int file,Guid id,CancellationToken ct=default)=>Access(session,file,id,"cancel",ct);
    public Task<UploadJobResult> ResumeAsync(VerifiedSession? session,int file,Guid id,CancellationToken ct=default)=>Access(session,file,id,"resume",ct);
    private Task<UploadJobResult> Access(VerifiedSession? session,int file,Guid id,string action,CancellationToken ct)=>Public(session,async (actor,token)=>
        id==Guid.Empty||file<1?new("invalid",Code:"invalid_job_request"):new("success",store.Output(await store.AccessAsync(actor,file,id,action,token))),ct);
    private async Task<UploadJobResult> Public(VerifiedSession? session,Func<VerifiedSession,CancellationToken,Task<UploadJobResult>> action,CancellationToken ct)
    {
        if(!options.Value.Enabled) return new("unavailable");if(session is null) return new("unauthenticated");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(30));
        try {return await action(session,deadline.Token);}
        catch(UploadJobRejected e) {return new(e.Code=="job_not_found"?"not_found":e.Code.Contains("capacity",StringComparison.Ordinal)?"busy":"conflict",Code:e.Code);}
        catch(ValidationPlanRejected e) {return new(e.Code=="plan_access_denied"?"forbidden":"conflict",Code:e.Code);}
        catch(PostgresException e) when(e.SqlState=="55P03") {return new("busy",Code:"job_finishing_or_busy");}
        catch(PostgresException e) when(e.SqlState=="23505") {return new("conflict",Code:"job_retry_same_request");}
        catch(PostgresException e) when(e.SqlState is "42P01" or "42703") {return new("review",Code:"job_schema_review_required");}
        catch(PostgresCommitOutcomeUnknownException) {return new("conflict",Code:"job_outcome_unknown_retry_same_request");}
        catch(OperationCanceledException) when(!ct.IsCancellationRequested) {return new("timeout",Code:"job_request_timed_out");}
    }

    // Also used by isolated tests: one bounded claim, never a second queue.
    public async Task<bool> RunOnceAsync(CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return false;
        var job=await store.ClaimAsync(worker,ct);if(job is null) return false;
        using var lifetime=CancellationTokenSource.CreateLinkedTokenSource(ct);
        var heartbeat=Beat(job,lifetime);
        try
        {
            if(job.Request.Format!=1||UploadJobStore.Digest(job.File,job.Request)!=job.Digest) throw new UploadJobRejected("job_format_invalid");
            var session=await store.SessionAsync(job,lifetime.Token);
            if(session is null) {await store.FailureAsync(job,"authorization_required","job_authorization_required",lifetime.Token);return true;}
            await staging.RestoreJobSourceAsync(session,job,sources,lifetime.Token);
            if(!job.SourceReady)
            {
                var source=job.Request.Review.Coverage.Footing.Validation.Source;
                var saved=await staging.ReadOwnedAsync(session,job.Client,job.Template,job.File,job.SourceHash,source.ConfigurationKey,
                    async(path,_,__,___,token)=>{await sources.EnsureStoredAsync(job,path,token);return true;},lifetime.Token);
                if(saved.Status!="success") {await FailResult(job,saved.Status,saved.Code,lifetime.Token);return true;}
                await store.MarkSourceAsync(job,lifetime.Token);
            }
            await store.StageAsync(job,"checking_and_importing",lifetime.Token);
            var execution=new UploadJobExecution(job,store);
            string status;string? code;
            if(job.Request.Kind=="import")
            {
                var result=await imports.ImportJobAsync(session,job.File,new(job.Id,job.Request.PlanId!,job.Request.Signature!,job.Request.Review),execution,lifetime.Token);
                status=result.Status;code=result.Code;
            }
            else
            {
                var result=await receipts.CheckJobAsync(session,job.File,new(job.Id,job.Request.Review),execution,lifetime.Token);status=result.Status;code=result.Code;
            }
            if(status=="success") await store.FinishRecoveredAsync(job,lifetime.Token);
            else await FailResult(job,status,code,lifetime.Token);
        }
        catch(OperationCanceledException) when(ct.IsCancellationRequested) {throw;} // Leave lease for restart reconciliation.
        catch(OperationCanceledException) {await SafeFailure(job,"retry_wait","job_attempt_timed_out",ct);}
        catch(ValidationPlanRejected e) {await SafeFailure(job,e.Code=="plan_access_denied"?"authorization_required":"review_required",e.Code,ct);}
        catch(UploadJobRejected e) {await SafeFailure(job,"review_required",e.Code,ct);}
        catch(PostgresCommitOutcomeUnknownException) {await SafeFailure(job,"recovering","job_commit_outcome_unknown",ct);}
        catch(PostgresException e) when(e.SqlState is "40001" or "40P01" or "55P03" or "57014") {await SafeFailure(job,"retry_wait","job_database_busy",ct);}
        catch(PostgresException) {await SafeFailure(job,"review_required","job_database_review_required",ct);}
        catch(Exception e) when(e is HttpRequestException or IOException or NpgsqlException or TimeoutException)
        {await SafeFailure(job,"retry_wait","job_transport_unavailable",ct);}
        catch(Exception e) when(e is JsonException or InvalidOperationException or FormatException)
        {await SafeFailure(job,"review_required","job_input_or_configuration_review",ct);}
        finally
        {
            await lifetime.CancelAsync();try {await heartbeat;} catch(OperationCanceledException) { }
        }
        return true;
    }
    private async Task Beat(UploadJobData job,CancellationTokenSource lifetime)
    {
        using var timer=new PeriodicTimer(TimeSpan.FromSeconds(options.Value.HeartbeatSeconds));
        try
        {
            while(await timer.WaitForNextTickAsync(lifetime.Token))
                if(!await store.HeartbeatAsync(job,lifetime.Token)) {await lifetime.CancelAsync();return;}
        }
        catch(OperationCanceledException) when(lifetime.IsCancellationRequested) { }
        catch(Exception e) when(e is NpgsqlException or TimeoutException or PostgresCommitOutcomeUnknownException)
        {
            // Losing the coordinator must not leave an unfenced worker running.
            await lifetime.CancelAsync();
        }
    }
    private Task FailResult(UploadJobData job,string status,string? code,CancellationToken ct)=>store.FailureAsync(job,
        code is "import_outcome_unknown_retry_same_operation" or "receipt_outcome_unknown_retry_same_request"?"recovering":
        status is "forbidden" or "unauthenticated"?"authorization_required":status is "busy" or "timeout"?"retry_wait":"review_required",code??"job_requires_review",ct);
    private async Task SafeFailure(UploadJobData job,string state,string code,CancellationToken ct)
    {
        try {await store.FailureAsync(job,state,code,ct);}
        catch(Exception e) when(e is NpgsqlException or OperationCanceledException or PostgresCommitOutcomeUnknownException or TimeoutException)
        {logger.LogWarning("Upload job state requires lease recovery; category {Category}",code);}
    }
    public async Task CleanupAsync(CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return;
        foreach(var item in await store.CleanupCandidatesAsync(ct))
        {
            await sources.DeleteAsync(item,ct);await staging.CleanupJobRestoresAsync(item.Id,item.Version,ct);await store.DeletedAsync(item.Id,ct);
        }
    }
}
