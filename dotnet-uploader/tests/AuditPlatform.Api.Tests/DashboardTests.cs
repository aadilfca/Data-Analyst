using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Dashboard;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using Npgsql;
using System.Net;
using System.Net.Http.Json;
using AuditPlatform.Api.Tenancy;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class DashboardTests(PostgresFixture fixture):IClassFixture<PostgresFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock:TimeProvider
    {public override DateTimeOffset GetUtcNow()=>new(2026,11,18,12,0,0,TimeSpan.Zero);public override TimeZoneInfo LocalTimeZone=>TimeZoneInfo.Utc;}
    [Fact]
    public async Task Workspace_dashboard_and_error_management_preserve_scope_counts_and_atomic_audit()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var control=await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string sql){await using var command=new NpgsqlCommand(sql,control);await command.ExecuteNonQueryAsync(Token);}
        await Sql("""
            CREATE TABLE public.platform_tenants(id INTEGER PRIMARY KEY,tenant_type TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER,expiry TIMESTAMPTZ,selected_tenant_id INTEGER,context_version BIGINT);
            CREATE TABLE public.platform_tenant_memberships(tenant_id INTEGER,user_id INTEGER,tenant_role TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_name TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.platform_tenant_client_homes(client_id INTEGER,tenant_id INTEGER);
            CREATE TABLE public.platform_audit_engagements(id INTEGER,firm_tenant_id INTEGER,client_id INTEGER,is_active BOOLEAN);
            CREATE TABLE public.platform_engagement_assignments(engagement_id INTEGER,firm_tenant_id INTEGER,user_id INTEGER,team_role TEXT,is_active BOOLEAN);
            CREATE TABLE public.templates(id INTEGER PRIMARY KEY,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.platform_tenant_templates(template_id INTEGER,tenant_id INTEGER,display_name TEXT,binding_origin TEXT);
            CREATE TABLE public.client_template_upload_frequency(client_id INTEGER,template_id INTEGER,frequency TEXT);
            CREATE TABLE public.platform_tenant_holidays(tenant_id INTEGER,holiday_date DATE,repeats_yearly BOOLEAN);
            CREATE TABLE public.uploaded_files(id INTEGER,client_id INTEGER,upload_date TIMESTAMPTZ);
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER,ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE,
                ADD COLUMN change_detection_status TEXT DEFAULT 'new',ADD COLUMN extract_date DATE;
            CREATE TABLE public.verification_sessions(id INTEGER,client_id INTEGER,status TEXT);
            CREATE TABLE public.verification_details(id INTEGER,session_id INTEGER,client_data_id INTEGER,
                transaction_ref TEXT,cleared_status TEXT,range_status TEXT,status TEXT);
            INSERT INTO public.platform_tenants VALUES(101,'audit_firm',TRUE),(102,'business',TRUE);
            INSERT INTO public.users VALUES(1,'Tenant A Admin',TRUE),(2,'Assigned staff',TRUE),(3,'Other Admin',TRUE);
            INSERT INTO public.platform_tenant_memberships VALUES(101,1,'admin',TRUE),(101,2,'member',TRUE),(102,3,'admin',TRUE);
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',101,0),
                (repeat('b',64),2,now()+interval '1 hour',101,0),(repeat('c',64),3,now()+interval '1 hour',102,0);
            INSERT INTO public.clients VALUES(1,'Client A',TRUE),(2,'Client B',TRUE),(3,'Other tenant',TRUE);
            INSERT INTO public.platform_tenant_client_homes VALUES(1,101),(2,101),(3,102);
            INSERT INTO public.platform_audit_engagements VALUES(1,101,1,TRUE);
            INSERT INTO public.platform_engagement_assignments VALUES(1,101,2,'internee',TRUE);
            INSERT INTO public.templates VALUES(1,TRUE);
            INSERT INTO public.platform_tenant_templates VALUES(1,101,'Template A','native');
            INSERT INTO public.client_data(client_id,template_id,extract_date) VALUES(1,1,'2026-11-13'),(2,1,'2026-11-18'),(3,1,'2026-01-01');
            INSERT INTO public.uploaded_files VALUES(1,1,'2026-11-13T00:00:00Z'),(2,2,'2026-11-18T00:00:00Z'),(3,3,'2026-12-01T00:00:00Z');
            INSERT INTO public.platform_tenant_holidays VALUES(101,'2026-11-17',FALSE),(102,'2026-11-18',FALSE);
            INSERT INTO public.verification_sessions VALUES(1,1,'In Progress'),(2,1,'In Progress'),(3,2,'In Progress'),(4,3,'In Progress'),(5,1,'Cancelled');
            INSERT INTO public.verification_details VALUES(1,1,1,'Same','Query','active','Pending'),
                (2,2,1,'Same','Cleared','active','Verified'),(3,3,2,'Same',NULL,'active','Pending'),
                (4,4,3,'Foreign','Query','active','Pending'),(5,5,1,'Cancelled','Query','active','Pending');
            INSERT INTO public.error_log(error_message) VALUES('Historical unknown tenant');
            """);
        var root=new DirectoryInfo(AppContext.BaseDirectory);
        while(root is not null&&!File.Exists(Path.Combine(root.FullName,"AuditPlatform.slnx")))root=root.Parent;
        Assert.NotNull(root);
        var migration=await File.ReadAllTextAsync(Path.Combine(root.FullName,"database/migrations/0023_error_log_tenant_scope.sql"),Token);
        await using(var tx=await control.BeginTransactionAsync(Token))
        {await using var command=new NpgsqlCommand(migration,control,tx);await command.ExecuteNonQueryAsync(Token);await tx.RollbackAsync(Token);}
        await Sql(migration);
        await Sql("INSERT INTO public.error_log(tenant_id,error_message,module,error_category) VALUES(101,'Visible <b>text</b>','Upload','System'),(102,'PRIVATE_FOREIGN','Upload','System')");
        using var pool=fixture.CreatePool();var pg=Options.Create(new PostgresOptions());
        var snapshots=new PostgresReadSnapshot(pool,pg);var transactions=new PostgresTransactions(pool,pg);
        var limits=Options.Create(new LogPersistenceOptions());var sanitizer=new LogSanitizer(limits);var audit=new PostgresAuditWriter(sanitizer,limits);
        var errors=new DiagnosticErrorManagementService(snapshots,transactions,audit,sanitizer,Options.Create(new ErrorLogManagementOptions{Enabled=true}));
        var dashboard=new DashboardService(snapshots,Options.Create(new DashboardOptions{Enabled=true}),new Clock());
        VerifiedSession Session(char token,int id,int tenant)=>new(new string(token,64),id,"untrusted snapshot",null,DateTime.UtcNow.AddHours(1),null,null,null,true,tenant,0);
        var admin=Session('a',1,101);var staff=Session('b',2,101);var other=Session('c',3,102);
        JsonElement Body(object value)=>JsonSerializer.SerializeToElement(value);
        var stats=await dashboard.ReadAsync(admin,"stats",Token);Assert.Equal(200,stats.Status);Assert.Equal(2,Body(stats.Value).GetProperty("total_records").GetInt32());
        Assert.Equal(1,Body((await dashboard.ReadAsync(staff,"stats",Token)).Value).GetProperty("total_records").GetInt32());
        var kpi=Body((await dashboard.ReadAsync(admin,"kpi-stats",Token)).Value);
        Assert.Equal(1,kpi.GetProperty("open_queries").GetInt32());Assert.Equal(50m,kpi.GetProperty("verification_progress_pct").GetDecimal());
        Assert.Equal(1,kpi.GetProperty("unresolved_errors").GetInt32());
        Assert.Equal(JsonValueKind.Null,Body((await dashboard.ReadAsync(staff,"kpi-stats",Token)).Value).GetProperty("unresolved_errors").ValueKind);
        Assert.Equal(403,(await dashboard.ReadAsync(staff,"db-size",Token)).Status);
        Assert.True(Body((await dashboard.ReadAsync(admin,"db-size",Token)).Value).GetProperty("total_bytes").GetInt64()>0);
        var overdue=Body((await dashboard.ReadAsync(admin,"overdue-uploads",Token)).Value).GetProperty("overdue");
        Assert.Equal(1,overdue.GetArrayLength());Assert.Equal(2,overdue[0].GetProperty("days_overdue").GetInt32());
        Assert.Equal(401,(await dashboard.ReadAsync(null,"stats",Token)).Status);
        Assert.Equal(403,(await dashboard.ReadAsync(admin with{ContextVersion=1},"stats",Token)).Status);
        var list=await errors.ReadAsync(admin,new(),ct:Token);Assert.Equal(1,list.Total);
        var row=Body(list.Value)[0];int errorId=row.GetProperty("id").GetInt32();
        Assert.Equal(403,(await errors.ReadAsync(staff,new(),ct:Token)).Status);
        Assert.Equal(404,(await errors.ReadAsync(other,new(),errorId,ct:Token)).Status);
        Assert.Equal(0,(await errors.ReadAsync(admin,new(Search:"%"),ct:Token)).Total);
        Assert.Equal(422,(await errors.ReadAsync(admin,new(Start:"bad-date"),ct:Token)).Status);
        Assert.Equal(200,(await errors.ChangeAsync(admin,errorId,true,"password=PRIVATE",Token)).Status);
        var detail=Body((await errors.ReadAsync(admin,new(),errorId,ct:Token)).Value);
        Assert.Equal("Tenant A Admin",detail.GetProperty("resolved_by").GetString());Assert.Equal("[redacted]",detail.GetProperty("resolution_notes").GetString());
        Assert.Equal(JsonValueKind.Null,detail.GetProperty("input_data").ValueKind);
        var errorStats=Body((await errors.ReadAsync(admin,new(),stats:true,ct:Token)).Value);
        Assert.Equal(1,errorStats.GetProperty("total").GetInt32());Assert.Equal(0,errorStats.GetProperty("pending").GetInt32());
        Assert.Equal(1,errorStats.GetProperty("category_breakdown").GetProperty("System").GetInt32());
        Assert.Equal(1,(await errors.ReadAsync(admin,new(Start:"2000-01-01",End:"9999-12-31",Resolved:true),ct:Token)).Total);
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_log_unavailable");
        Assert.Equal(503,(await errors.ChangeAsync(admin,errorId,null,null,Token)).Status);
        Assert.Equal(200,(await errors.ReadAsync(admin,new(),errorId,ct:Token)).Status);
        await Sql("ALTER TABLE public.audit_log_unavailable RENAME TO audit_log");
        var logger=new DiagnosticErrorLogger(transactions,pool,sanitizer,limits,NullLogger<DiagnosticErrorLogger>.Instance,Options.Create(new ErrorLogManagementOptions{Enabled=true}));
        var persisted=await logger.LogAsync(new(Guid.NewGuid(),"p57",new("authenticated",1,"Tenant A Admin"),null,"Dashboard","/api/dashboard/stats","GET",500,"System","Safe error",null,null,TenantId:101),Token);
        Assert.NotNull(persisted.ErrorId);Assert.Equal(2,(await errors.ReadAsync(admin,new(),ct:Token)).Total);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE");
        Assert.Equal(0,Body((await dashboard.ReadAsync(staff,"stats",Token)).Value).GetProperty("total_records").GetInt32());
        Assert.Equal(200,(await errors.ChangeAsync(admin,errorId,null,null,Token)).Status);
        Assert.Equal(404,(await errors.ReadAsync(admin,new(),errorId,ct:Token)).Status);
        // Exercise the real authentication/context/CSRF pipeline using fixture-owned sessions.
        await Sql("ALTER TABLE public.users ADD COLUMN role TEXT; ALTER TABLE public.clients ADD COLUMN client_code TEXT; ALTER TABLE public.sessions ADD COLUMN selected_client_id INTEGER;");
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://localhost";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<DashboardOptions>(o=>o.Enabled=true);
            services.PostConfigure<ErrorLogManagementOptions>(o=>o.Enabled=true);
        }));
        using var http=app.CreateClient(new WebApplicationFactoryClientOptions{HandleCookies=false});
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/dashboard/stats",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/api/dashboard/stats",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(admin));
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/api/dashboard/stats",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PutAsJsonAsync($"/api/error-logs/{persisted.ErrorId}",new{is_resolved=true},Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin","http://localhost");
        Assert.Equal(HttpStatusCode.OK,(await http.PutAsJsonAsync($"/api/error-logs/{persisted.ErrorId}",new{is_resolved=true,resolved_by="FORGED"},Token)).StatusCode);
        http.DefaultRequestHeaders.Remove("Cookie");http.DefaultRequestHeaders.Add("Cookie","session_token="+staff.Token);
        http.DefaultRequestHeaders.Remove("X-App-Context");http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(staff));
        Assert.Equal(HttpStatusCode.Forbidden,(await http.GetAsync("/api/error-logs",Token)).StatusCode);
    }
}
