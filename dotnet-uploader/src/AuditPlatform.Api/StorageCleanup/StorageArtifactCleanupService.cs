using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Uploads;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.StorageCleanup;

public sealed class StorageArtifactCleanupService(PostgresTransactions transactions,UploadStagingService staging,
    UploadJobSourceStore objects,IOptions<StorageCleanupOptions> options)
{
    private sealed record Work(Guid Operation,string Kind,Guid Key,string Location,long SourceVersion,long LeaseVersion,Guid Worker);
    // Leases survive restart. A worker can retry only generated artifact removal,
    // never the accounting DELETE transaction or another operation's keys.
    public async Task<bool> ProcessOnceAsync(CancellationToken ct=default)
    {
        if(!options.Value.Enabled||!options.Value.PurgeEnabled||!options.Value.ArtifactWorkerEnabled) return false;
        var work=await transactions.ExecuteAsync(async(db,token)=>
        {
            await using(var exhausted=db.CreateCommand("UPDATE public.platform_storage_cleanup_artifacts SET state='review',worker_id=NULL,lease_until=NULL,code='artifact_retry_limit' WHERE state='working' AND lease_until<=now() AND attempts>=3"))
                await exhausted.ExecuteNonQueryAsync(token);
            Guid operation,key;string kind,location;long sourceVersion,version;
            await using(var claim=db.CreateCommand("""
                SELECT a.operation_id,a.kind,a.source_key,a.location_hash,a.source_version,a.lease_version
                FROM public.platform_storage_cleanup_artifacts a JOIN public.platform_storage_cleanup_runs r USING(operation_id)
                WHERE r.state='completed' AND a.attempts<3 AND (a.state='pending' OR a.state='working' AND a.lease_until<=now())
                ORDER BY r.completed_at,a.operation_id,a.kind,a.source_key LIMIT 1 FOR UPDATE OF a SKIP LOCKED
                """))
            await using(var reader=await claim.ExecuteReaderAsync(token))
            {
                if(!await reader.ReadAsync(token)) return null;
                operation=reader.GetGuid(0);kind=reader.GetString(1);key=reader.GetGuid(2);location=reader.GetString(3);sourceVersion=reader.GetInt64(4);version=reader.GetInt64(5)+1;
            }
            var worker=Guid.NewGuid();
            await using var update=db.CreateCommand("UPDATE public.platform_storage_cleanup_artifacts SET state='working',attempts=attempts+1,lease_version=$4,worker_id=$5,lease_until=now()+interval '150 seconds',code=NULL WHERE operation_id=$1 AND kind=$2 AND source_key=$3");
            update.Parameters.AddWithValue(operation);update.Parameters.AddWithValue(kind);update.Parameters.AddWithValue(key);update.Parameters.AddWithValue(version);update.Parameters.AddWithValue(worker);
            await update.ExecuteNonQueryAsync(token);return new Work(operation,kind,key,location,sourceVersion,version,worker);
        },ct);
        if(work is null) return false;
        string? failure=null;
        try
        {
            using var transfer=CancellationTokenSource.CreateLinkedTokenSource(ct);transfer.CancelAfter(TimeSpan.FromSeconds(120));
            if(work.Kind=="object") await objects.DeleteAsync(new(work.Key,0,work.Location),transfer.Token);
            else await staging.CleanupPurgedAsync(work.Key,work.Location,work.Kind=="restore",work.SourceVersion,transfer.Token);
        }
        catch(Exception error) when(error is IOException or HttpRequestException or UploadJobRejected or InvalidOperationException or OperationCanceledException or UnauthorizedAccessException)
        {
            if(ct.IsCancellationRequested) throw;
            failure="artifact_storage_or_ownership_review";
        }
        await transactions.ExecuteAsync(async(db,token)=>
        {
            await using var update=db.CreateCommand("""
                UPDATE public.platform_storage_cleanup_artifacts SET state=CASE WHEN $6::text IS NULL THEN 'done' WHEN attempts>=3 THEN 'review' ELSE 'pending' END,
                    worker_id=NULL,lease_until=NULL,code=$6 WHERE operation_id=$1 AND kind=$2 AND source_key=$3 AND lease_version=$4 AND worker_id=$5 AND state='working'
                """);
            update.Parameters.AddWithValue(work.Operation);update.Parameters.AddWithValue(work.Kind);update.Parameters.AddWithValue(work.Key);
            update.Parameters.AddWithValue(work.LeaseVersion);update.Parameters.AddWithValue(work.Worker);
            update.Parameters.AddWithValue(NpgsqlTypes.NpgsqlDbType.Text,(object?)failure??DBNull.Value);return await update.ExecuteNonQueryAsync(token);
        },ct);
        return true;
    }
}
