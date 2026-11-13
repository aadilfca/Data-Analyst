using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Uploads;

public sealed class UploadJobStore(PostgresTransactions transactions,UploadValidationPlanStore plans,UploadValidationPlanSigner signer,
    PostgresSessionStore sessions,PostgresAuditWriter audit,IOptions<UploadJobOptions> options,IConfiguration configuration)
{
    private const string Columns="job_id,tenant_id,user_id,client_id,template_id,file_id,request_text,input_hash,session_hash,context_version,state,stage,attempts,lease_version,worker_id,lease_until,expires_at,source_expires_at,source_bytes,source_hash,source_ready,code,completion,storage_hash";
    internal static string Hash(string text)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    internal static string StorageIdentity(UploadJobOptions settings)=>Hash(settings.StorageUrl.TrimEnd('/')+"\n"+settings.StorageBucket);
    internal static string Digest(int file,UploadJobRequest input)=>Hash(JsonSerializer.Serialize(new {file,input.Format,input.Kind,input.PlanId,input.Signature,review=UploadValidationPlanSigner.RequestHash(file,input.Review)}));
    private static UploadJobData Data(NpgsqlDataReader r)=>new(r.GetGuid(0),r.GetInt32(1),r.GetInt32(2),r.GetInt32(3),r.GetInt32(4),r.GetInt32(5),
        JsonSerializer.Deserialize<UploadJobRequest>(r.GetString(6))??throw new UploadJobRejected("job_format_invalid"),r.GetString(7),r.GetString(8),r.GetInt64(9),
        r.GetString(10),r.GetString(11),r.GetInt32(12),r.GetInt64(13),r.IsDBNull(14)?null:r.GetGuid(14),r.IsDBNull(15)?null:new(r.GetDateTime(15)),
        new(r.GetDateTime(16)),new(r.GetDateTime(17)),r.GetInt64(18),r.GetString(19),r.GetBoolean(20),r.IsDBNull(21)?null:r.GetString(21),
        r.IsDBNull(22)?null:JsonSerializer.Deserialize<UploadJobCompletion>(r.GetString(22)),r.GetString(23));
    internal UploadJobOutput Output(UploadJobData j)=>new(j.Id,j.File,j.State,j.Stage,j.Attempts,options.Value.MaximumAttempts,j.Expires,j.SourceReady,j.Code,j.Completion);
    private static async Task<UploadJobData?> Read(PostgresTransactionContext db,Guid id,CancellationToken ct,string locking="")
    {
        await using var c=db.CreateCommand($"SELECT {Columns} FROM public.platform_upload_jobs WHERE job_id=$1 {locking}");c.Parameters.AddWithValue(id);
        await using var r=await c.ExecuteReaderAsync(ct);return await r.ReadAsync(ct)?Data(r):null;
    }
    private static void Owner(VerifiedSession s,int file,UploadJobData j)
    {if(j.Tenant!=s.TenantId||j.User!=s.UserId||j.File!=file) throw new UploadJobRejected("job_not_found");}

    internal Task<UploadJobData> CreateAsync(VerifiedSession actor,int file,UploadJobRequest input,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        var source=input.Review.Coverage.Footing.Validation.Source;
        return await plans.AuthorizeScopeAsync(db,actor,source.ClientId,source.TemplateId,async(_,__)=>
        {
            var prior=await Read(db,input.RequestId,token);
            if(prior is null&&input.PlanId is not null)
            {
                await using var find=db.CreateCommand($"SELECT {Columns} FROM public.platform_upload_jobs WHERE plan_id=$1");find.Parameters.AddWithValue(input.PlanId);
                await using var r=await find.ExecuteReaderAsync(token);if(await r.ReadAsync(token)) prior=Data(r);
            }
            if(prior is not null)
            {Owner(actor,file,prior);if(prior.Digest!=Digest(file,input)) throw new UploadJobRejected("job_request_reused");return prior;}
            return await plans.AuthorizeAsync(db,actor,file,input.Review,true,async(context,sourceExpiry,cancel)=>
            {
                DateTimeOffset expiry=DateTimeOffset.UtcNow.AddMinutes(30);
                if(input.Kind=="import")
                {
                    var p=await plans.Read(context,actor,null,input.PlanId,cancel)??throw new ValidationPlanRejected("plan_not_found");
                    UploadValidationPlanStore.Validate(p,signer,DateTimeOffset.UtcNow);
                    if(p.Envelope.NoOp||p.Envelope.File!=file||p.Envelope.Client!=source.ClientId||p.Envelope.Template!=source.TemplateId
                        ||p.Envelope.RequestHash!=UploadValidationPlanSigner.RequestHash(file,input.Review)||!signer.Verify(p.Envelope,input.Signature!))
                        throw new ValidationPlanRejected("plan_request_changed");
                    expiry=p.Envelope.ExpiresAt;
                }
                if(sourceExpiry<expiry) expiry=sourceExpiry;
                long bytes;
                await using(var staged=context.CreateCommand("SELECT byte_count FROM public.platform_upload_staging WHERE upload_id=$1 AND sha256=$2"))
                {staged.Parameters.AddWithValue(file);staged.Parameters.AddWithValue(source.Sha256!);bytes=Convert.ToInt64(await staged.ExecuteScalarAsync(cancel)??0);}
                if(bytes<1||bytes>options.Value.MaximumFileBytes) throw new UploadJobRejected("job_source_capacity");
                // Serialize reservations across scopes. External bytes remain reserved until deletion is confirmed.
                await using(var serial=context.CreateCommand("SELECT pg_advisory_xact_lock(746128)")) await serial.ExecuteNonQueryAsync(cancel);
                await using(var clean=context.CreateCommand("DELETE FROM public.platform_upload_jobs WHERE job_id IN(SELECT job_id FROM public.platform_upload_jobs WHERE source_deleted AND updated_at<now()-interval '24 hours' AND state IN('succeeded','failed','cancelled','expired','review_required') ORDER BY updated_at LIMIT 32)")) await clean.ExecuteNonQueryAsync(cancel);
                await using(var budget=context.CreateCommand("""
                    SELECT count(*) FILTER(WHERE state IN('storing_source','queued','running','retry_wait','recovering','authorization_required')),
                        count(*) FILTER(WHERE user_id=$1 AND tenant_id=$2 AND state IN('storing_source','queued','running','retry_wait','recovering','authorization_required')),
                        COALESCE(sum(source_bytes) FILTER(WHERE NOT source_deleted),0)::bigint,
                        count(*) FILTER(WHERE tenant_id=$2 AND user_id=$1 AND client_id=$3 AND template_id=$4)
                    FROM public.platform_upload_jobs
                    """))
                {
                    budget.Parameters.AddWithValue(actor.UserId);budget.Parameters.AddWithValue(actor.TenantId!.Value);budget.Parameters.AddWithValue(source.ClientId);budget.Parameters.AddWithValue(source.TemplateId);
                    await using var r=await budget.ExecuteReaderAsync(cancel);await r.ReadAsync(cancel);
                    if(r.GetInt64(0)>=options.Value.MaximumOutstandingJobs||r.GetInt64(1)>=options.Value.MaximumJobsPerOwner
                        ||r.GetInt64(2)+bytes>options.Value.MaximumSourceBytes||r.GetInt64(3)>=options.Value.MaximumRetainedJobsPerScope) throw new UploadJobRejected("job_capacity");
                }
                string text=JsonSerializer.Serialize(input);
                if(Encoding.UTF8.GetByteCount(text)>1048576) throw new UploadJobRejected("job_request_capacity");
                await using(var size=context.CreateCommand("SELECT pg_database_size(current_database())"))
                    if((long)(await size.ExecuteScalarAsync(cancel))!+Encoding.UTF8.GetByteCount(text)*4L+1048576>=Math.Min(500000000,configuration.GetValue<long?>("Platform:DatabaseCapacityLimitBytes")??500000000))
                        throw new UploadJobRejected("job_storage_capacity");
                await using(var insert=context.CreateCommand("""
                    INSERT INTO public.platform_upload_jobs(job_id,tenant_id,user_id,client_id,template_id,file_id,kind,plan_id,input_hash,request_text,
                        session_hash,context_version,source_hash,source_bytes,source_expires_at,expires_at,storage_hash)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                    """))
                {
                    insert.Parameters.AddWithValue(input.RequestId);insert.Parameters.AddWithValue(actor.TenantId.Value);insert.Parameters.AddWithValue(actor.UserId);
                    insert.Parameters.AddWithValue(source.ClientId);insert.Parameters.AddWithValue(source.TemplateId);insert.Parameters.AddWithValue(file);insert.Parameters.AddWithValue(input.Kind);
                    insert.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)input.PlanId??DBNull.Value);insert.Parameters.AddWithValue(Digest(file,input));insert.Parameters.AddWithValue(text);
                    insert.Parameters.AddWithValue(Hash(actor.Token));insert.Parameters.AddWithValue(actor.ContextVersion);insert.Parameters.AddWithValue(source.Sha256!);
                    insert.Parameters.AddWithValue(bytes);insert.Parameters.AddWithValue(sourceExpiry);insert.Parameters.AddWithValue(expiry);insert.Parameters.AddWithValue(StorageIdentity(options.Value));await insert.ExecuteNonQueryAsync(cancel);
                }
                var job=(await Read(context,input.RequestId,cancel))!;await Audit(context,job,actor.Username,"UploadJobSubmitted",cancel);return job;
            },token);
        },token);
    },ct);

    internal Task<UploadJobData> AccessAsync(VerifiedSession actor,int file,Guid id,string action,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        var known=await Read(db,id,token)??throw new UploadJobRejected("job_not_found");Owner(actor,file,known);
        // Progress/cancellation must not wait behind the accounting writer's
        // global configuration lock. These owner-only metadata actions perform
        // live access checks; the importer separately pins full authorization.
        await AuthorizeAccess(db,actor,known,token);
        {
            if(action=="status") return (await Read(db,id,token))!;
            var job=await Read(db,id,token,"FOR UPDATE NOWAIT")??throw new UploadJobRejected("job_not_found");
            if(job.State is "succeeded" or "cancelled" or "failed" or "expired" or "review_required") return job;
            if(await ReconcileWithin(db,job,token)) return (await Read(db,id,token))!;
            if(action=="cancel")
            {
                await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET state='cancelled',stage='cancelled',worker_id=NULL,lease_until=NULL,lease_version=lease_version+1,updated_at=now() WHERE job_id=$1");
                c.Parameters.AddWithValue(id);await c.ExecuteNonQueryAsync(token);await Audit(db,job,actor.Username,"UploadJobCancelled",token);
            }
            else if(action=="resume")
            {
                if(job.State!="authorization_required"||job.Expires<=DateTimeOffset.UtcNow||job.Attempts>=options.Value.MaximumAttempts) throw new UploadJobRejected("job_cannot_resume");
                // Explicitly attach this owner's fresh session; never adopt another login silently.
                await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET state='queued',stage='queued',session_hash=$2,context_version=$3,code=NULL,next_attempt_at=now(),updated_at=now() WHERE job_id=$1");
                c.Parameters.AddWithValue(id);c.Parameters.AddWithValue(Hash(actor.Token));c.Parameters.AddWithValue(actor.ContextVersion);await c.ExecuteNonQueryAsync(token);
                await Audit(db,job,actor.Username,"UploadJobResumed",token);
            }
            return (await Read(db,id,token))!;
        }
    },ct);

    private static async Task AuthorizeAccess(PostgresTransactionContext db,VerifiedSession actor,UploadJobData j,CancellationToken ct)
    {
        if(!actor.TenantContextRequired||actor.TenantId!=j.Tenant) throw new ValidationPlanRejected("plan_access_denied");
        await using var c=db.CreateCommand("""
            SELECT 1 FROM public.sessions s JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.tenant_id=$3
            JOIN public.platform_tenants t ON t.id=m.tenant_id
            JOIN public.clients c ON c.id=s.selected_client_id
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=$3
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE AND m.is_active IS TRUE AND t.is_active IS TRUE
              AND s.selected_tenant_id=$3 AND s.context_version=$4 AND s.selected_client_id=$5 AND c.is_active IS TRUE
              AND (m.tenant_role='admin' OR EXISTS(SELECT 1 FROM public.platform_engagement_assignments a
                JOIN public.platform_audit_engagements e ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                WHERE a.firm_tenant_id=$3 AND a.user_id=$2 AND e.client_id=$5 AND a.is_active IS TRUE AND e.is_active IS TRUE AND a.team_role IN('manager','internee')))
            """);
        c.Parameters.AddWithValue(actor.Token);c.Parameters.AddWithValue(actor.UserId);c.Parameters.AddWithValue(j.Tenant);c.Parameters.AddWithValue(actor.ContextVersion);c.Parameters.AddWithValue(j.Client);
        if(await c.ExecuteScalarAsync(ct) is null) throw new ValidationPlanRejected("plan_access_denied");
    }

    internal Task<UploadJobData?> ClaimAsync(Guid worker,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        await using(var serial=db.CreateCommand("SELECT pg_advisory_xact_lock(746128)")) await serial.ExecuteNonQueryAsync(token);
        await using(var count=db.CreateCommand("SELECT count(*) FROM public.platform_upload_jobs WHERE state='running' AND lease_until>now()"))
            if((long)(await count.ExecuteScalarAsync(token))!>=options.Value.MaximumWorkers) return null;
        UploadJobData? job;
        await using(var c=db.CreateCommand($"SELECT {Columns} FROM public.platform_upload_jobs WHERE (state IN('storing_source','queued','retry_wait','recovering') AND next_attempt_at<=now() OR state='running' AND lease_until<=now() OR state='authorization_required' AND expires_at<=now()) ORDER BY created_at,job_id LIMIT 1 FOR UPDATE SKIP LOCKED"))
        {await using var r=await c.ExecuteReaderAsync(token);job=await r.ReadAsync(token)?Data(r):null;}
        if(job is null) return null;
        // Reconciliation also runs after expiry and retry exhaustion. It never writes accounting rows.
        if(await ReconcileWithin(db,job,token)) return null;
        if(job.Expires<=DateTimeOffset.UtcNow||job.Attempts>=options.Value.MaximumAttempts)
        {
            await using var stop=db.CreateCommand("UPDATE public.platform_upload_jobs SET state=$2,stage=$2,code=$3,worker_id=NULL,lease_until=NULL,updated_at=now() WHERE job_id=$1");
            stop.Parameters.AddWithValue(job.Id);stop.Parameters.AddWithValue(job.Expires<=DateTimeOffset.UtcNow?"expired":"failed");stop.Parameters.AddWithValue("job_expired_or_attempts_exhausted");await stop.ExecuteNonQueryAsync(token);return null;
        }
        await using(var claim=db.CreateCommand("UPDATE public.platform_upload_jobs SET state='running',stage=CASE WHEN source_ready THEN 'checking' ELSE 'storing_source' END,attempts=attempts+1,lease_version=lease_version+1,worker_id=$2,lease_until=now()+make_interval(secs=>$3),updated_at=now() WHERE job_id=$1"))
        {claim.Parameters.AddWithValue(job.Id);claim.Parameters.AddWithValue(worker);claim.Parameters.AddWithValue(options.Value.LeaseSeconds);await claim.ExecuteNonQueryAsync(token);}
        return await Read(db,job.Id,token);
    },ct);

    internal async Task<VerifiedSession?> SessionAsync(UploadJobData job,CancellationToken ct)
    {
        string? token=await transactions.ExecuteAsync(async(db,cancel)=>
        {
            await using var c=db.CreateCommand("SELECT token FROM public.sessions WHERE user_id=$1 AND selected_tenant_id=$2 AND selected_client_id=$3 AND context_version=$4 AND expiry>now() AND encode(sha256(convert_to(token,'UTF8')),'hex')=$5");
            c.Parameters.AddWithValue(job.User);c.Parameters.AddWithValue(job.Tenant);c.Parameters.AddWithValue(job.Client);c.Parameters.AddWithValue(job.ContextVersion);c.Parameters.AddWithValue(job.SessionHash);
            return (string?)await c.ExecuteScalarAsync(cancel);
        },ct);
        return token is null?null:await sessions.FindAsync(token,ct);
    }

    internal Task<bool> HeartbeatAsync(UploadJobData job,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        // A locked import row is still owned; do not queue heartbeats behind its transaction.
        var current=await Read(db,job.Id,token);
        if(current is null||current.Version!=job.Version||current.Worker!=job.Worker||current.State!="running") return false;
        await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET lease_until=now()+make_interval(secs=>$4) WHERE job_id IN(SELECT job_id FROM public.platform_upload_jobs WHERE job_id=$1 AND lease_version=$2 AND worker_id=$3 AND lease_until>now() FOR UPDATE SKIP LOCKED)");
        c.Parameters.AddWithValue(job.Id);c.Parameters.AddWithValue(job.Version);c.Parameters.AddWithValue(job.Worker!.Value);c.Parameters.AddWithValue(options.Value.LeaseSeconds);await c.ExecuteNonQueryAsync(token);return true;
    },ct);

    internal async Task GuardAsync(PostgresTransactionContext db,UploadJobData expected,CancellationToken ct)
    {
        var j=await Read(db,expected.Id,ct,"FOR UPDATE")??throw new UploadJobRejected("job_ownership_lost");
        if(j.State!="running"||j.Version!=expected.Version||j.Worker!=expected.Worker||j.Lease is null||j.Lease.Value<=DateTimeOffset.UtcNow||j.Expires<=DateTimeOffset.UtcNow||j.Digest!=expected.Digest)
            throw new UploadJobRejected("job_ownership_lost");
        // The row lock pins execution through COMMIT. Cancellation uses NOWAIT and truthfully reports finishing.
    }
    internal async Task CompleteAsync(PostgresTransactionContext db,UploadJobData job,UploadJobCompletion result,CancellationToken ct)
    {
        await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET state='succeeded',stage='completed',completion=$4,code=NULL,worker_id=NULL,lease_until=NULL,updated_at=now() WHERE job_id=$1 AND lease_version=$2 AND worker_id=$3 AND state='running'");
        c.Parameters.AddWithValue(job.Id);c.Parameters.AddWithValue(job.Version);c.Parameters.AddWithValue(job.Worker!.Value);c.Parameters.AddWithValue(JsonSerializer.Serialize(result));
        if(await c.ExecuteNonQueryAsync(ct)!=1) throw new UploadJobRejected("job_ownership_lost");
        // Preserve the verified owner's actual audit attribution in this same
        // transaction; the background worker is an executor, not a new user.
        await using var actor=db.CreateCommand("SELECT username FROM public.users WHERE id=$1");actor.Parameters.AddWithValue(job.User);
        string username=(string?)await actor.ExecuteScalarAsync(ct)??throw new UploadJobRejected("job_authorization_required");
        await Audit(db,job,username,"UploadJobCompleted",ct);
    }
    internal Task MarkSourceAsync(UploadJobData job,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        await GuardAsync(db,job,token);await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET source_ready=TRUE,stage='checking',updated_at=now() WHERE job_id=$1");c.Parameters.AddWithValue(job.Id);await c.ExecuteNonQueryAsync(token);return true;
    },ct);
    internal Task StageAsync(UploadJobData job,string stage,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {await GuardAsync(db,job,token);await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET stage=$2,updated_at=now() WHERE job_id=$1");c.Parameters.AddWithValue(job.Id);c.Parameters.AddWithValue(stage);return await c.ExecuteNonQueryAsync(token);},ct);
    internal Task FinishRecoveredAsync(UploadJobData job,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {var current=await Read(db,job.Id,token,"FOR UPDATE");return current is not null&&await ReconcileWithin(db,current,token);},ct);

    private static async Task<bool> ReconcileWithin(PostgresTransactionContext db,UploadJobData j,CancellationToken ct)
    {
        if(j.State=="succeeded") return true;
        UploadJobCompletion? result=null;string reviewHash=UploadValidationPlanSigner.RequestHash(j.File,j.Request.Review);
        if(j.Request.Kind=="import")
        {
            await using var c=db.CreateCommand("SELECT outcome::text FROM public.platform_upload_imports WHERE plan_id=$1 AND tenant_id=$2 AND user_id=$3 AND file_id=$4 AND client_id=$5 AND template_id=$6 AND request_hash=$7 AND signature=$8");
            c.Parameters.AddWithValue(j.Request.PlanId!);c.Parameters.AddWithValue(j.Tenant);c.Parameters.AddWithValue(j.User);c.Parameters.AddWithValue(j.File);c.Parameters.AddWithValue(j.Client);c.Parameters.AddWithValue(j.Template);c.Parameters.AddWithValue(reviewHash);c.Parameters.AddWithValue(j.Request.Signature!);
            if(await c.ExecuteScalarAsync(ct) is string text)
            {var o=JsonSerializer.Deserialize<UploadImportOutput>(text)??throw new UploadJobRejected("job_outcome_invalid");result=new(o.ReceiptId??o.OperationId,o.ControlStatus??"unavailable",o);}
        }
        else
        {
            await using var c=db.CreateCommand("SELECT receipt FROM public.platform_upload_import_receipts WHERE receipt_id=$1 AND tenant_id=$2 AND user_id=$3 AND file_id=$4 AND client_id=$5 AND template_id=$6 AND kind='no_changes' AND request_hash=$7");
            c.Parameters.AddWithValue(j.Id);c.Parameters.AddWithValue(j.Tenant);c.Parameters.AddWithValue(j.User);c.Parameters.AddWithValue(j.File);c.Parameters.AddWithValue(j.Client);c.Parameters.AddWithValue(j.Template);c.Parameters.AddWithValue(reviewHash);
            if(await c.ExecuteScalarAsync(ct) is string text)
            {var receipt=JsonSerializer.Deserialize<UploadImportReceipt>(text)??throw new UploadJobRejected("job_outcome_invalid");result=new(receipt.Id,receipt.Reconciliation.Status);}
        }
        if(result is null) return false;
        await using var update=db.CreateCommand("UPDATE public.platform_upload_jobs SET state='succeeded',stage='completed',completion=$2,code=NULL,worker_id=NULL,lease_until=NULL,updated_at=now() WHERE job_id=$1");
        update.Parameters.AddWithValue(j.Id);update.Parameters.AddWithValue(JsonSerializer.Serialize(result));await update.ExecuteNonQueryAsync(ct);return true;
    }

    internal Task FailureAsync(UploadJobData job,string state,string code,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        var current=await Read(db,job.Id,token,"FOR UPDATE");
        if(current is null||current.Version!=job.Version||current.Worker!=job.Worker||current.State!="running") return false;
        if(await ReconcileWithin(db,current,token)) return true;
        int delay=job.Attempts<=1?2:10;
        await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET state=$4,stage=$4,code=$5,next_attempt_at=now()+make_interval(secs=>$6),worker_id=NULL,lease_until=NULL,updated_at=now() WHERE job_id=$1 AND lease_version=$2 AND worker_id=$3");
        c.Parameters.AddWithValue(job.Id);c.Parameters.AddWithValue(job.Version);c.Parameters.AddWithValue(job.Worker!.Value);c.Parameters.AddWithValue(state);c.Parameters.AddWithValue(code);c.Parameters.AddWithValue(delay);await c.ExecuteNonQueryAsync(token);return true;
    },ct);

    internal Task<IReadOnlyList<UploadJobCleanup>> CleanupCandidatesAsync(CancellationToken ct)=>transactions.ExecuteAsync<IReadOnlyList<UploadJobCleanup>>(async(db,token)=>
    {
        await using var c=db.CreateCommand("SELECT job_id,lease_version,storage_hash FROM public.platform_upload_jobs WHERE NOT source_deleted AND (source_expires_at<=now()-make_interval(secs=>$1) OR state IN('succeeded','failed','cancelled','expired','review_required') AND updated_at<now()-make_interval(secs=>$1)) ORDER BY created_at LIMIT 8");
        c.Parameters.AddWithValue(2*options.Value.TransferTimeoutSeconds+options.Value.LeaseSeconds);
        var ids=new List<UploadJobCleanup>();await using var r=await c.ExecuteReaderAsync(token);while(await r.ReadAsync(token)) ids.Add(new(r.GetGuid(0),r.GetInt64(1),r.GetString(2)));return ids;
    },ct);
    internal Task DeletedAsync(Guid id,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {await using var c=db.CreateCommand("UPDATE public.platform_upload_jobs SET source_deleted=TRUE WHERE job_id=$1 AND (source_expires_at<=now() OR state IN('succeeded','failed','cancelled','expired','review_required'))");c.Parameters.AddWithValue(id);return await c.ExecuteNonQueryAsync(token);},ct);
    private Task<int> Audit(PostgresTransactionContext db,UploadJobData j,string username,string action,CancellationToken ct)=>audit.WriteAsync(db,
        new AuditEvent(Guid.NewGuid(),j.Id,new LogActor("authenticated",j.User,username),j.Client,action,"UploadJob",JsonSerializer.SerializeToElement(new {tenant_id=j.Tenant,template_id=j.Template,record_id=j.File})),ct);
}
