using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Dashboard;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Logging;

public sealed record ErrorLogFilter(int Skip=0,int Limit=50,string? Module=null,string? Category=null,
    bool? Resolved=null,string? Search=null,string? Start=null,string? End=null);
public sealed record ErrorLogResult(int Status,object Value,long? Total=null);

// Explicit tenant ownership only: neither historical usernames nor current client homes infer ownership.
public sealed class DiagnosticErrorManagementService(PostgresReadSnapshot snapshots,PostgresTransactions transactions,
    PostgresAuditWriter audit,LogSanitizer sanitizer,IOptions<ErrorLogManagementOptions> options)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    public Task<ErrorLogResult> ReadAsync(VerifiedSession? session,ErrorLogFilter filter,int? id=null,bool stats=false,CancellationToken ct=default)=>
        Run(async token=>
        {
            if(!Valid(filter)||id is <=0)throw new WorkspaceRejected("invalid_error_log_request",422);
            var result=await snapshots.ExecuteAsync(async(db,inner)=>
            {
                var actor=await WorkspaceReadScope.Authorize(db,session,true,inner);
                if(stats)return new ErrorLogResult(200,await Stats(db,actor.TenantId,inner));
                return await Rows(db,actor.TenantId,filter,id,inner);
            },token);
            await snapshots.ExecuteAsync(async(db,inner)=>await WorkspaceReadScope.Authorize(db,session,true,inner),token);
            return result;
        },ct);

    public Task<ErrorLogResult> ChangeAsync(VerifiedSession? session,int id,bool? resolved,string? notes,CancellationToken ct=default)=>
        Run(token=>transactions.ExecuteAsync(async(db,inner)=>
        {
            if(id<1||notes?.Length>1000)throw new WorkspaceRejected("invalid_error_log_request",422);
            var actor=await WorkspaceReadScope.Authorize(db,session,true,inner,writing:true);
            await using(var exists=db.CreateCommand("SELECT id FROM public.error_log WHERE id=$1 AND tenant_id=$2 FOR UPDATE"))
            {exists.Parameters.AddWithValue(id);exists.Parameters.AddWithValue(actor.TenantId);
                if(await exists.ExecuteScalarAsync(inner) is null)throw new WorkspaceRejected("Error log not found",404);}
            await using(var command=db.CreateCommand(resolved is null?
                "DELETE FROM public.error_log WHERE id=$1 AND tenant_id=$2":
                "UPDATE public.error_log SET is_resolved=$3,resolution_notes=$4,resolved_by=$5,resolved_at=CASE WHEN $3 THEN now() ELSE NULL END WHERE id=$1 AND tenant_id=$2"))
            {
                command.Parameters.AddWithValue(id);command.Parameters.AddWithValue(actor.TenantId);
                if(resolved is not null){command.Parameters.AddWithValue(resolved.Value);
                    PostgresAuditWriter.Add(command,NpgsqlTypes.NpgsqlDbType.Text,notes is null?null:sanitizer.Text(notes,1000));
                    command.Parameters.AddWithValue(sanitizer.Text(actor.Name,256));}
                await command.ExecuteNonQueryAsync(inner);
            }
            await audit.WriteAsync(db,new(Guid.NewGuid(),Guid.NewGuid(),new("authenticated",actor.UserId,actor.Name),null,
                resolved is null?"DeleteError":resolved.Value?"ResolveError":"UnresolveError","ErrorLog",
                JsonSerializer.SerializeToElement(new{tenant_id=actor.TenantId,record_id=id})),inner);
            return new ErrorLogResult(200,new{message=resolved is null?"Error log deleted successfully":"Error log updated successfully"});
        },token),ct);

    private async Task<ErrorLogResult> Run(Func<CancellationToken,Task<ErrorLogResult>> operation,CancellationToken ct)
    {
        if(!options.Value.Enabled)return new(503,new{detail="Error management is unavailable"});
        if(!await requests.WaitAsync(0,ct))return new(429,new{detail="Error management is busy"});
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try{var result=await operation(deadline.Token);
            if(JsonSerializer.SerializeToUtf8Bytes(result.Value).Length>options.Value.MaximumResponseBytes)throw new WorkspaceRejected("error_log_response_capacity",413);
            return result;}
        catch(WorkspaceRejected e){return new(e.Status,new{detail=e.Message});}
        catch(PostgresException e) when(e.SqlState is "42P01" or "42703" or "42501"){return new(503,new{detail="Error management requires schema review"});}
        catch(PostgresException e) when(e.SqlState is "57014" or "55P03"){return new(504,new{detail="Error management timed out"});}
        catch(OperationCanceledException) when(!ct.IsCancellationRequested){return new(504,new{detail="Error management timed out"});}
        finally{requests.Release();}
    }
    private static bool Valid(ErrorLogFilter f)=>f.Skip>=0&&f.Limit is >=1 and <=200&&f.Module?.Length is not >100
        &&f.Category?.Length is not >100&&f.Search?.Length is not >512
        &&(f.Start is null||DateOnly.TryParseExact(f.Start,"yyyy-MM-dd",out _))
        &&(f.End is null||DateOnly.TryParseExact(f.End,"yyyy-MM-dd",out _))
        &&(f.Start is null||f.End is null||string.CompareOrdinal(f.Start,f.End)<=0);
    private async Task<ErrorLogResult> Rows(PostgresTransactionContext db,int tenant,ErrorLogFilter f,int? id,CancellationToken ct)
    {
        await using var command=db.CreateCommand("");command.Parameters.AddWithValue(tenant);
        var where="tenant_id=$1";
        void Add(string expression,object value){command.Parameters.AddWithValue(value);where+=" AND "+expression.Replace("?","$"+command.Parameters.Count);}
        if(id is not null)Add("id=?",id.Value);
        if(f.Module is not null)Add("module=?",f.Module);
        if(f.Category is not null)Add("error_category=?",f.Category);
        if(f.Resolved is not null)Add("COALESCE(is_resolved,FALSE)=?",f.Resolved.Value);
        // Substring search treats wildcard characters literally and never exposes raw input_data.
        if(f.Search is not null)Add("(strpos(lower(COALESCE(error_message,'')),lower(?))>0 OR strpos(lower(COALESCE(file_name,'')),lower(?))>0)",f.Search);
        if(f.Start is not null)Add("created_at>=?",DateTime.SpecifyKind(DateOnly.ParseExact(f.Start,"yyyy-MM-dd").ToDateTime(TimeOnly.MinValue),DateTimeKind.Utc));
        if(f.End is not null)Add("(created_at AT TIME ZONE 'UTC')::date<=?",DateOnly.ParseExact(f.End,"yyyy-MM-dd"));
        command.CommandText="SELECT count(*) FROM public.error_log WHERE "+where;
        long total=(long)(await command.ExecuteScalarAsync(ct))!;
        if(id is not null&&total==0)throw new WorkspaceRejected("Error log not found",404);
        command.Parameters.AddWithValue(id is null?f.Limit:1);int limit=command.Parameters.Count;
        command.Parameters.AddWithValue(f.Skip);
        // Omit request contents, source code and filesystem paths from every projection.
        command.CommandText="""
            SELECT id,error_code,user_id,client_id,module,endpoint,method,status_code,error_category,
              error_message,stack_trace,suggested_fix,is_resolved,resolution_notes,resolved_by,resolved_at,created_at
            FROM public.error_log WHERE
            """+" "+where+$" ORDER BY created_at DESC NULLS LAST,id DESC LIMIT ${limit} OFFSET ${limit+1}";
        var rows=new List<object>();await using var reader=await command.ExecuteReaderAsync(ct);
        string? Text(int column,int max=1000)=>reader.IsDBNull(column)?null:sanitizer.Text(reader.GetString(column),max);
        string? Date(int column)=>reader.IsDBNull(column)?null:reader.GetDateTime(column).ToUniversalTime().ToString("dd/MM/yyyy HH:mm",CultureInfo.InvariantCulture);
        while(await reader.ReadAsync(ct))rows.Add(new{id=reader.GetInt32(0),error_code=Text(1),user_id=Text(2,256),
            client_id=reader.IsDBNull(3)?(int?)null:reader.GetInt32(3),module=Text(4,100),endpoint=Text(5,500),method=Text(6,16),
            status_code=reader.IsDBNull(7)?(int?)null:reader.GetInt32(7),error_category=Text(8,100),error_message=Text(9),
            file_name=(string?)null,line_number=(int?)null,code_context=(string?)null,stack_trace=id is null?null:Text(10,5000),
            input_data=(string?)null,suggested_fix=Text(11,500),is_resolved=!reader.IsDBNull(12)&&reader.GetBoolean(12),
            resolution_notes=Text(13),resolved_by=Text(14,256),resolved_at=Date(15),created_at=Date(16)});
        return new(200,id is null?rows:rows.Single(),total);
    }
    private async Task<object> Stats(PostgresTransactionContext db,int tenant,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT count(*),count(*) FILTER(WHERE is_resolved IS TRUE) FROM public.error_log WHERE tenant_id=$1");
        command.Parameters.AddWithValue(tenant);long total,resolved;
        await using(var reader=await command.ExecuteReaderAsync(ct)){await reader.ReadAsync(ct);total=reader.GetInt64(0);resolved=reader.GetInt64(1);}
        command.CommandText="SELECT COALESCE(error_category,'Unknown'),count(*) FROM public.error_log WHERE tenant_id=$1 GROUP BY error_category ORDER BY count(*) DESC LIMIT 1001";
        var categories=new Dictionary<string,long>();await using(var reader=await command.ExecuteReaderAsync(ct))
        {while(await reader.ReadAsync(ct)){if(categories.Count>=1000)throw new WorkspaceRejected("error_category_capacity",413);
            string category=sanitizer.Text(reader.GetString(0),100);if(category.Length>100)throw new WorkspaceRejected("error_category_review",409);
            categories[category]=categories.GetValueOrDefault(category)+reader.GetInt64(1);}}
        command.CommandText="SELECT (created_at AT TIME ZONE 'UTC')::date::text,count(*) FROM public.error_log WHERE tenant_id=$1 AND created_at>=((now() AT TIME ZONE 'UTC')::date-7) AT TIME ZONE 'UTC' GROUP BY 1 ORDER BY 1 DESC";
        var days=new Dictionary<string,long>();await using(var reader=await command.ExecuteReaderAsync(ct))
        {while(await reader.ReadAsync(ct))days[reader.GetString(0)]=reader.GetInt64(1);}
        return new{total,resolved,pending=total-resolved,category_breakdown=categories,daily_counts=days};
    }
}
