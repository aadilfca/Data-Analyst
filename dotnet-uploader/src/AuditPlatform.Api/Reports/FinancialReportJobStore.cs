using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.TrialBalance;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Reports;

internal sealed class FinancialReportJobStore(PostgresTransactions transactions,IOptions<FinancialReportJobOptions> options)
{
    public Task<FinancialReportJobResult> CreateAsync(VerifiedSession? actor,FinancialReportJobRequest request,
        string fingerprint,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        var scope=await TrialBalanceStorageStore.Authorize(db,actor,request.ClientId,false,token);
        string sessionHash=Hash(actor!.Token);string requestJson=JsonSerializer.Serialize(request);
        string requestHash=Hash(requestJson);var id=Guid.NewGuid();
        await using(var existing=db.CreateCommand("""
            SELECT job_id,request_hash,input_fingerprint FROM public.financial_report_jobs
            WHERE tenant_id=$1 AND user_id=$2 AND client_id=$3 AND request_id=$4
            """))
        {
            existing.Parameters.AddWithValue(scope.Tenant);existing.Parameters.AddWithValue(actor.UserId);
            existing.Parameters.AddWithValue(request.ClientId);existing.Parameters.AddWithValue(request.RequestId);
            await using var reader=await existing.ExecuteReaderAsync(token);
            if(await reader.ReadAsync(token))
            {
                var foundId=reader.GetGuid(0);string foundRequest=reader.GetString(1).Trim();string foundFingerprint=reader.GetString(2).Trim();
                await reader.DisposeAsync();
                if(foundRequest!=requestHash||foundFingerprint!=fingerprint)
                    return new FinancialReportJobResult("conflict",Code:"job_request_id_reused");
                return await ReadOutputAsync(db,foundId,token);
            }
        }
        await using(var count=db.CreateCommand("""
            SELECT count(*) FROM public.financial_report_jobs WHERE tenant_id=$1 AND user_id=$2
              AND state IN ('queued','running')
            """))
        {
            count.Parameters.AddWithValue(scope.Tenant);count.Parameters.AddWithValue(actor.UserId);
            if((long)(await count.ExecuteScalarAsync(token))!>=options.Value.MaximumOutstandingPerOwner)
                return new FinancialReportJobResult("unavailable",Code:"job_capacity");
        }
        await using(var insert=db.CreateCommand("""
            INSERT INTO public.financial_report_jobs(job_id,request_id,tenant_id,user_id,client_id,context_version,
                session_hash,request_hash,request_json,input_fingerprint,maximum_attempts,expires_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,now()+make_interval(hours=>$12))
            """))
        {
            insert.Parameters.AddWithValue(id);insert.Parameters.AddWithValue(request.RequestId);
            insert.Parameters.AddWithValue(scope.Tenant);insert.Parameters.AddWithValue(actor.UserId);
            insert.Parameters.AddWithValue(request.ClientId);insert.Parameters.AddWithValue(actor.ContextVersion);
            insert.Parameters.AddWithValue(sessionHash);insert.Parameters.AddWithValue(requestHash);
            insert.Parameters.AddWithValue(requestJson);insert.Parameters.AddWithValue(fingerprint);
            insert.Parameters.AddWithValue(options.Value.MaximumAttempts);insert.Parameters.AddWithValue(options.Value.RetentionHours);
            await insert.ExecuteNonQueryAsync(token);
        }
        return await ReadOutputAsync(db,id,token);
    },ct);

    public Task<FinancialReportJobResult> GetAsync(VerifiedSession? actor,Guid id,CancellationToken ct)=>
        transactions.ExecuteAsync(async(db,token)=>
        {
            if(actor?.ClientId is not { } client)return new("unauthenticated");
            try{_=await TrialBalanceStorageStore.Authorize(db,actor,client,false,token);}
            catch(TrialBalanceStorageRejected error){return new(error.Status,Code:error.Code);}
            await using var command=db.CreateCommand("""
                SELECT job_id,request_json::text,request_hash,input_fingerprint,state,attempts,lease_id,lease_until,
                       expires_at,code,file_name,tenant_id,user_id,client_id,context_version,session_hash,artifact_size,artifact_sha256
                FROM public.financial_report_jobs WHERE job_id=$1 AND tenant_id=$2 AND user_id=$3 AND client_id=$4
                """);
            command.Parameters.AddWithValue(id);command.Parameters.AddWithValue(actor.TenantId!.Value);
            command.Parameters.AddWithValue(actor.UserId);command.Parameters.AddWithValue(client);
            await using var reader=await command.ExecuteReaderAsync(token);
            if(!await reader.ReadAsync(token))return new("not_found",Code:"job_not_found");
            var job=ReadJob(reader);
            if(job.ContextVersion!=actor.ContextVersion||job.SessionHash!=Hash(actor.Token))return new("not_found",Code:"job_not_found");
            return new FinancialReportJobResult("success",Output(job,reader.IsDBNull(16)?null:reader.GetInt32(16)));
        },ct);

    public Task<FinancialReportJobResult> CancelAsync(VerifiedSession? actor,Guid id,CancellationToken ct)=>
        transactions.ExecuteAsync(async(db,token)=>
        {
            if(actor?.ClientId is not { } client)return new("unauthenticated");
            try{_=await TrialBalanceStorageStore.Authorize(db,actor,client,false,token);}
            catch(TrialBalanceStorageRejected error){return new(error.Status,Code:error.Code);}
            await using(var update=db.CreateCommand("""
                UPDATE public.financial_report_jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
                    cancel_requested=CASE WHEN state='running' THEN TRUE ELSE cancel_requested END,updated_at=now()
                WHERE job_id=$1 AND tenant_id=$2 AND user_id=$3 AND client_id=$4 AND context_version=$5
                  AND session_hash=$6 AND state IN ('queued','running')
                """))
            {
                update.Parameters.AddWithValue(id);update.Parameters.AddWithValue(actor.TenantId!.Value);
                update.Parameters.AddWithValue(actor.UserId);update.Parameters.AddWithValue(client);
                update.Parameters.AddWithValue(actor.ContextVersion);update.Parameters.AddWithValue(Hash(actor.Token));
                await update.ExecuteNonQueryAsync(token);
            }
            await using(var owned=db.CreateCommand("""
                SELECT 1 FROM public.financial_report_jobs WHERE job_id=$1 AND tenant_id=$2 AND user_id=$3
                  AND client_id=$4 AND context_version=$5 AND session_hash=$6
                """))
            {
                owned.Parameters.AddWithValue(id);owned.Parameters.AddWithValue(actor.TenantId!.Value);
                owned.Parameters.AddWithValue(actor.UserId);owned.Parameters.AddWithValue(client);
                owned.Parameters.AddWithValue(actor.ContextVersion);owned.Parameters.AddWithValue(Hash(actor.Token));
                if(await owned.ExecuteScalarAsync(token) is null)return new("not_found",Code:"job_not_found");
            }
            return await ReadOutputAsync(db,id,token);
        },ct);

    public Task<FinancialReportJobDownload?> DownloadAsync(VerifiedSession? actor,Guid id,CancellationToken ct)=>
        transactions.ExecuteAsync(async(db,token)=>
        {
            if(actor?.ClientId is not { } client)return null;
            try{_=await TrialBalanceStorageStore.Authorize(db,actor,client,false,token);}
            catch(TrialBalanceStorageRejected){return null;}
            await using var command=db.CreateCommand("""
                SELECT artifact_bytes,file_name,artifact_content_type,artifact_sha256,session_hash,context_version
                FROM public.financial_report_jobs WHERE job_id=$1 AND tenant_id=$2 AND user_id=$3 AND client_id=$4
                  AND state='succeeded' AND expires_at>now()
                """);
            command.Parameters.AddWithValue(id);command.Parameters.AddWithValue(actor.TenantId!.Value);
            command.Parameters.AddWithValue(actor.UserId);command.Parameters.AddWithValue(client);
            await using var reader=await command.ExecuteReaderAsync(token);
            if(!await reader.ReadAsync(token)||reader.IsDBNull(0)||reader.IsDBNull(1)||reader.IsDBNull(2)
                ||reader.IsDBNull(3)||reader.GetString(4).Trim()!=Hash(actor.Token)||reader.GetInt64(5)!=actor.ContextVersion)return null;
            byte[] bytes=(byte[])reader[0];string hash=Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
            if(hash!=reader.GetString(3).Trim())return null;
            return new FinancialReportJobDownload(bytes,reader.GetString(1),reader.GetString(2));
        },ct);

    public Task<FinancialReportJobLease?> ClaimAsync(CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        await using(var expired=db.CreateCommand("""
            UPDATE public.financial_report_jobs SET state='failed',code='job_attempts_exhausted',updated_at=now()
            WHERE state='running' AND lease_until<now() AND attempts>=maximum_attempts
            """))await expired.ExecuteNonQueryAsync(token);
        Guid lease=Guid.NewGuid();
        await using var claim=db.CreateCommand("""
            WITH candidate AS (
                SELECT job_id FROM public.financial_report_jobs
                WHERE (state='queued' AND available_at<=now()) OR (state='running' AND lease_until<now() AND attempts<maximum_attempts)
                ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE public.financial_report_jobs j SET state='running',attempts=j.attempts+1,lease_id=$1,
                lease_until=now()+make_interval(secs=>$2),updated_at=now()
            FROM candidate WHERE j.job_id=candidate.job_id
            RETURNING j.job_id,j.request_json::text,j.request_hash,j.input_fingerprint,j.state,j.attempts,
                j.lease_id,j.lease_until,j.expires_at,j.code,j.file_name,j.tenant_id,j.user_id,j.client_id,
                j.context_version,j.session_hash
            """);
        claim.Parameters.AddWithValue(lease);claim.Parameters.AddWithValue(options.Value.LeaseSeconds);
        await using var reader=await claim.ExecuteReaderAsync(token);if(!await reader.ReadAsync(token))return null;
        var job=ReadJob(reader);await reader.DisposeAsync();
        VerifiedSession? actor=await ActorAsync(db,job,token);
        if(actor is null)
        {
            await using var fail=db.CreateCommand("UPDATE public.financial_report_jobs SET state='failed',code='job_session_revoked',lease_id=NULL,lease_until=NULL WHERE job_id=$1 AND lease_id=$2");
            fail.Parameters.AddWithValue(job.Id);fail.Parameters.AddWithValue(lease);await fail.ExecuteNonQueryAsync(token);return null;
        }
        return new FinancialReportJobLease(job,actor);
    },ct);

    public Task CompleteAsync(FinancialReportJobLease lease,byte[] artifact,string fileName,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        if(artifact.Length is <1 or >16777216)throw new InvalidDataException("Financial report artifact exceeds its configured bound.");
        string hash=Convert.ToHexString(SHA256.HashData(artifact)).ToLowerInvariant();
        await using var command=db.CreateCommand("""
            UPDATE public.financial_report_jobs SET state=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'succeeded' END,
                file_name=CASE WHEN cancel_requested THEN NULL ELSE $3 END,
                artifact_content_type=CASE WHEN cancel_requested THEN NULL ELSE 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' END,
                artifact_bytes=CASE WHEN cancel_requested THEN NULL ELSE $4 END,
                artifact_size=CASE WHEN cancel_requested THEN NULL ELSE $5 END,
                artifact_sha256=CASE WHEN cancel_requested THEN NULL ELSE $6 END,
                lease_id=NULL,lease_until=NULL,updated_at=now(),code=NULL
            WHERE job_id=$1 AND lease_id=$2 AND state='running'
            """);
        command.Parameters.AddWithValue(lease.Job.Id);command.Parameters.AddWithValue(lease.Job.LeaseId!.Value);
        command.Parameters.AddWithValue(fileName);command.Parameters.AddWithValue(artifact);command.Parameters.AddWithValue(artifact.Length);command.Parameters.AddWithValue(hash);
        await command.ExecuteNonQueryAsync(token);return true;
    },ct);

    public Task FailAsync(FinancialReportJobLease lease,string code,CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        await using var command=db.CreateCommand("""
            UPDATE public.financial_report_jobs SET state=CASE WHEN attempts<maximum_attempts THEN 'queued' ELSE 'failed' END,
                available_at=now()+interval '5 seconds',code=$3,lease_id=NULL,lease_until=NULL,updated_at=now()
            WHERE job_id=$1 AND lease_id=$2 AND state='running'
            """);command.Parameters.AddWithValue(lease.Job.Id);command.Parameters.AddWithValue(lease.Job.LeaseId!.Value);command.Parameters.AddWithValue(code);
        await command.ExecuteNonQueryAsync(token);return true;
    },ct);

    public Task CleanupAsync(CancellationToken ct)=>transactions.ExecuteAsync(async(db,token)=>
    {
        await using var command=db.CreateCommand("""
            UPDATE public.financial_report_jobs SET state='expired',artifact_bytes=NULL,artifact_size=NULL,
                artifact_sha256=NULL,file_name=NULL,artifact_content_type=NULL,updated_at=now()
            WHERE expires_at<=now() AND state IN ('succeeded','failed','cancelled')
            """);await command.ExecuteNonQueryAsync(token);return true;
    },ct);

    private static async Task<VerifiedSession?> ActorAsync(PostgresTransactionContext db,FinancialReportJobData job,CancellationToken ct)
    {
        await using var command=db.CreateCommand("""
            SELECT s.token,u.username,u.role,s.expiry,s.selected_client_id,c.client_name,c.client_code,s.selected_tenant_id,s.context_version
            FROM public.sessions s JOIN public.users u ON u.id=s.user_id
            JOIN public.clients c ON c.id=s.selected_client_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.tenant_id=s.selected_tenant_id AND m.is_active IS TRUE AND m.tenant_role='admin'
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=m.tenant_id
            WHERE s.user_id=$1 AND s.selected_tenant_id=$2 AND s.selected_client_id=$3
              AND s.context_version=$4 AND s.expiry>now() AND u.is_active IS TRUE
            ORDER BY s.expiry DESC LIMIT 1
            """);command.Parameters.AddWithValue(job.UserId);command.Parameters.AddWithValue(job.TenantId);
        command.Parameters.AddWithValue(job.ClientId);command.Parameters.AddWithValue(job.ContextVersion);
        await using var reader=await command.ExecuteReaderAsync(ct);if(!await reader.ReadAsync(ct))return null;
        string token=reader.GetString(0);if(Hash(token)!=job.SessionHash)return null;
        return new VerifiedSession(token,job.UserId,reader.GetString(1),reader.IsDBNull(2)?null:reader.GetString(2),
            reader.GetDateTime(3),reader.GetInt32(4),reader.GetString(5),reader.GetString(6),true,reader.GetInt32(7),reader.GetInt64(8));
    }

    private static async Task<FinancialReportJobResult> ReadOutputAsync(PostgresTransactionContext db,Guid id,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT job_id,request_json::text,state,attempts,expires_at,code,file_name,artifact_size FROM public.financial_report_jobs WHERE job_id=$1");command.Parameters.AddWithValue(id);
        await using var reader=await command.ExecuteReaderAsync(ct);if(!await reader.ReadAsync(ct))return new("not_found",Code:"job_not_found");
        var request=JsonSerializer.Deserialize<FinancialReportJobRequest>(reader.GetString(1))!;
        return new("success",new(reader.GetGuid(0),request.ClientId,request.Report,reader.GetString(2),reader.GetInt32(3),new DateTimeOffset(reader.GetDateTime(4)),
            reader.IsDBNull(5)?null:reader.GetString(5),reader.IsDBNull(6)?null:reader.GetString(6),reader.IsDBNull(7)?null:reader.GetInt32(7)));
    }

    private static FinancialReportJobData ReadJob(NpgsqlDataReader r)
    {
        var request=JsonSerializer.Deserialize<FinancialReportJobRequest>(r.GetString(1))!;
        return new(r.GetGuid(0),r.GetInt32(11),r.GetInt32(12),r.GetInt32(13),r.GetInt64(14),
            r.GetString(15).Trim(),request,r.GetString(3).Trim(),r.GetString(4),r.GetInt32(5),
            r.IsDBNull(6)?null:r.GetGuid(6),r.IsDBNull(7)?null:new DateTimeOffset(r.GetDateTime(7)),new DateTimeOffset(r.GetDateTime(8)),
            r.IsDBNull(9)?null:r.GetString(9),r.IsDBNull(10)?null:r.GetString(10),null,null);
    }
    private static FinancialReportJobOutput Output(FinancialReportJobData job,int? bytes)=>new(job.Id,job.ClientId,job.Request.Report,job.State,job.Attempts,job.ExpiresAt,job.Code,job.FileName,bytes);
    private static string Hash(string value)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
}
