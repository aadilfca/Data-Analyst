using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.DataBank;
using AuditPlatform.Api.Uploads;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.StorageCleanup;

// A preview is SELECT-only. The signed proof contains scope and a digest, never
// source rows, filenames, credentials, arbitrary SQL or artifact paths.
public sealed class StorageCleanupService(PostgresReadSnapshot snapshots, DataBankService dataBank,
    UploadStagingService staging, IOptions<StorageCleanupOptions> options, TimeProvider? clock = null, IOptions<UploadJobOptions>? jobs = null)
{
    private readonly SemaphoreSlim previews = new(2);
    private DateTimeOffset Now => (clock ?? TimeProvider.System).GetUtcNow();
    internal static string Hash(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
    internal static string Quote(string value) => "\"" + value.Replace("\"", "\"\"") + "\"";
    private sealed record Table(string Name, string Key, string Where, bool Optional = false, bool Delete = true);
    private const string Scope = "t.client_id=@client AND t.template_id=@template";
    private const string Plans = Scope + " AND (t.status IN('applied','expired','superseded') OR t.expires_at<=now())";
    private static readonly Table[] SourceTables = [
        new("client_data","id",Scope), new("client_data_history","id",Scope), new("uploaded_files","id",Scope),
        new("upload_validation_plans","plan_id",Scope),
        new("platform_upload_staging","request_id",Scope), new("platform_upload_validation_plans","request_id",Scope),
        new("platform_upload_imports","operation_id",Scope), new("platform_upload_import_receipts","receipt_id",Scope),
        new("platform_upload_jobs","job_id",Scope), new("upload_lifecycle_runs","operation_id",Scope,true),
        new("cash_bank_transaction_classifications","id",Scope,true), new("cashflow_voucher_occurrences","id",Scope,true),
        new("cashflow_prepared_lines","id",Scope,true), new("cashflow_prepared_rollups","id",Scope,true),
        new("cash_bank_summary_read_models","id",Scope,true), new("cashflow_ledger_rollup_read_models","id",Scope,true),
        new("data_audit_log","id","EXISTS(SELECT 1 FROM public.client_data cd WHERE cd.id=t.client_data_id AND cd.client_id=@client AND cd.template_id=@template)"),
        new("verifications","id","EXISTS(SELECT 1 FROM public.uploaded_files u WHERE u.id=t.file_id AND u.client_id=@client AND u.template_id=@template)",true),
        new("account_control_cashflow_settings","client_id","t.client_id=@client AND EXISTS(SELECT 1 FROM public.uploaded_files u WHERE u.id=t.source_upload_id AND u.client_id=@client AND u.template_id=@template)",true,false)
    ];
    private static readonly (string Table, string Column)[] ProtectedTables = [
        ("verification_details","client_data_id"), ("voucher_line_documents","client_data_id"),
        ("voucher_line_physical_bill_items","source_document_row_id"), ("cashflow_prepared_lines","source_client_data_id")
    ];
    internal static string Predicate(string table, string kind) => table switch {
        "upload_validation_plans" when kind == "plans" => Plans,
        "audit_log" => "t.id=ANY(@audit)",
        _ => SourceTables.Single(t => t.Name == table).Where
    };
    internal NpgsqlCommand Command(PostgresTransactionContext db, string sql, int client, int template, int tenant, int[]? audit = null)
    {
        var command = db.CreateCommand(sql);
        foreach (var (name, value) in new[] { ("client",client), ("template",template), ("tenant",tenant),
            ("rowcap",options.Value.MaximumRows+1), ("cellcap",Math.Min(options.Value.MaximumInputBytes,4194304)+1) })
            if (sql.Contains("@"+name, StringComparison.Ordinal)) command.Parameters.AddWithValue(name,value);
        if(sql.Contains("@audit",StringComparison.Ordinal)) command.Parameters.AddWithValue("audit",audit??[]);
        return command;
    }
    internal async Task Authorize(PostgresTransactionContext db, VerifiedSession session, int client, int template, CancellationToken ct)
    {
        await DataBankService.Authorize(db,session,client,ct,admin:true);
        _ = await dataBank.TemplateWithin(db,session.TenantId!.Value,template,ct);
    }
    internal async Task<bool> Exists(PostgresTransactionContext db,string table,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT to_regclass($1) IS NOT NULL");command.Parameters.AddWithValue("public."+table);
        return (bool)(await command.ExecuteScalarAsync(ct))!;
    }
    internal async Task<long> Count(PostgresTransactionContext db,string sql,int client,int template,int tenant,CancellationToken ct)
    {await using var command=Command(db,sql,client,template,tenant);return Convert.ToInt64(await command.ExecuteScalarAsync(ct));}

    public async Task<CleanupResult> PreviewAsync(VerifiedSession? session,int client,int template,string kind,CancellationToken ct=default)
    {
        if(!options.Value.Enabled || kind=="purge"&&!options.Value.PurgeEnabled) return new("unavailable",Code:"cleanup_disabled");
        if(session is null) return new("unauthenticated",Code:"not_authenticated");
        if(client<1||template<1||kind is not("purge" or "plans")) return new("invalid",Code:"template_scope_required");
        if(!await previews.WaitAsync(0,ct)) return new("busy",Code:"cleanup_busy");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var capture=await snapshots.ExecuteAsync((db,token)=>CaptureWithin(db,session,client,template,kind,token),deadline.Token);
            await snapshots.ExecuteAsync(async(db,token)=>{await Authorize(db,session,client,template,token);return true;},deadline.Token);
            var value=new CleanupPreview(client,template,kind,Now,capture.Counts,capture.Protected,capture.Fingerprint,
                Sign(session,capture,"preview"),capture.Blockers.Count==0,capture.Blockers,capture.Artifacts.Count);
            if(JsonSerializer.SerializeToUtf8Bytes(value,CleanupJson.Options).Length>options.Value.MaximumResponseBytes) throw new CleanupRejected("too_large","cleanup_response_capacity");
            return new("success",value);
        }
        catch(Exception error) when(Map(error,ct,deadline) is not null) {return Map(error,ct,deadline)!;}
        finally {previews.Release();}
    }

    internal async Task<CleanupCapture> CaptureWithin(PostgresTransactionContext db,VerifiedSession session,int client,int template,string kind,CancellationToken ct)
    {
        await Authorize(db,session,client,template,ct);
        await using(var settings=db.CreateCommand("SET LOCAL timezone='UTC'; SET LOCAL datestyle='ISO,YMD'")) await settings.ExecuteNonQueryAsync(ct);
        int tenant=session.TenantId!.Value;
        var counts=new Dictionary<string,long>();var protectedCounts=new Dictionary<string,long>();var blockers=new List<string>();
        var artifacts=new List<CleanupArtifact>();var uploads=new List<int>();var auditIds=new List<int>();var present=new List<string>();
        using var digest=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        long totalRows=0,totalBytes=0;
        void Add(string text)
        {
            byte[] bytes=Encoding.UTF8.GetBytes(text);totalBytes+=bytes.Length;
            if(totalBytes>options.Value.MaximumInputBytes) throw new CleanupRejected("too_large","cleanup_evidence_capacity");
            digest.AppendData(BitConverter.GetBytes(bytes.Length));digest.AppendData(bytes);
        }
        async Task Scan(Table table)
        {
            if(!await Exists(db,table.Name,ct))
            {if(!table.Optional) throw new CleanupRejected("review","cleanup_schema_review_required");return;}
            present.Add(table.Name);
            if(await Count(db,"SELECT row_security_active('public."+table.Name+"'::regclass)::integer",client,template,tenant,ct)!=0)
                throw new CleanupRejected("review","cleanup_row_security_review_required");
            Add(table.Name);
            await using var command=Command(db,$"SELECT t.{Quote(table.Key)}::text,left(to_jsonb(t)::text,@cellcap) FROM public.{Quote(table.Name)} t WHERE {table.Where} ORDER BY t.{Quote(table.Key)} LIMIT @rowcap",client,template,tenant);
            await using var reader=await command.ExecuteReaderAsync(ct);long count=0;
            while(await reader.ReadAsync(ct))
            {
                if(++totalRows>options.Value.MaximumRows) throw new CleanupRejected("too_large","cleanup_row_capacity");
                string raw=reader.GetString(1);
                if(raw.Length>=Math.Min(options.Value.MaximumInputBytes,4194304)+1) throw new CleanupRejected("too_large","cleanup_cell_capacity");
                Add(reader.GetString(0));Add(raw);count++;
                using var json=JsonDocument.Parse(raw);var row=json.RootElement;
                if(table.Name.StartsWith("platform_upload_",StringComparison.Ordinal)&&row.GetProperty("tenant_id").GetInt32()!=tenant)
                    blockers.Add("cross_workspace_upload_metadata");
                if(table.Name.StartsWith("platform_upload_",StringComparison.Ordinal)
                    &&(row.TryGetProperty("file_id",out var file)||row.TryGetProperty("upload_id",out file))&&!uploads.Contains(file.GetInt32())&&kind=="purge")
                    blockers.Add("native_file_scope_review_required");
                if(table.Name=="uploaded_files")
                {uploads.Add(row.GetProperty("id").GetInt32());if(row.TryGetProperty("file_path",out var path)&&path.ValueKind==JsonValueKind.String&&path.GetString()!="") blockers.Add("legacy_source_location_review_required");}
                if(table.Name=="client_data" && ((row.TryGetProperty("is_deleted",out var deleted)&&deleted.ValueKind==JsonValueKind.True)
                    ||row.TryGetProperty("change_detection_status",out var state)&&state.GetString()=="deleted")) blockers.Add("retired_rows_outside_backup_contract");
                if(table.Name=="data_audit_log" && new[]{"old_row_data","new_row_data"}.Any(k=>row.TryGetProperty(k,out var value)&&value.ValueKind is not JsonValueKind.Null))
                    blockers.Add("legacy_audit_payload_backup_review_required");
                if(table.Name=="data_audit_log"&&row.TryGetProperty("client_id",out var auditClient)&&auditClient.ValueKind!=JsonValueKind.Null&&auditClient.GetInt32()!=client)
                    blockers.Add("cross_scope_audit_metadata");
                if(table.Name=="platform_upload_staging" && kind=="purge")
                {
                    if(row.GetProperty("state").GetString()=="receiving"&&row.GetProperty("expires_at").GetDateTimeOffset()>Now) blockers.Add("upload_receiving");
                    artifacts.Add(new("local",row.GetProperty("storage_key").GetGuid(),staging.PurgeStorageIdentity()));
                }
                if(table.Name=="platform_upload_jobs")
                {
                    if(row.GetProperty("state").GetString() is not("succeeded" or "failed" or "cancelled" or "expired" or "review_required")) blockers.Add("active_upload_job");
                    if(kind=="purge")
                    {
                        // Match P28's transfer/lease grace: a cancelled worker's
                        // earlier external PUT must settle before its key is removed.
                        int grace=2*(jobs?.Value.TransferTimeoutSeconds??120)+(jobs?.Value.LeaseSeconds??120);
                        if(!row.GetProperty("source_deleted").GetBoolean()&&row.GetProperty("updated_at").GetDateTimeOffset()>Now.AddSeconds(-grace)) blockers.Add("upload_transfer_grace");
                        var id=row.GetProperty("job_id").GetGuid();long version=row.GetProperty("lease_version").GetInt64();
                        if(!row.GetProperty("source_deleted").GetBoolean()) artifacts.Add(new("object",id,row.GetProperty("storage_hash").GetString()!));
                        if(version>0) artifacts.Add(new("restore",id,staging.PurgeStorageIdentity(),version));
                    }
                }
            }
            counts[table.Name]=count;
        }
        var tables=kind=="plans" ? new[]{new Table("upload_validation_plans","plan_id",Plans),new Table("platform_upload_jobs","job_id",Scope,Delete:false)} : SourceTables;
        foreach(var table in tables) await Scan(table);
        // Configuration participates in the proof but is never deleted.
        await Scan(new("templates","id","t.id=@template",Delete:false));
        await Scan(new("platform_tenant_templates","template_id","t.template_id=@template AND t.tenant_id=@tenant",Delete:false));
        await Scan(new("template_column_mappings","id","t.template_id=@template",Delete:false));
        foreach(string name in new[]{"templates","platform_tenant_templates","template_column_mappings"}) counts.Remove(name);
        if(kind=="purge")
        {
            foreach(var (table,column) in ProtectedTables)
            {
                if(!await Exists(db,table,ct)) {if(table=="cashflow_prepared_lines") continue;throw new CleanupRejected("review","cleanup_protection_schema_required");}
                if(!present.Contains(table)) present.Add(table);
                long value=await Count(db,$"SELECT COUNT(*) FROM public.{Quote(table)} p JOIN public.client_data cd ON cd.id=p.{Quote(column)} WHERE cd.client_id=@client AND cd.template_id=@template",client,template,tenant,ct);
                protectedCounts[table]=value;Add(table+":"+value);
                if(value>0) blockers.Add("protected_"+table);
            }
            long invalid=await Count(db,"""
                SELECT COUNT(*) FROM public.client_data cd WHERE cd.client_id=@client AND cd.template_id=@template AND cd.upload_id IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM public.uploaded_files u WHERE u.id=cd.upload_id AND u.client_id=@client AND u.template_id=@template)
                """,client,template,tenant,ct);
            if(invalid>0) blockers.Add("invalid_upload_links");
            if(await Count(db,"""
                SELECT COUNT(*) FROM public.uploaded_files u WHERE u.client_id=@client AND u.template_id=@template AND NOT EXISTS(
                    SELECT 1 FROM public.platform_upload_staging s WHERE s.upload_id=u.id AND s.client_id=@client AND s.template_id=@template AND s.tenant_id=@tenant)
                """,client,template,tenant,ct)>0) blockers.Add("unowned_upload_metadata");
            if(!await Exists(db,"audit_log",ct)) throw new CleanupRejected("review","cleanup_audit_schema_required");
            present.Add("audit_log");
            await using(var old=Command(db,"SELECT id,left(details,@cellcap) FROM public.audit_log WHERE action IN('Permanent data delete','Replacement data purge') AND (client_id=@client OR client_id IS NULL) ORDER BY id LIMIT @rowcap",client,template,tenant))
            await using(var reader=await old.ExecuteReaderAsync(ct))
            {
                int inspected=0;
                while(await reader.ReadAsync(ct))
                {
                    if(++inspected>options.Value.MaximumRows) throw new CleanupRejected("too_large","cleanup_audit_capacity");
                    string text=reader.IsDBNull(1)?"":reader.GetString(1);
                    if(text.Length>=Math.Min(options.Value.MaximumInputBytes,4194304)+1) throw new CleanupRejected("too_large","cleanup_audit_capacity");
                    try
                    {
                        using var json=JsonDocument.Parse(text);var value=json.RootElement;
                        if(value.TryGetProperty("client_id",out var c)&&c.TryGetInt32(out int cid)&&cid==client
                            &&value.TryGetProperty("template_id",out var t)&&t.TryGetInt32(out int tid)&&tid==template)
                        {auditIds.Add(reader.GetInt32(0));Add("audit:"+reader.GetInt32(0));Add(text);}
                    }
                    catch(JsonException) { /* Unattributable shared events are retained. */ }
                }
            }
            counts["audit_log"]=auditIds.Count;
        }
        await CheckReferences(db,client,template,tenant,kind,auditIds.ToArray(),blockers,present,ct);
        if(artifacts.Count>options.Value.MaximumArtifacts) throw new CleanupRejected("too_large","cleanup_artifact_capacity");
        foreach(var artifact in artifacts) Add(JsonSerializer.Serialize(artifact));
        return new(client,template,kind,Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant(),counts,protectedCounts,
            blockers.Distinct().ToList(),artifacts,present.Distinct().Order().ToArray(),uploads.ToArray(),auditIds.ToArray());
    }

    private async Task CheckReferences(PostgresTransactionContext db,int client,int template,int tenant,string kind,int[] audit,List<string> blockers,List<string> tables,CancellationToken ct)
    {
        // Inspect actual foreign keys, including unrecognised/cascading links.
        // An unknown referencing row protects the target; it never authorizes deletion.
        await using var catalog=db.CreateCommand("""
            SELECT n.nspname::text,r.relname::text,t.relname::text,
                ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(num,pos) JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=k.num ORDER BY pos),
                ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY k(num,pos) JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.num ORDER BY pos)
            FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
            JOIN pg_class t ON t.oid=c.confrelid WHERE c.contype='f' AND c.confrelid IN(SELECT to_regclass(name) FROM unnest($1::text[]) name)
            ORDER BY c.oid LIMIT 257
            """);
        catalog.Parameters.AddWithValue(kind=="plans"?new[]{"public.upload_validation_plans"}:
            SourceTables.Where(t=>t.Delete&&tables.Contains(t.Name)).Select(t=>"public."+t.Name).Append("public.audit_log").ToArray());
        var references=new List<(string Schema,string Table,string Target,string[] Keys,string[] Targets)>();
        await using(var reader=await catalog.ExecuteReaderAsync(ct))
            while(await reader.ReadAsync(ct))
            {if(references.Count==256) throw new CleanupRejected("review","cleanup_catalog_capacity");references.Add((reader.GetString(0),reader.GetString(1),reader.GetString(2),reader.GetFieldValue<string[]>(3),reader.GetFieldValue<string[]>(4)));}
        foreach(var reference in references)
        {
            if(reference.Schema!="public") throw new CleanupRejected("review","cleanup_external_schema_reference");
            if(!tables.Contains(reference.Table)) tables.Add(reference.Table);
            string join=string.Join(" AND ",reference.Keys.Zip(reference.Targets,(a,b)=>"r."+Quote(a)+"=t."+Quote(b)));
            var known=kind=="plans"?null:SourceTables.FirstOrDefault(t=>t.Name==reference.Table);
            string outside=known is null?"TRUE":"("+known.Where.Replace("t.","r.")+") IS NOT TRUE";
            string target=Predicate(reference.Target,kind);
            await using var countCommand=Command(db,$"SELECT COUNT(*) FROM public.{Quote(reference.Table)} r JOIN public.{Quote(reference.Target)} t ON {join} WHERE {target} AND ({outside})",client,template,tenant,audit);
            long count=Convert.ToInt64(await countCommand.ExecuteScalarAsync(ct));
            if(count>0) blockers.Add("referenced_outside_purge_scope");
        }
        // Plain legacy history links may exist without declared foreign keys.
        if(kind=="purge"&&await Count(db,"""
            SELECT COUNT(*) FROM public.client_data_history h JOIN public.uploaded_files u ON u.id=h.source_upload_id OR u.id=h.replacing_upload_id
            WHERE u.client_id=@client AND u.template_id=@template AND (h.client_id IS DISTINCT FROM @client OR h.template_id IS DISTINCT FROM @template)
            """,client,template,tenant,ct)>0) blockers.Add("cross_scope_history_upload_reference");
        // RLS or application triggers could hide evidence or mutate unreviewed data.
        foreach(string table in tables.Distinct())
        {
            await using var command=db.CreateCommand("SELECT row_security_active($1::regclass),EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=$1::regclass AND NOT tgisinternal AND tgenabled<>'D' AND tgname<>ALL($2))");
            command.Parameters.AddWithValue("public."+Quote(table));
            command.Parameters.AddWithValue(new[]{"guard_upload_staging","guard_upload_plan_receipt","platform_upload_imports_no_update","platform_upload_import_receipts_no_update","platform_upload_job_identity"});
            await using var reader=await command.ExecuteReaderAsync(ct);await reader.ReadAsync(ct);
            if(reader.GetBoolean(0)||reader.GetBoolean(1)&&table is not("templates" or "platform_tenant_templates" or "template_column_mappings")) blockers.Add("catalog_or_trigger_review_required");
        }
    }

    internal string Sign(VerifiedSession session,CleanupCapture capture,string purpose)
    {
        var proof=new CleanupProof(1,purpose,session.TenantId!.Value,session.UserId,session.ContextVersion,capture.Client,capture.Template,capture.Kind,capture.Fingerprint,Now.AddMinutes(options.Value.ProofMinutes));
        string text=Convert.ToBase64String(JsonSerializer.SerializeToUtf8Bytes(proof));
        return text+"."+Convert.ToHexString(HMACSHA256.HashData(Convert.FromBase64String(options.Value.SigningKey),Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    }
    internal CleanupProof Verify(VerifiedSession session,string text,string purpose,int client,int template,string kind)
    {
        try
        {
            if(text.Length>4096) throw new FormatException();var parts=text.Split('.');if(parts.Length!=2) throw new FormatException();
            var actual=Convert.FromHexString(parts[1]);var expected=HMACSHA256.HashData(Convert.FromBase64String(options.Value.SigningKey),Encoding.UTF8.GetBytes(parts[0]));
            if(!CryptographicOperations.FixedTimeEquals(actual,expected)) throw new FormatException();
            var proof=JsonSerializer.Deserialize<CleanupProof>(Convert.FromBase64String(parts[0]))??throw new FormatException();
            if(proof.Version!=1||proof.Purpose!=purpose||proof.Tenant!=session.TenantId||proof.User!=session.UserId||proof.Context!=session.ContextVersion
                ||proof.Client!=client||proof.Template!=template||proof.Kind!=kind||proof.Expires<=Now) throw new FormatException();
            return proof;
        }
        catch(Exception error) when(error is FormatException or JsonException or ArgumentException) {throw new CleanupRejected("conflict","cleanup_proof_expired_or_changed");}
    }
    internal bool EnabledForPurge => options.Value.Enabled&&options.Value.PurgeEnabled;
    internal static CleanupResult? Map(Exception error,CancellationToken caller,CancellationTokenSource deadline) => error switch {
        CleanupRejected rejected=>new(rejected.Status,Code:rejected.Code),
        DataBankRejected rejected=>new(rejected.Status,Code:rejected.Code),
        ValidationPlanRejected=>new("forbidden",Code:"cleanup_access_denied"),
        PostgresException {SqlState:"42P01" or "42703" or "42501"}=>new("review",Code:"cleanup_schema_review_required"),
        PostgresException {SqlState:"55P03" or "40P01"}=>new("busy",Code:"cleanup_lock_busy"),
        PostgresException {SqlState:"57014"}=>new("timeout",Code:"cleanup_database_timeout"),
        OperationCanceledException when !caller.IsCancellationRequested&&deadline.IsCancellationRequested=>new("timeout",Code:"cleanup_timeout"), _=>null
    };
}
