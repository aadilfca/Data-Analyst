using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Dashboard;

public sealed record DashboardResult(int Status, object Value);

public sealed class DashboardService(PostgresReadSnapshot snapshots, IOptions<DashboardOptions> options, TimeProvider? clock = null)
{
    private readonly SemaphoreSlim requests = new(options.Value.MaximumConcurrentRequests);
    public async Task<DashboardResult> ReadAsync(VerifiedSession? session, string operation, CancellationToken ct = default)
    {
        if (!options.Value.Enabled) return new(503,new { code="dashboard_disabled" });
        if (!await requests.WaitAsync(0,ct)) return new(429,new { code="dashboard_busy" });
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var body=await snapshots.ExecuteAsync(async(db,token)=>
            {
                var actor=await WorkspaceReadScope.Authorize(db,session,operation=="db-size",token);
                return operation switch
                {
                    "stats"=>await Stats(db,actor,token), "kpi-stats"=>await Kpis(db,actor,token),
                    "db-size"=>await Size(db,token), "overdue-uploads"=>await Overdue(db,actor,token),
                    _=>throw new WorkspaceRejected("dashboard_operation_unknown",404)
                };
            },deadline.Token);
            // A second snapshot rejects revocation or changed workspace while a slow query ran.
            await snapshots.ExecuteAsync(async(db,token)=>await WorkspaceReadScope.Authorize(db,session,operation=="db-size",token),deadline.Token);
            if(JsonSerializer.SerializeToUtf8Bytes(body).Length>options.Value.MaximumResponseBytes) throw new WorkspaceRejected("dashboard_response_capacity",413);
            return new(200,body);
        }
        catch(WorkspaceRejected e){return new(e.Status,new {code=e.Message});}
        catch(Npgsql.PostgresException e) when(e.SqlState is "42P01" or "42703" or "42501"){return new(503,new{code="dashboard_schema_review_required"});}
        catch(Npgsql.PostgresException e) when(e.SqlState is "57014" or "55P03"){return new(504,new{code="dashboard_timeout"});}
        catch(OperationCanceledException) when(!ct.IsCancellationRequested){return new(504,new{code="dashboard_timeout"});}
        finally{requests.Release();}
    }
    private static async Task<object> Stats(PostgresTransactionContext db,WorkspaceReadScope.Actor actor,CancellationToken ct)
    {
        await using var command=db.CreateCommand(WorkspaceReadScope.Clients+"""
            SELECT (SELECT count(*) FROM public.uploaded_files u JOIN allowed_clients c ON c.id=u.client_id),
              (SELECT count(*) FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id
               JOIN allowed_clients c ON c.id=s.client_id WHERE s.status<>'Cancelled' AND d.range_status='active' AND d.status='Pending'),
              (SELECT count(*) FROM public.client_data d JOIN allowed_clients c ON c.id=d.client_id
               WHERE d.is_deleted IS FALSE AND d.change_detection_status<>'deleted'),
              (SELECT max(upload_date) FROM public.uploaded_files u JOIN allowed_clients c ON c.id=u.client_id)
            """);
        WorkspaceReadScope.Parameters(command,actor);await using var reader=await command.ExecuteReaderAsync(ct);await reader.ReadAsync(ct);
        return new {total_uploads=reader.GetInt64(0),pending_verifications=reader.GetInt64(1),active_modules=2,
            total_records=reader.GetInt64(2),last_upload=reader.IsDBNull(3)?(DateTime?)null:reader.GetDateTime(3)};
    }
    private static async Task<object> Kpis(PostgresTransactionContext db,WorkspaceReadScope.Actor actor,CancellationToken ct)
    {
        await using var command=db.CreateCommand(WorkspaceReadScope.Clients+"""
            , active_lines AS (
              SELECT d.*,s.client_id FROM public.verification_details d
              JOIN public.verification_sessions s ON s.id=d.session_id
              JOIN allowed_clients c ON c.id=s.client_id WHERE d.range_status='active' AND s.status<>'Cancelled'),
            latest AS (SELECT DISTINCT ON(client_data_id) client_id,transaction_ref,cleared_status
              FROM active_lines ORDER BY client_data_id,id DESC),
            vouchers AS (SELECT client_id,transaction_ref,bool_and(COALESCE(cleared_status='Cleared',FALSE)) cleared
              FROM latest WHERE transaction_ref IS NOT NULL AND transaction_ref<>'' GROUP BY client_id,transaction_ref)
            SELECT (SELECT count(*) FROM active_lines WHERE cleared_status='Query'),
              (SELECT count(*) FROM vouchers),(SELECT count(*) FROM vouchers WHERE cleared),
              (SELECT CASE WHEN $3 THEN count(*) ELSE NULL END FROM public.error_log WHERE tenant_id=$1 AND is_resolved IS NOT TRUE)
            """);
        WorkspaceReadScope.Parameters(command,actor);await using var reader=await command.ExecuteReaderAsync(ct);await reader.ReadAsync(ct);
        long total=reader.GetInt64(1),cleared=reader.GetInt64(2);
        return new {open_queries=reader.GetInt64(0),verification_progress_pct=total==0?0:Math.Round(100m*cleared/total,1,MidpointRounding.ToEven),
            total_vouchers=total,cleared_vouchers=cleared,unresolved_errors=reader.IsDBNull(3)?(long?)null:reader.GetInt64(3)};
    }
    private static async Task<object> Size(PostgresTransactionContext db,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT pg_database_size(current_database())");long bytes=(long)(await command.ExecuteScalarAsync(ct))!;
        return new {total_bytes=bytes,formatted=(bytes/(1024m*1024)).ToString("F1",CultureInfo.InvariantCulture)+" MB",scope="shared_database"};
    }
    private async Task<object> Overdue(PostgresTransactionContext db,WorkspaceReadScope.Actor actor,CancellationToken ct)
    {
        // Aggregate source dates once. Data dates, rather than file-import times, drive overdue status.
        await using var command=db.CreateCommand(WorkspaceReadScope.Clients+"""
            SELECT c.id,c.client_name,t.id,n.display_name,max(d.extract_date),COALESCE(f.frequency,'Daily')
            FROM allowed_clients c JOIN public.client_data d ON d.client_id=c.id
            JOIN public.templates t ON t.id=d.template_id AND t.is_active IS TRUE
            JOIN public.platform_tenant_templates n ON n.template_id=t.id AND n.tenant_id=$1 AND n.binding_origin='native'
            LEFT JOIN public.client_template_upload_frequency f ON f.client_id=c.id AND f.template_id=t.id
            WHERE d.is_deleted IS FALSE AND d.change_detection_status<>'deleted'
            GROUP BY c.id,c.client_name,t.id,n.display_name,f.frequency ORDER BY c.id,t.id LIMIT $4
            """);
        WorkspaceReadScope.Parameters(command,actor);command.Parameters.AddWithValue(options.Value.MaximumOverdueRows+1);
        var pairs=new List<(int Client,string ClientName,int Template,string TemplateName,DateOnly Last,string Frequency)>();
        await using(var reader=await command.ExecuteReaderAsync(ct))
        {while(await reader.ReadAsync(ct)){if(pairs.Count==options.Value.MaximumOverdueRows)throw new WorkspaceRejected("dashboard_overdue_capacity",413);
            if(!reader.IsDBNull(4))pairs.Add((reader.GetInt32(0),reader.GetString(1),reader.GetInt32(2),reader.GetString(3),reader.GetFieldValue<DateOnly>(4),reader.GetString(5)));}}
        var exact=new HashSet<DateOnly>();var yearly=new HashSet<(int,int)>();
        await using(var holidays=db.CreateCommand("SELECT holiday_date,repeats_yearly FROM public.platform_tenant_holidays WHERE tenant_id=$1 LIMIT 10001"))
        {holidays.Parameters.AddWithValue(actor.TenantId);await using var reader=await holidays.ExecuteReaderAsync(ct);int count=0;
            while(await reader.ReadAsync(ct)){if(++count>10000)throw new WorkspaceRejected("dashboard_holiday_capacity",413);
                var day=reader.GetFieldValue<DateOnly>(0);if(reader.GetBoolean(1))yearly.Add((day.Month,day.Day));else exact.Add(day);}}
        var today=DateOnly.FromDateTime((clock??TimeProvider.System).GetLocalNow().DateTime);
        var rows=new List<(int Days,int Client,int Template,object Value)>();
        foreach(var pair in pairs)
        {
            int allowed=pair.Frequency switch{"Daily"=>1,"Weekly"=>6,"Monthly"=>22,_=>throw new WorkspaceRejected("dashboard_frequency_review_required",409)};
            // Full weeks avoid work proportional to historical data age; holidays are bounded above.
            int span=Math.Max(0,today.DayNumber-pair.Last.DayNumber),elapsed=span/7*6;
            for(int i=1;i<=span%7;i++)if(pair.Last.AddDays(span/7*7+i).DayOfWeek!=DayOfWeek.Sunday)elapsed++;
            var excluded=new HashSet<DateOnly>(exact.Where(d=>d>pair.Last&&d<=today&&d.DayOfWeek!=DayOfWeek.Sunday));
            for(int year=pair.Last.Year;year<=today.Year;year++)foreach(var (month,day) in yearly)
            {ct.ThrowIfCancellationRequested();if(day>DateTime.DaysInMonth(year,month))continue;var date=new DateOnly(year,month,day);
                if(date>pair.Last&&date<=today&&date.DayOfWeek!=DayOfWeek.Sunday)excluded.Add(date);}
            int overdue=elapsed-excluded.Count-allowed;if(overdue>0)rows.Add((overdue,pair.Client,pair.Template,new{
                client_id=pair.Client,client_name=pair.ClientName,template_id=pair.Template,template_name=pair.TemplateName,
                last_upload_date=pair.Last.ToString("yyyy-MM-dd"),frequency=pair.Frequency,days_overdue=overdue}));
        }
        return new {overdue=rows.OrderByDescending(r=>r.Days).ThenBy(r=>r.Client).ThenBy(r=>r.Template).Select(r=>r.Value).ToArray()};
    }
}
