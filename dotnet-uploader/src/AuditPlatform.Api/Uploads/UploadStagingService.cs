using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Workbooks;
using Microsoft.Extensions.Options;
using NpgsqlTypes;

namespace AuditPlatform.Api.Uploads;

public sealed record StagedHeader(int Position,JsonElement Value);
public sealed record StagedSheet(string Name,IReadOnlyList<StagedHeader> Headers);
public sealed record StagingResult(string Status,int? FileId=null,Guid? RequestId=null,string? State=null,
    long? Bytes=null,string? Sha256=null,IReadOnlyList<StagedSheet>? Sheets=null,string? Code=null);

// Local private storage is a rehearsal boundary. Public activation still requires
// durable private storage and import acceptance; this module writes no transactions.
public sealed class UploadStagingService(PostgresTransactions transactions,PostgresAuditWriter audit,IOptions<UploadStagingOptions> options,TimeProvider? clock=null)
{
    private DateTime Now=>(clock??TimeProvider.System).GetUtcNow().UtcDateTime;
    private readonly SemaphoreSlim receipts=new(options.Value.MaximumConcurrentReceipts);
    private readonly object rootLock=new();
    private string? root;
    private sealed record Actor(int Tenant,int User,string Name);
    private sealed record Receipt(int Id,Guid Request,Guid Key,string State,DateTime Expiry,long? Bytes,string? Hash,string? Discovery);
    private sealed class Denied : Exception;
    private sealed class InvalidWorkbook : Exception;

    public async Task<StagingResult> ReceiveAsync(VerifiedSession? session,int client,int template,Guid request,string filename,Stream input,CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(string.IsNullOrWhiteSpace(filename) || filename.Length>255 || filename.Contains('\0')) return new("invalid",Code:"invalid_workbook");
        string extension=Path.GetExtension(filename).ToLowerInvariant();
        if(request==Guid.Empty || extension is not (".xlsx" or ".xls")) return new("invalid",Code:"invalid_workbook");
        if(!await receipts.WaitAsync(0,ct)) return new("busy");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.ReceiptTimeoutSeconds)); ct=deadline.Token;
        Receipt? receipt=null;
        try
        {
            var reservation=await AuthorizedAsync(session,client,template,async(db,actor,token)=>
            {
                var existing=await FindAsync(db,actor,client,template,request,null,token);
                if(existing is not null) return (Receipt:existing,Created:false);
                await using(var collision=db.CreateCommand("SELECT 1 FROM public.platform_upload_staging WHERE request_id=$1"))
                { collision.Parameters.AddWithValue(request); if(await collision.ExecuteScalarAsync(token) is not null) throw new Denied(); }
                int id;
                await using(var insert=db.CreateCommand("""
                    INSERT INTO public.uploaded_files(file_name,file_path,file_type,record_count,status,uploaded_by,client_id,template_id)
                    VALUES($1,'','excel',0,'Pending',$2,$3,$4) RETURNING id
                    """))
                {
                    // The original name is display metadata only, never a path or log field.
                    insert.Parameters.AddWithValue(Path.GetFileName(filename.Replace('\\','/')));
                    insert.Parameters.AddWithValue(actor.Name); insert.Parameters.AddWithValue(client); insert.Parameters.AddWithValue(template);
                    id=(int)(await insert.ExecuteScalarAsync(token))!;
                }
                var key=Guid.NewGuid();
                await using(var insert=db.CreateCommand("""
                    INSERT INTO public.platform_upload_staging(request_id,upload_id,tenant_id,user_id,client_id,template_id,storage_key,expires_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,now()+make_interval(mins=>$8))
                    """))
                {
                    insert.Parameters.AddWithValue(request); insert.Parameters.AddWithValue(id); insert.Parameters.AddWithValue(actor.Tenant);
                    insert.Parameters.AddWithValue(actor.User); insert.Parameters.AddWithValue(client); insert.Parameters.AddWithValue(template);
                    insert.Parameters.AddWithValue(key); insert.Parameters.AddWithValue(options.Value.LifetimeMinutes);
                    await insert.ExecuteNonQueryAsync(token);
                }
                await AuditAsync(db,actor,client,template,id,"Reserve",token);
                return (Receipt:(await FindAsync(db,actor,client,template,request,null,token))!,Created:true);
            },ct);
            receipt=reservation.Receipt;
            if(!reservation.Created) return new("conflict",receipt.Id,request,receipt.State,Code:"check_upload_status");
            // Reserve audit distinguishes a new receipt without trusting a browser retry.
            // A repeated request is reconciled through status, never replays the file write.
            string partial=FilePath(receipt.Key,".part"), complete=FilePath(receipt.Key,".bin");
            if(receipt.State!="receiving" || File.Exists(partial) || File.Exists(complete))
                return new("conflict",receipt.Id,request,receipt.State,Code:"check_upload_status");
            long length=0; string hash;
            // CreateNew prevents simultaneous deliveries of the same request from
            // replacing bytes. All incomplete files remain owner-tracked until expiry.
            using(var digest=IncrementalHash.CreateHash(HashAlgorithmName.SHA256))
            {
                await using var output=new FileStream(partial,FileMode.CreateNew,FileAccess.Write,FileShare.None,65536,FileOptions.Asynchronous);
                var buffer=new byte[65536]; int count;
                while((count=await input.ReadAsync(buffer,ct))>0)
                {
                    length+=count; if(length>options.Value.MaximumFileBytes) throw new InvalidWorkbook();
                    digest.AppendData(buffer,0,count); await output.WriteAsync(buffer.AsMemory(0,count),ct);
                }
                if(length==0) throw new InvalidWorkbook();
                await output.FlushAsync(ct); hash=Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant();
            }
            IReadOnlyList<StagedSheet> sheets;
            try { sheets=Discover(partial,extension,ct); }
            catch(Exception error) when(error is not (OperationCanceledException or OutOfMemoryException)) { throw new InvalidWorkbook(); }
            File.Move(partial,complete,false);
            return await AuthorizedAsync(session,client,template,async(db,actor,token)=>
            {
                var current=await FindAsync(db,actor,client,template,request,null,token)??throw new Denied();
                if(current.State!="receiving" || current.Expiry<=Now) return new StagingResult("gone",current.Id,request,Code:"staged_file_expired");
                await using(var update=db.CreateCommand("UPDATE public.platform_upload_staging SET state='ready',byte_count=$1,sha256=$2,discovery=$3 WHERE request_id=$4"))
                {
                    update.Parameters.AddWithValue(length); update.Parameters.AddWithValue(hash);
                    update.Parameters.AddWithValue(NpgsqlDbType.Jsonb,JsonSerializer.Serialize(sheets)); update.Parameters.AddWithValue(request);
                    await update.ExecuteNonQueryAsync(token);
                }
                await AuditAsync(db,actor,client,template,current.Id,"Ready",token);
                return new StagingResult("success",current.Id,request,"ready",length,hash,sheets);
            },ct);
        }
        catch(Denied) { return new("forbidden"); }
        catch(PostgresCommitOutcomeUnknownException) { throw; } // Keep bytes/receipt for reconciliation.
        catch(Exception error) when(error is InvalidWorkbook or InvalidDataException or IOException
            or ExcelDataReader.Exceptions.HeaderException or ExcelDataReader.Exceptions.InvalidPasswordException)
        {
            if(receipt is not null)
            {
                try
                {
                    await AuthorizedAsync(session,client,template,async(db,actor,token)=>
                    {
                        await using var reject=db.CreateCommand("UPDATE public.platform_upload_staging SET state='rejected' WHERE request_id=$1 AND state='receiving'");
                        reject.Parameters.AddWithValue(request); await reject.ExecuteNonQueryAsync(token);
                        await AuditAsync(db,actor,client,template,receipt.Id,"Reject",token); return true;
                    },ct);
                }
                catch(Denied) { /* Revoked access leaves the receipt for expiry recovery. */ }
            }
            return new("invalid",receipt?.Id,request,receipt is null?null:"rejected",Code:"invalid_or_interrupted_workbook");
        }
        finally { receipts.Release(); }
    }

    public async Task<StagingResult> StatusAsync(VerifiedSession? session,int client,int template,Guid? request,int? id,bool cleanup=false,CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return new("unavailable");
        Receipt? receipt=null;
        try
        {
            // Verify access before disk work, release DB locks while hashing, then
            // recheck/pin live access and receipt state before recording the result.
            var known=await AuthorizedAsync(session,client,template,
                (db,actor,token)=>FindAsync(db,actor,client,template,request,id,token),ct);
            if(known is null) return new("not_found");
            bool missing=false;
            if(known.State=="ready" && known.Expiry>Now)
            {
                try
                {
                    var path=FilePath(known.Key,".bin");
                    await using var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,65536,FileOptions.Asynchronous);
                    var hash=Convert.ToHexString(await SHA256.HashDataAsync(stream,ct)).ToLowerInvariant();
                    missing=stream.Length!=known.Bytes || hash!=known.Hash;
                }
                catch(FileNotFoundException) { missing=true; }
                catch(DirectoryNotFoundException) { missing=true; }
            }
            var result=await AuthorizedAsync(session,client,template,async(db,actor,token)=>
            {
                receipt=await FindAsync(db,actor,client,template,request,id,token);
                if(receipt is null) return new StagingResult("not_found");
                string state=receipt.State;
                if(receipt.Expiry<=Now) state="expired";
                else if(cleanup) return new StagingResult("conflict",receipt.Id,receipt.Request,state,Code:"upload_not_expired");
                else if(state=="ready" && known.Key==receipt.Key && missing) state="missing";
                if(state!=receipt.State)
                {
                    await using var update=db.CreateCommand("UPDATE public.platform_upload_staging SET state=$1,discovery=CASE WHEN $1='expired' THEN NULL ELSE discovery END WHERE request_id=$2");
                    update.Parameters.AddWithValue(state); update.Parameters.AddWithValue(receipt.Request); await update.ExecuteNonQueryAsync(token);
                    await AuditAsync(db,actor,client,template,receipt.Id,state=="expired"?"Expire":"Missing",token);
                }
                return new StagingResult(state=="ready"?"success":state is "missing" or "expired"?"gone":"conflict",
                    receipt.Id,receipt.Request,state,receipt.Bytes,receipt.Hash,
                    state=="ready"?JsonSerializer.Deserialize<List<StagedSheet>>(receipt.Discovery!):null,
                    state=="missing"?"staged_file_missing":state=="expired"?"staged_file_expired":"check_upload_status");
            },ct);
            // Commit first. A failed or uncertain audit/commit cannot authorize deletion.
            if(cleanup && result.State=="expired" && receipt is not null)
            {
                foreach(var suffix in new[]{".part",".bin"}) File.Delete(FilePath(receipt.Key,suffix));
                return result with {Status="success"};
            }
            return result;
        }
        catch(Denied) { return new("forbidden"); }
    }

    // Only this owner-bound boundary may expose a generated private path to an
    // internal reader. Database locks are released during file parsing, then
    // live access, expiry, source and configuration are checked again.
    internal async Task<OwnedWorkbookResult<T>> ReadOwnedAsync<T>(VerifiedSession? session,int client,int template,int id,
        string? expectedHash,string? expectedConfiguration,
        Func<string,string,TemplateDefinition,WorkbookReadLimits,CancellationToken,Task<T>> read,CancellationToken ct,bool recheckAfterRead=true)
    {
        if(!options.Value.Enabled) return new("unavailable");
        try
        {
            async Task<(Receipt? Receipt,TemplateDefinition Definition)> Snapshot()=>await AuthorizedAsync(session,client,template,async(db,actor,token)=>
                (await FindAsync(db,actor,client,template,null,id,token),
                    (await TenantTemplateService.LoadAsync(db,actor.Tenant,template,token)).Single()),ct);
            var before=await Snapshot(); var receipt=before.Receipt;
            if(receipt is null) return new("not_found");
            if(receipt.State!="ready" || receipt.Expiry<=Now) return new("gone",Code:"staged_file_unavailable");
            if(expectedHash is not null && expectedHash!=receipt.Hash || expectedConfiguration is not null && expectedConfiguration!=before.Definition.ConfigurationKey)
                return new("conflict",Code:"preparation_context_changed");
            string path=FilePath(receipt.Key,".bin");
            // The lease prevents replacement on Windows; verify the registered
            // bytes both before and after parsing on all supported hosts.
            await using var lease=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,65536,FileOptions.Asynchronous);
            async Task<bool> Matches()
            {
                await using var source=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,65536,FileOptions.Asynchronous);
                return source.Length==receipt.Bytes && Convert.ToHexString(await SHA256.HashDataAsync(source,ct)).ToLowerInvariant()==receipt.Hash;
            }
            if(!await Matches()) return new("gone",Code:"staged_file_changed");
            var value=options.Value;
            var limits=new WorkbookReadLimits(value.MaximumFileBytes,value.MaximumExpandedBytes,value.MaximumSheets,value.MaximumColumns,value.MaximumRows,value.MaximumCellCharacters);
            // The generated suffix is .bin. Reader format detection uses the
            // source signature; the .xls hint still applies ZIP expansion guards.
            var output=await read(path,receipt.Hash!,before.Definition,limits,ct);
            // Import and no-change receipt coordinators recheck bytes/access
            // inside their transaction. Durable outcomes survive later expiry.
            if(!recheckAfterRead) return new("success",output);
            if(!await Matches()) return new("gone",Code:"staged_file_changed");
            var after=await Snapshot();
            if(after.Receipt is not {State:"ready"} current || current.Expiry<=Now) return new("gone",Code:"staged_file_unavailable");
            if(current.Key!=receipt.Key || current.Hash!=receipt.Hash || current.Bytes!=receipt.Bytes
                || before.Definition.ConfigurationKey!=after.Definition.ConfigurationKey) return new("conflict",Code:"preparation_context_changed");
            return new("success",output);
        }
        catch(Denied) {return new("forbidden");}
        catch(FileNotFoundException) {return new("gone",Code:"staged_file_missing");}
        catch(DirectoryNotFoundException) {return new("gone",Code:"staged_file_missing");}
    }

    private async Task<T> AuthorizedAsync<T>(VerifiedSession? session,int client,int template,
        Func<PostgresTransactionContext,Actor,CancellationToken,Task<T>> action,CancellationToken ct)
    {
        if(session is null || !session.TenantContextRequired || session.TenantId is not >0 || !AuthenticationService.ValidToken(session.Token)) throw new Denied();
        return await transactions.ExecuteAsync(async(db,token)=>
        {
            await using(var serial=db.CreateCommand("SELECT pg_advisory_xact_lock(746102)")) await serial.ExecuteNonQueryAsync(token);
            await using(var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE"))
            { tenant.Parameters.AddWithValue(session.TenantId.Value); if(await tenant.ExecuteScalarAsync(token) is null) throw new Denied(); }
            Actor actor; string role;
            await using(var command=db.CreateCommand("""
                SELECT u.id,u.username,m.tenant_role FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE AND s.selected_tenant_id=$3
                  AND s.context_version=$4 AND s.selected_client_id=$5 AND m.tenant_id=$3 AND m.is_active IS TRUE FOR SHARE OF s,u,m
                """))
            {
                command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId); command.Parameters.AddWithValue(session.TenantId.Value);
                command.Parameters.AddWithValue(session.ContextVersion); command.Parameters.AddWithValue(client);
                await using var reader=await command.ExecuteReaderAsync(token); if(!await reader.ReadAsync(token)) throw new Denied();
                actor=new(session.TenantId.Value,reader.GetInt32(0),reader.GetString(1)); role=reader.GetString(2);
            }
            await using(var home=db.CreateCommand("SELECT c.id FROM public.clients c JOIN public.platform_tenant_client_homes h ON h.client_id=c.id WHERE c.id=$1 AND h.tenant_id=$2 AND c.is_active IS TRUE FOR SHARE OF c,h"))
            { home.Parameters.AddWithValue(client); home.Parameters.AddWithValue(actor.Tenant); if(await home.ExecuteScalarAsync(token) is null) throw new Denied(); }
            if(role!="admin")
            {
                await using var assignment=db.CreateCommand("""
                    SELECT a.user_id FROM public.platform_engagement_assignments a JOIN public.platform_audit_engagements e
                      ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                    WHERE a.firm_tenant_id=$1 AND a.user_id=$2 AND e.client_id=$3 AND a.team_role IN('manager','internee')
                      AND a.is_active IS TRUE AND e.is_active IS TRUE FOR SHARE OF a,e
                    """);
                assignment.Parameters.AddWithValue(actor.Tenant); assignment.Parameters.AddWithValue(actor.User); assignment.Parameters.AddWithValue(client);
                if(await assignment.ExecuteScalarAsync(token) is null) throw new Denied();
            }
            var definition=(await TenantTemplateService.LoadAsync(db,actor.Tenant,template,token)).SingleOrDefault();
            if(definition is null || definition.Origin!="native" || definition.Columns.Count==0 || definition.Columns.Any(c=>!c.Active)) throw new Denied();
            await using(var bindings=db.CreateCommand("""
                SELECT count(*),count(*) FILTER(WHERE p.tenant_id=$2 AND p.storage_key=m.column_name AND c.binding_origin='native' AND m.is_active IS TRUE)
                FROM public.template_column_mappings p LEFT JOIN public.master_columns m ON m.id=p.column_id
                LEFT JOIN public.platform_tenant_columns c ON c.column_id=p.column_id AND c.tenant_id=p.tenant_id
                WHERE p.template_id=$1
                """))
            { bindings.Parameters.AddWithValue(template); bindings.Parameters.AddWithValue(actor.Tenant);
              await using var reader=await bindings.ExecuteReaderAsync(token); await reader.ReadAsync(token);
              if(reader.GetInt64(0)!=definition.Columns.Count || reader.GetInt64(1)!=definition.Columns.Count) throw new Denied(); }
            return await action(db,actor,token);
        },ct);
    }

    private static async Task<Receipt?> FindAsync(PostgresTransactionContext db,Actor actor,int client,int template,Guid? request,int? id,CancellationToken ct)
    {
        await using var command=db.CreateCommand("""
            SELECT upload_id,request_id,storage_key,state,expires_at,byte_count,sha256,discovery::text FROM public.platform_upload_staging
            WHERE tenant_id=$1 AND user_id=$2 AND client_id=$3 AND template_id=$4
              AND (($5::uuid IS NOT NULL AND request_id=$5) OR ($6::integer IS NOT NULL AND upload_id=$6)) FOR UPDATE
            """);
        command.Parameters.AddWithValue(actor.Tenant); command.Parameters.AddWithValue(actor.User); command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(template);
        command.Parameters.AddWithValue(NpgsqlDbType.Uuid,(object?)request??DBNull.Value); command.Parameters.AddWithValue(NpgsqlDbType.Integer,(object?)id??DBNull.Value);
        await using var reader=await command.ExecuteReaderAsync(ct); if(!await reader.ReadAsync(ct)) return null;
        return new(reader.GetInt32(0),reader.GetGuid(1),reader.GetGuid(2),reader.GetString(3),reader.GetDateTime(4),
            reader.IsDBNull(5)?null:reader.GetInt64(5),reader.IsDBNull(6)?null:reader.GetString(6),reader.IsDBNull(7)?null:reader.GetString(7));
    }
    private Task<int> AuditAsync(PostgresTransactionContext db,Actor actor,int client,int template,int id,string action,CancellationToken ct)=>
        audit.WriteAsync(db,new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),new LogActor("authenticated",actor.User,actor.Name),client,
            action,"UploadStaging",JsonSerializer.SerializeToElement(new {tenant_id=actor.Tenant,template_id=template,record_id=id})),ct);

    private string FilePath(Guid key,string suffix)
    {
        lock(rootLock)
        {
            var path=Path.GetFullPath(options.Value.RootDirectory);
            // Refuse reparse directories and source/output trees; filenames are generated UUIDs.
            for(var directory=new DirectoryInfo(path);directory is not null;directory=directory.Parent)
            {
                if(directory.Exists && (directory.Attributes&FileAttributes.ReparsePoint)!=0) throw new IOException("Unsafe staging directory");
                if(File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx")) || File.Exists(Path.Combine(directory.FullName,"CLAUDE.md")))
                    throw new IOException("Staging must be outside the application tree");
            }
            if(root is null)
            {
                Directory.CreateDirectory(path); var marker=Path.Combine(path,".auditplatform-staging");
                if(!File.Exists(marker))
                {
                    if(Directory.EnumerateFileSystemEntries(path).Any()) throw new IOException("Use an empty private staging directory");
                    File.WriteAllText(marker,"AuditPlatform.LocalStaging.v1");
                }
                if(File.ReadAllText(marker)!="AuditPlatform.LocalStaging.v1") throw new IOException("Invalid staging ownership marker");
                root=path;
            }
            if(root!=path) throw new IOException("Staging directory changed");
            if(!Directory.Exists(path)) throw new DirectoryNotFoundException();
            var ownership=Path.Combine(path,".auditplatform-staging");
            if(!File.Exists(ownership) || File.ReadAllText(ownership)!="AuditPlatform.LocalStaging.v1") throw new IOException("Staging ownership changed");
            var file=Path.Combine(root,key.ToString("N")+suffix);
            if(File.Exists(file) && (File.GetAttributes(file)&FileAttributes.ReparsePoint)!=0) throw new IOException("Unsafe staged file");
            return file;
        }
    }

    private IReadOnlyList<StagedSheet> Discover(string path,string extension,CancellationToken ct)
    {
        var value=options.Value;
        var limits=new AuditPlatform.Api.Workbooks.WorkbookReadLimits(value.MaximumFileBytes,value.MaximumExpandedBytes,
            value.MaximumSheets,value.MaximumColumns,value.MaximumRows,value.MaximumCellCharacters);
        var result=AuditPlatform.Api.Workbooks.WorkbookReader.Discover(path,extension,limits,ct)
            .Select(sheet=>new StagedSheet(sheet.Name,sheet.Header.Select(cell=>new StagedHeader(cell.Column-1,HeaderValue(cell))).ToArray())).ToArray();
        if(JsonSerializer.SerializeToUtf8Bytes(result).Length>1048576) throw new InvalidWorkbook();
        return result;
    }

    private static JsonElement HeaderValue(AuditPlatform.Api.Workbooks.WorkbookCell cell)
    {
        // Keep staging's existing JSON value envelope. Full typed cells remain
        // available to subsequent preparation/validation phases via the reader.
        object? value=cell.Kind switch {
            AuditPlatform.Api.Workbooks.WorkbookCellKind.Text=>cell.Text,
            AuditPlatform.Api.Workbooks.WorkbookCellKind.Number=>double.Parse(cell.NumberToken!,System.Globalization.CultureInfo.InvariantCulture),
            AuditPlatform.Api.Workbooks.WorkbookCellKind.Boolean=>cell.Boolean,
            AuditPlatform.Api.Workbooks.WorkbookCellKind.DateTime=>cell.DateTime,
            AuditPlatform.Api.Workbooks.WorkbookCellKind.Time or AuditPlatform.Api.Workbooks.WorkbookCellKind.Duration=>cell.Time,
            AuditPlatform.Api.Workbooks.WorkbookCellKind.Error=>throw new AuditPlatform.Api.Workbooks.WorkbookReadException("An Excel header contains an error."),
            _=>null};
        return JsonSerializer.SerializeToElement(value);
    }

}
