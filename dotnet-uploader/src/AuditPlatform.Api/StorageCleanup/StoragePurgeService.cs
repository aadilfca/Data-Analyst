using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Uploads;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.StorageCleanup;

public sealed class StoragePurgeService(PostgresTransactions transactions,PostgresReadSnapshot snapshots,
    StorageCleanupService evidence,UploadValidationPlanStore plans,IOptions<StorageCleanupOptions> options,IConfiguration configuration)
{
    private readonly SemaphoreSlim applies=new(1);
    public const string PurgeConfirmation="PERMANENTLY DELETE SELECTED DATA";
    public const string PlanConfirmation="DELETE APPROVED VALIDATION PLANS";

    public async Task<CleanupResult> ApplyAsync(VerifiedSession? session,int client,int template,string kind,CleanupRequest? request,CancellationToken ct=default)
    {
        if(!options.Value.Enabled||kind=="purge"&&!options.Value.PurgeEnabled) return new("unavailable",Code:"cleanup_disabled");
        if(session is null) return new("unauthenticated",Code:"not_authenticated");
        if(client<1||template<1||kind is not("plans" or "purge")||request is null||request.OperationId==Guid.Empty
            ||request.PreviewToken is not {Length:>0 and <=4096}||request.BackupToken?.Length>4096
            ||request.Confirmation!=(kind=="purge"?PurgeConfirmation:PlanConfirmation)) return new("invalid",Code:"cleanup_scope_or_confirmation_invalid");
        if(!await applies.WaitAsync(0,ct)) return new("busy",Code:"cleanup_busy");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        string requestHash=StorageCleanupService.Hash(JsonSerializer.Serialize(new {client,template,kind,request}));
        try
        {
            var value=await transactions.ExecuteAsync((db,token)=>plans.AuthorizeScopeAsync(db,session,client,template,async(owned,cancel)=>
            {
                await evidence.Authorize(owned,session,client,template,cancel);
                // Match configuration/import ordering, then the job reservation/claim lock.
                await using(var jobs=owned.CreateCommand("SELECT pg_advisory_xact_lock(746128)")) await jobs.ExecuteNonQueryAsync(cancel);
                await using(var operation=owned.CreateCommand("SELECT pg_advisory_xact_lock(hashtext($1))"))
                {operation.Parameters.AddWithValue("storage-cleanup:"+request.OperationId);await operation.ExecuteNonQueryAsync(cancel);}
                var prior=await ReadWithin(owned,session,client,template,request.OperationId,cancel,requestHash);
                if(prior is not null) return prior;
                var proof=evidence.Verify(session,request.PreviewToken,"preview",client,template,kind);
                var backup=kind=="purge"?evidence.Verify(session,request.BackupToken??"","backup",client,template,kind):null;
                var before=await evidence.CaptureWithin(owned,session,client,template,kind,cancel);
                // NOWAIT prevents lock-order waits/deadlocks with unported writers.
                foreach(string table in before.Tables)
                {
                    await using var locked=owned.CreateCommand("LOCK TABLE public."+StorageCleanupService.Quote(table)+" IN SHARE ROW EXCLUSIVE MODE NOWAIT");
                    await locked.ExecuteNonQueryAsync(cancel);
                }
                var fresh=await evidence.CaptureWithin(owned,session,client,template,kind,cancel);
                if(fresh.Fingerprint!=proof.Fingerprint||backup is not null&&backup.Fingerprint!=fresh.Fingerprint)
                    throw new CleanupRejected("conflict","cleanup_preview_or_backup_stale");
                if(fresh.Blockers.Count>0) throw new CleanupRejected("conflict","cleanup_protected_or_review_required");
                await using(var capacity=evidence.Command(owned,"SELECT COUNT(*) FROM public.platform_storage_cleanup_runs WHERE tenant_id=@tenant AND client_id=@client AND template_id=@template",client,template,session.TenantId!.Value))
                    if((long)(await capacity.ExecuteScalarAsync(cancel))!>=options.Value.MaximumOperationsPerScope) throw new CleanupRejected("too_large","cleanup_operation_capacity");
                await using(var capacity=owned.CreateCommand("SELECT pg_database_size(current_database())"))
                    if((long)(await capacity.ExecuteScalarAsync(cancel))!+2097152>=Math.Min(500000000,configuration.GetValue<long?>("Platform:DatabaseCapacityLimitBytes")??500000000))
                        throw new CleanupRejected("too_large","cleanup_database_capacity");
                await using(var insert=owned.CreateCommand("""
                    INSERT INTO public.platform_storage_cleanup_runs(operation_id,tenant_id,user_id,client_id,template_id,kind,request_hash,fingerprint,state)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,'applying')
                    """))
                {
                    insert.Parameters.AddWithValue(request.OperationId);insert.Parameters.AddWithValue(session.TenantId.Value);insert.Parameters.AddWithValue(session.UserId);
                    insert.Parameters.AddWithValue(client);insert.Parameters.AddWithValue(template);insert.Parameters.AddWithValue(kind);insert.Parameters.AddWithValue(requestHash);
                    insert.Parameters.AddWithValue(fresh.Fingerprint);await insert.ExecuteNonQueryAsync(cancel);
                }
                await using(var permit=owned.CreateCommand("SELECT set_config('auditplatform.purge_operation',$1,true)"))
                {permit.Parameters.AddWithValue(request.OperationId.ToString());await permit.ExecuteNonQueryAsync(cancel);}
                var deleted=new Dictionary<string,long>();
                var order=kind=="plans"?new[]{"upload_validation_plans"}:new[]{
                    "cashflow_prepared_lines","cashflow_prepared_rollups","cash_bank_summary_read_models","cashflow_ledger_rollup_read_models",
                    "cash_bank_transaction_classifications","cashflow_voucher_occurrences","data_audit_log","client_data_history",
                    "upload_validation_plans","upload_lifecycle_runs","verifications","platform_upload_jobs","platform_upload_import_receipts",
                    "platform_upload_imports","platform_upload_validation_plans","platform_upload_staging","audit_log","client_data","uploaded_files"};
                if(kind=="purge"&&fresh.Tables.Contains("account_control_cashflow_settings"))
                {
                    await using var detach=evidence.Command(owned,"UPDATE public.account_control_cashflow_settings t SET source_upload_id=NULL WHERE "+StorageCleanupService.Predicate("account_control_cashflow_settings",kind),client,template,session.TenantId.Value);
                    if(await detach.ExecuteNonQueryAsync(cancel)!=fresh.Counts["account_control_cashflow_settings"]) throw new CleanupRejected("conflict","cleanup_settings_changed");
                }
                foreach(string table in order.Where(fresh.Tables.Contains))
                {
                    await using var command=evidence.Command(owned,"DELETE FROM public."+StorageCleanupService.Quote(table)+" t WHERE "+StorageCleanupService.Predicate(table,kind),client,template,session.TenantId.Value,fresh.AuditIds);
                    long count=await command.ExecuteNonQueryAsync(cancel);
                    if(count!=fresh.Counts[table]) throw new CleanupRejected("conflict","cleanup_affected_count_changed");
                    deleted[table]=count;
                }
                foreach(var artifact in fresh.Artifacts)
                {
                    await using var insert=owned.CreateCommand("INSERT INTO public.platform_storage_cleanup_artifacts(operation_id,kind,source_key,location_hash,source_version) VALUES($1,$2,$3,$4,$5)");
                    insert.Parameters.AddWithValue(request.OperationId);insert.Parameters.AddWithValue(artifact.Kind);insert.Parameters.AddWithValue(artifact.Key);
                    insert.Parameters.AddWithValue(artifact.LocationHash);insert.Parameters.AddWithValue(artifact.Version);await insert.ExecuteNonQueryAsync(cancel);
                }
                var after=await evidence.CaptureWithin(owned,session,client,template,kind,cancel);
                if(order.Any(t=>after.Counts.GetValueOrDefault(t)!=0)) throw new CleanupRejected("conflict","cleanup_postcheck_failed");
                await using(var complete=owned.CreateCommand("UPDATE public.platform_storage_cleanup_runs SET state='completed',completed_at=now(),deleted_counts=$2::jsonb WHERE operation_id=$1 AND state='applying'"))
                {complete.Parameters.AddWithValue(request.OperationId);complete.Parameters.AddWithValue(JsonSerializer.Serialize(deleted));if(await complete.ExecuteNonQueryAsync(cancel)!=1) throw new CleanupRejected("conflict","cleanup_completion_changed");}
                return (await ReadWithin(owned,session,client,template,request.OperationId,cancel,requestHash))!;
            },token),deadline.Token);
            return new("success",value);
        }
        catch(PostgresCommitOutcomeUnknownException)
        {
            // Reconcile only. A missing COMMIT response never authorizes replay.
            using var recovery=new CancellationTokenSource(TimeSpan.FromSeconds(5));
            try
            {
                var found=await StatusAsync(session,client,template,request.OperationId,recovery.Token);
                if(found.Status=="success") return found;
            }
            catch(Exception error) when(error is NpgsqlException or OperationCanceledException) { }
            return new("conflict",Code:"cleanup_outcome_unknown_check_same_operation");
        }
        catch(Exception error) when(StorageCleanupService.Map(error,ct,deadline) is not null) {return StorageCleanupService.Map(error,ct,deadline)!;}
        finally {applies.Release();}
    }

    public async Task<CleanupResult> StatusAsync(VerifiedSession? session,int client,int template,Guid id,CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return new("unavailable",Code:"cleanup_disabled");
        if(session is null) return new("unauthenticated",Code:"not_authenticated");
        if(id==Guid.Empty||client<1||template<1) return new("invalid",Code:"template_scope_required");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var value=await snapshots.ExecuteAsync(async(db,token)=>
            {await evidence.Authorize(db,session,client,template,token);return await ReadWithin(db,session,client,template,id,token);},deadline.Token);
            await snapshots.ExecuteAsync(async(db,token)=>{await evidence.Authorize(db,session,client,template,token);return true;},deadline.Token);
            return value is null?new("not_found",Code:"cleanup_operation_not_found"):new("success",value);
        }
        catch(Exception error) when(StorageCleanupService.Map(error,ct,deadline) is not null) {return StorageCleanupService.Map(error,ct,deadline)!;}
    }
    public async Task<CleanupResult> RecentAsync(VerifiedSession? session,int client,int template,CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return new("unavailable",Code:"cleanup_disabled");
        if(session is null) return new("unauthenticated",Code:"not_authenticated");
        if(client<1||template<1) return new("invalid",Code:"template_scope_required");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var values=await snapshots.ExecuteAsync(async(db,token)=>
            {
                await evidence.Authorize(db,session,client,template,token);
                var ids=new List<Guid>();
                await using(var command=db.CreateCommand("SELECT operation_id FROM public.platform_storage_cleanup_runs WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3 AND state='completed' ORDER BY completed_at DESC,operation_id LIMIT 20"))
                {
                    command.Parameters.AddWithValue(session.TenantId!.Value);command.Parameters.AddWithValue(client);command.Parameters.AddWithValue(template);
                    await using var reader=await command.ExecuteReaderAsync(token);while(await reader.ReadAsync(token)) ids.Add(reader.GetGuid(0));
                }
                var rows=new List<CleanupOperation>();foreach(var id in ids) rows.Add((await ReadWithin(db,session,client,template,id,token))!);return rows;
            },deadline.Token);
            await snapshots.ExecuteAsync(async(db,token)=>{await evidence.Authorize(db,session,client,template,token);return true;},deadline.Token);
            if(JsonSerializer.SerializeToUtf8Bytes(values,CleanupJson.Options).Length>options.Value.MaximumResponseBytes) return new("too_large",Code:"cleanup_response_capacity");
            return new("success",values);
        }
        catch(Exception error) when(StorageCleanupService.Map(error,ct,deadline) is not null) {return StorageCleanupService.Map(error,ct,deadline)!;}
    }
    internal static async Task<CleanupOperation?> ReadWithin(PostgresTransactionContext db,VerifiedSession session,int client,int template,Guid id,CancellationToken ct,string? requestHash=null)
    {
        await using var command=db.CreateCommand("SELECT tenant_id,user_id,client_id,template_id,kind,request_hash,state,completed_at,deleted_counts::text FROM public.platform_storage_cleanup_runs WHERE operation_id=$1");
        command.Parameters.AddWithValue(id);
        string kind;DateTimeOffset completed;Dictionary<string,long> counts;
        await using(var reader=await command.ExecuteReaderAsync(ct))
        {
            if(!await reader.ReadAsync(ct)) return null;
            if(reader.GetInt32(0)!=session.TenantId||requestHash is not null&&reader.GetInt32(1)!=session.UserId||reader.GetInt32(2)!=client||reader.GetInt32(3)!=template
                ||requestHash is not null&&reader.GetString(5)!=requestHash) throw new CleanupRejected("conflict","cleanup_operation_reused");
            if(reader.GetString(6)!="completed") throw new CleanupRejected("review","cleanup_operation_review_required");
            kind=reader.GetString(4);completed=new(reader.GetDateTime(7));counts=JsonSerializer.Deserialize<Dictionary<string,long>>(reader.GetString(8))!;
        }
        await using var artifacts=db.CreateCommand("SELECT count(*) FILTER(WHERE state IN('pending','working')),count(*) FILTER(WHERE state='review') FROM public.platform_storage_cleanup_artifacts WHERE operation_id=$1");
        artifacts.Parameters.AddWithValue(id);await using var rows=await artifacts.ExecuteReaderAsync(ct);await rows.ReadAsync(ct);
        int pending=checked((int)rows.GetInt64(0)),review=checked((int)rows.GetInt64(1));
        return new(id,client,template,kind,completed,counts,review>0?"review_required":pending>0?"pending":"complete",pending,review);
    }
}
