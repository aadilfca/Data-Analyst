using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.DataBank;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class DataBankTests
{
    private static CancellationToken Ct => TestContext.Current.CancellationToken;
    [Fact]
    public async Task DefaultsAndPrerequisitesRemainDisabled()
    {
        await using var app = new WebApplicationFactory<Program>();
        Assert.False(app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value.Enabled);
        using var client = app.CreateClient(); Assert.Equal(HttpStatusCode.ServiceUnavailable, (await client.GetAsync("/api/data/availability", Ct)).StatusCode);
        await using var invalid = new WebApplicationFactory<Program>().WithWebHostBuilder(b => b.ConfigureServices(s => s.PostConfigure<DataBankOptions>(o => o.Enabled = true)));
        Assert.Throws<OptionsValidationException>(() => invalid.CreateClient());
    }

    [Fact]
    public async Task OwnedReadSnapshotsOracleAuthorizationBoundsAndBrowser()
    {
        await using var fixture = new PostgresFixture(); await fixture.InitializeAsync(); await fixture.ApplyAuditSchemaAsync(Ct);
        await using var connection = await fixture.OpenControlConnectionAsync(Ct);
        async Task Sql(string sql) { await using var command = new NpgsqlCommand(sql, connection); await command.ExecuteNonQueryAsync(Ct); }
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,is_active BOOLEAN DEFAULT TRUE,industry TEXT,contact_person TEXT,email TEXT,phone TEXT,created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER,selected_tenant_id INTEGER,context_version BIGINT NOT NULL DEFAULT 0);
            CREATE TABLE public.master_columns(id INTEGER PRIMARY KEY,column_name TEXT,data_type TEXT,is_active BOOLEAN DEFAULT TRUE,category TEXT DEFAULT 'General',notes TEXT);
            CREATE TABLE public.templates(id INTEGER PRIMARY KEY,template_name TEXT,created_by TEXT DEFAULT 'Admin',created_date TIMESTAMPTZ DEFAULT now(),is_active BOOLEAN DEFAULT TRUE,is_system BOOLEAN DEFAULT FALSE,has_subtotals_by_default BOOLEAN DEFAULT TRUE,data_purpose TEXT DEFAULT 'standard',ledger_columns TEXT DEFAULT '{}',rate_source TEXT);
            CREATE TABLE public.template_column_mappings(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id),sort_order INTEGER,reconciliation_role TEXT,compare_for_changes BOOLEAN,comparison_review_required BOOLEAN DEFAULT FALSE);
            CREATE TABLE public.uploaded_files(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,status TEXT,ledger_account_code TEXT,ledger_account_name TEXT);
            CREATE TABLE public.client_template_upload_frequency(client_id INTEGER,template_id INTEGER,frequency TEXT);
            CREATE TABLE public.client_data_history(id INTEGER PRIMARY KEY,client_id INTEGER,row_data JSONB);
            INSERT INTO public.users(id,username,password_hash,role) VALUES(1,'owner','unused','admin'),(2,'other','unused','admin'),(3,'auditor','unused','user'),(4,'unassigned','unused','user');
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(20,'B','Beta');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0),(repeat('d',64),4,now()+interval '1 hour',10,101,0);
            INSERT INTO public.master_columns(id,column_name,data_type) VALUES(1,'col_1','Text'),(2,'col_2','Date'),(3,'col_3','Numeric'),(4,'col_4','Text'),(5,'col_5','Text');
            INSERT INTO public.templates(id,template_name,data_purpose) VALUES(1,'native_1','standard'),(2,'native_2','account_ledger'),(3,'native_3','standard'),(4,'native_4','standard'),(5,'native_5','standard'),(6,'foreign','standard');
            """);
        await fixture.ApplyTenantFoundationAsync(Ct); await fixture.ApplyEngagementFoundationAsync(Ct); await fixture.ApplyTenantUserRolesAsync(Ct);
        await fixture.ApplyTenantColumnsAsync(Ct); await fixture.ApplyTenantTemplatesAsync(Ct); await fixture.ApplyTenantSettingsAsync(Ct);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member'),(101,4,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Document','native'),(2,101,'Date','native'),(3,101,'Amount','native'),(4,101,'Memo','native'),(5,101,'Balance','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Transactions','native'),(2,101,'Ledger','native'),(3,101,'Empty','native'),(4,101,'Invalid','native'),(5,101,'Large','native'),(6,102,'Foreign secret','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key)
                SELECT t,c,c-1,CASE WHEN c=1 THEN 'document_id' WHEN c=2 THEN 'document_date' WHEN c=3 AND t<>3 THEN 'footing' ELSE NULL END,101,'col_'||c FROM generate_series(1,5) t CROSS JOIN generate_series(1,5) c;
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            INSERT INTO public.platform_tenant_holidays(tenant_id,holiday_date,label,repeats_yearly) VALUES(101,'2025-04-04','Repeat',TRUE),(101,'2026-04-05','Sunday holiday',FALSE),(102,'2026-04-06','Foreign secret holiday',FALSE);
            INSERT INTO public.client_template_upload_frequency VALUES(10,1,'Monthly'),(10,2,'Weekly');
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER,ADD COLUMN extract_date DATE,ADD COLUMN extract_amount REAL,
                ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE,ADD COLUMN change_detection_status TEXT DEFAULT 'unchanged',ADD COLUMN upload_id INTEGER,
                ADD COLUMN source_order_upload_id INTEGER,ADD COLUMN source_row_number INTEGER,ADD COLUMN document_group_id TEXT,ADD COLUMN data_quality_flag TEXT,ADD COLUMN rate_source TEXT;
            INSERT INTO public.uploaded_files VALUES(11,10,1,'Imported',NULL,NULL),(21,10,2,'Imported','001','Old name'),(22,10,2,'Imported','001','Latest name'),(23,10,2,'Imported','0011','Other account');
            INSERT INTO public.client_data(id,client_id,template_id,row_data,extract_date,extract_amount,upload_id,source_order_upload_id,source_row_number,document_group_id,data_quality_flag)
            OVERRIDING SYSTEM VALUE VALUES
            (101,10,1,'{"col_1":"V1","col_2":"2026-04-03","col_3":9007199254740993.01,"col_4":"O''Reilly <safe>","col_5":"001 DR"}','2026-04-03',10,11,11,2,'V1',NULL),
            (102,10,1,'{"col_1":"V1","col_2":"2026-04-03","col_3":0.09,"col_4":"O''Reilly <safe>","col_5":"001 DR"}','2026-04-03',0.09,11,11,3,'V1',NULL),
            (103,10,1,'{"col_1":"V2","col_2":"2026-04-05","col_3":-1.10,"col_4":"Sunday"}','2026-04-05',-1.1,11,11,4,'V2',NULL),
            (104,10,1,'{"col_1":" ","col_2":"2026-04-06","col_3":0,"col_4":null}','2026-04-06',0,11,11,5,'blank','incomplete'),
            (107,10,1,'{"col_1":"Undated","col_3":4}',NULL,4,11,11,6,'Undated',NULL),
            (201,10,2,'{"col_1":"L","col_3":2,"col_5":"100 DR"}','2026-04-03',2,21,21,9,'L',NULL),
            (202,10,2,'{"col_1":"L","col_3":2,"col_5":"102 DR"}','2026-04-03',2,22,22,2,'L',NULL),
            (203,10,2,'{"col_1":"M","col_3":9}','2026-04-03',9,23,23,1,'M',NULL),
            (401,10,4,'{"col_1":"Bad","col_3":"invalid"}','2026-04-03',0,11,11,1,'Bad',NULL),
            (901,20,6,'{"sentinel":"unchanged"}','2026-04-03',0,NULL,NULL,NULL,NULL,NULL);
            SELECT setval(pg_get_serial_sequence('public.client_data','id'),1000);
            INSERT INTO public.client_data(client_id,template_id,row_data,extract_date,is_deleted,change_detection_status) VALUES(10,1,'{"col_1":"Deleted","col_3":999}','2026-04-03',FALSE,'deleted'),(10,1,'{"col_1":"Soft","col_3":999}','2026-04-03',TRUE,'unchanged');
            INSERT INTO public.client_data(client_id,template_id,row_data,extract_date,extract_amount,source_order_upload_id,source_row_number)
                SELECT 10,5,jsonb_build_object('col_1','Repeated','col_3',0.01),'2026-04-03'::date,0.01,100,n FROM generate_series(1,10000) n;
            INSERT INTO public.client_data_history VALUES(1,10,'{"prior":"preserved"}');
            """);
        await using var app = Host(fixture); app.UseKestrel(server => server.Listen(IPAddress.Loopback, 0));
        string address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin = address;
        var sessions = app.Services.GetRequiredService<PostgresSessionStore>();
        var actor = (await sessions.FindAsync(new string('a',64), Ct))!;
        var service = app.Services.GetRequiredService<DataBankService>();
        async Task<JsonElement> Read(DataBankQuery query)
        { var result = await service.ReadAsync(actor, query, Ct); Assert.Equal("success", result.Status); return JsonSerializer.SerializeToElement(result.Value); }
        DataBankQuery Query(int template = 1) => new("preview",10,template,"2026-04-01","2026-04-30",Limit:2,Metrics:true);
        async Task<string> Snapshot()
        {
            await using var command = new NpgsqlCommand("SELECT (SELECT jsonb_agg(to_jsonb(c) ORDER BY id)::text FROM public.client_data c)||'/'||(SELECT jsonb_agg(to_jsonb(h) ORDER BY id)::text FROM public.client_data_history h)", connection);
            return (string)(await command.ExecuteScalarAsync(Ct))!;
        }
        string before = await Snapshot();
        var page = await Read(Query());
        Assert.Equal(4,page.GetProperty("total_rows").GetInt32()); Assert.Equal(101,page.GetProperty("rows")[0].GetProperty("id").GetInt32());
        Assert.Equal(JsonValueKind.Number,page.GetProperty("rows")[0].GetProperty("row_data").GetProperty("Amount").ValueKind);
        Assert.Equal("9007199254740993.01",page.GetProperty("rows")[0].GetProperty("numeric_text").GetProperty("Amount").GetString());
        Assert.Equal("9007199254740992.00",page.GetProperty("preview_metrics").GetProperty("footing_totals").GetProperty("Amount").GetString());
        Assert.Equal(2,page.GetProperty("preview_metrics").GetProperty("document_count").GetInt32());
        var queries = new[] { Query(), Query() with {Page=2}, Query() with {Search="O'Reilly"}, Query() with {Sunday=true}, Query() with {Quality="incomplete"},
            Query() with {Quality="complete", Minimum=0,Maximum=1}, Query(3),Query(4),Query(5),Query(2) with {Account="001"}, Query() with {Start="2026-05-01",End="2026-05-31"} };
        var expected = new List<object>();
        foreach(var query in queries) {var timer=Stopwatch.StartNew();var value=await Read(query); expected.Add(new {query,value,elapsed_ms=timer.Elapsed.TotalMilliseconds});}
        Assert.Equal("unavailable",(await Read(Query(4))).GetProperty("preview_metrics").GetProperty("status").GetString());
        var ledger = await Read(Query(2) with {Account="001"}); Assert.Equal(2,ledger.GetProperty("total_rows").GetInt32());
        Assert.Equal(201,ledger.GetProperty("rows")[0].GetProperty("id").GetInt32());
        Assert.Equal("invalid",(await service.ReadAsync(actor,Query(2),Ct)).Status);
        var calendar = await Read(new("calendar",10,Year:2026,Month:4,TemplateIds:[1]));
        var days=calendar.GetProperty("templates")[0].GetProperty("days");
        Assert.Equal("holiday",days[3].GetProperty("status").GetString()); Assert.Equal("present",days[4].GetProperty("status").GetString());
        Assert.Equal("present",days[5].GetProperty("status").GetString());
        var emptyCalendar=await Read(new("calendar",10,Year:2026,Month:4,TemplateIds:[3]));
        Assert.Equal("missing",emptyCalendar.GetProperty("templates")[0].GetProperty("days")[5].GetProperty("status").GetString());
        var availability=await Read(new("availability",10,Start:"2026-04-01",End:"2026-04-30"));
        Assert.Equal(5,availability.GetArrayLength()); Assert.DoesNotContain("Foreign",availability.GetRawText());
        var metric=await Read(new("metrics",10)); Assert.Equal(5,metric.GetProperty("templates").GetArrayLength());
        var documentCounts=metric.GetProperty("templates").EnumerateArray()
            .ToDictionary(row=>row.GetProperty("template_id").GetInt32(),row=>row.GetProperty("document_count").GetInt64());
        Assert.Equal(3L,documentCounts[1]); Assert.Equal(2L,documentCounts[2]); Assert.Equal(0L,documentCounts[3]);
        Assert.Equal(1L,documentCounts[4]); Assert.Equal(1L,documentCounts[5]);
        foreach (var bad in new[] {Query() with {Page=int.MaxValue},Query() with {Limit=201},Query() with {Minimum=double.NaN},Query() with {Start="bad"},Query() with {Search=new string('x',513)}})
            Assert.Equal("invalid",(await service.ReadAsync(actor,bad,Ct)).Status);
        Assert.Equal("not_found",(await service.ReadAsync(actor,Query(6),Ct)).Status);
        var staff=(await sessions.FindAsync(new string('c',64),Ct))!;
        Assert.Equal("success",(await service.ReadAsync(staff,Query(),Ct)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3");
        Assert.Equal("forbidden",(await service.ReadAsync(staff,Query(),Ct)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE user_id=3");
        Assert.Equal("forbidden",(await service.ReadAsync((await sessions.FindAsync(new string('d',64),Ct))!,Query(),Ct)).Status);
        Assert.Equal("forbidden",(await service.ReadAsync((await sessions.FindAsync(new string('b',64),Ct))!,Query(),Ct)).Status);
        // Hold the data read after its entry authorization, revoke access, then
        // release it. The final fresh authorization must discard the old snapshot.
        await using (var blocker=await fixture.OpenControlConnectionAsync(Ct))
        {
            await using var transaction=await blocker.BeginTransactionAsync(Ct);
            await using(var command=new NpgsqlCommand("LOCK TABLE public.client_data IN ACCESS EXCLUSIVE MODE",blocker,transaction)) await command.ExecuteNonQueryAsync(Ct);
            var reading=service.ReadAsync(staff,Query(),Ct);
            var second=service.ReadAsync(actor,Query(),Ct);
            var watch=Stopwatch.StartNew(); bool waiting=false;
            while(watch.Elapsed<TimeSpan.FromSeconds(5))
            {
                await using var check=new NpgsqlCommand("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'",connection);
                if((long)(await check.ExecuteScalarAsync(Ct))!>=2) {waiting=true;break;}
                await Task.Delay(20,Ct);
            }
            Assert.True(waiting,"Both bounded reads should reach the owned table lock.");
            Assert.Equal("busy",(await service.ReadAsync(actor,Query(),Ct)).Status);
            await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3");
            await transaction.RollbackAsync(Ct);
            Assert.Equal("forbidden",(await reading).Status); Assert.Equal("success",(await second).Status);
            await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE user_id=3");
        }
        await Sql("UPDATE public.sessions SET context_version=1 WHERE user_id=1");
        Assert.Equal("forbidden",(await service.ReadAsync(actor,Query(),Ct)).Status);
        await Sql("UPDATE public.sessions SET context_version=0 WHERE user_id=1");
        var readSnapshot=app.Services.GetRequiredService<PostgresReadSnapshot>();
        var denied=await Assert.ThrowsAsync<PostgresException>(()=>readSnapshot.ExecuteAsync(async(db,token)=>{await using var command=db.CreateCommand("UPDATE public.client_data SET extract_amount=0");return await command.ExecuteNonQueryAsync(token);},Ct));
        Assert.Equal("25006",denied.SqlState);
        var options=app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value; options.MaximumResponseBytes=4096;
        Assert.Equal("too_large",(await service.ReadAsync(actor,Query(5) with {Limit=200},Ct)).Status); options.MaximumResponseBytes=1048576;
        await using (var blocker=await fixture.OpenControlConnectionAsync(Ct))
        {
            await using var transaction=await blocker.BeginTransactionAsync(Ct);
            await using(var command=new NpgsqlCommand("LOCK TABLE public.client_data IN ACCESS EXCLUSIVE MODE",blocker,transaction)) await command.ExecuteNonQueryAsync(Ct);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Ct);cancelled.CancelAfter(200);
            await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>service.ReadAsync(actor,Query(),cancelled.Token));
            options.TimeoutSeconds=1;
            Assert.Equal("timeout",(await service.ReadAsync(actor,Query(),Ct)).Status);options.TimeoutSeconds=30;
            await transaction.RollbackAsync(Ct);
        }
        Assert.Equal("success",(await service.ReadAsync(actor,Query(),Ct)).Status);
        using var http=new HttpClient(new HttpClientHandler {UseCookies=false,AllowAutoRedirect=false}) {BaseAddress=new Uri(address)};
        http.DefaultRequestHeaders.Add("Cookie","session_token="+new string('a',64));
        var request=await http.GetAsync("/api/data/preview?client_id=10&template_id=1&date_start=2026-04-01&date_end=2026-04-30",Ct);
        Assert.Equal(HttpStatusCode.Conflict,request.StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(actor));
        // The native host denies unported actions without invoking coexistence.
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.PutAsync("/api/data/upload-frequency",new StringContent("{}"),Ct)).StatusCode);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/api/data/preview?client_id=10&template_id=1&date_start=2026-04-01&date_end=2026-04-30&document_id=3",Ct)).StatusCode);
        string root=Root(), directory=Path.Combine(root,".tools","p30-"+Guid.NewGuid().ToString("N")); Directory.CreateDirectory(directory);
        var dataBankPage=await http.GetAsync("/static/databank.html",Ct);
        Assert.Equal(HttpStatusCode.OK,dataBankPage.StatusCode);
        Assert.Equal("text/html",dataBankPage.Content.Headers.ContentType?.MediaType);
        options.Enabled=false;
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.GetAsync("/api/data/availability",Ct)).StatusCode);
        Assert.Equal(before,await Snapshot());
        TestContext.Current.TestOutputHelper!.WriteLine("Owned P30 artifacts: "+directory);
    }
    private static WebApplicationFactory<Program> Host(PostgresFixture fixture)=>new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>
    {
        s.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
        s.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
        s.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);s.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);s.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
        s.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);s.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);s.PostConfigure<SettingsManagementOptions>(o=>o.Enabled=true);s.PostConfigure<DataBankOptions>(o=>o.Enabled=true);
    }));
    private static string Root() {DirectoryInfo? folder=new(AppContext.BaseDirectory);while(folder is not null&&!File.Exists(Path.Combine(folder.FullName,"AuditPlatform.slnx"))) folder=folder.Parent;return folder!.FullName;}
}
