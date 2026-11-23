using System.Diagnostics;
using System.Net;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
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

public sealed class DataBankHistoryExportTests
{
    private static CancellationToken Ct => TestContext.Current.CancellationToken;
    [Fact]
    public async Task HistoryAndExportsDefaultOffAndRequireReads()
    {
        await using var app = new WebApplicationFactory<Program>();
        Assert.False(app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value.HistoryEnabled);
        Assert.False(app.Services.GetRequiredService<IOptions<DataBankExportOptions>>().Value.Enabled);
        await using var bad = new WebApplicationFactory<Program>().WithWebHostBuilder(b => b.ConfigureServices(s => s.PostConfigure<DataBankExportOptions>(o => o.Enabled = true)));
        Assert.Throws<OptionsValidationException>(() => bad.CreateClient());
    }
    [Fact]
    public async Task OwnedHistoryOracleWorkbooksAuthorizationAndBrowser()
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
            CREATE TABLE public.client_data_history(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,client_data_id INTEGER,action TEXT,changed_at TIMESTAMPTZ,changed_by TEXT,
                source_upload_id INTEGER,replacing_upload_id INTEGER,source_identity TEXT,row_data JSONB,changed_fields JSONB,ledger_account_code TEXT,ledger_account_name TEXT,import_operation_id UUID,source_metadata JSONB);
            CREATE TABLE IF NOT EXISTS public.data_audit_log(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_data_id INTEGER,"user" TEXT,action TEXT,old_row_data JSONB,new_row_data JSONB,changed_at TIMESTAMPTZ DEFAULT now());
            INSERT INTO public.users(id,username,password_hash,role) VALUES(1,'owner','unused','admin'),(2,'other','unused','admin'),(3,'auditor','unused','user'),(4,'unassigned','unused','user');
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(20,'B','Beta');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0),(repeat('d',64),4,now()+interval '1 hour',10,101,0);
            INSERT INTO public.master_columns(id,column_name,data_type) VALUES(1,'col_1','Text'),(2,'col_2','Date'),(3,'col_3','Numeric'),(4,'col_4','Text'),(5,'col_5','Text');
            INSERT INTO public.templates(id,template_name,data_purpose) VALUES(1,'native_1','standard'),(2,'native_2','account_ledger'),(3,'native_3','standard'),(4,'native_4','standard'),(5,'foreign','standard');
            """);
        await fixture.ApplyTenantFoundationAsync(Ct); await fixture.ApplyEngagementFoundationAsync(Ct); await fixture.ApplyTenantUserRolesAsync(Ct);
        await fixture.ApplyTenantColumnsAsync(Ct); await fixture.ApplyTenantTemplatesAsync(Ct); await fixture.ApplyTenantSettingsAsync(Ct);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member'),(101,4,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Document','native'),(2,101,'Date','native'),(3,101,'Amount','native'),(4,101,'Memo','native'),(5,101,'Balance','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Transactions','native'),(2,101,'Ledger','native'),(3,101,'History only','native'),(4,101,'Large','native'),(5,102,'Foreign secret','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key)
                SELECT t,c,c-1,CASE WHEN c=1 THEN 'document_id' WHEN c=2 THEN 'document_date' WHEN c=3 THEN 'footing' END,101,'col_'||c FROM generate_series(1,4) t CROSS JOIN generate_series(1,5) c;
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER,ADD COLUMN extract_date DATE,ADD COLUMN extract_amount REAL,ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE,
                ADD COLUMN change_detection_status TEXT DEFAULT 'unchanged',ADD COLUMN upload_id INTEGER,ADD COLUMN source_order_upload_id INTEGER,ADD COLUMN source_row_number INTEGER,
                ADD COLUMN document_group_id TEXT,ADD COLUMN data_quality_flag TEXT,ADD COLUMN rate_source TEXT;
            INSERT INTO public.uploaded_files VALUES(11,10,1,'Imported',NULL,NULL),(21,10,2,'Imported','001','Current account'),(40,10,1,'Imported',NULL,NULL);
            INSERT INTO public.client_data(id,client_id,template_id,row_data,extract_date,extract_amount,upload_id,source_order_upload_id,source_row_number,document_group_id)
            OVERRIDING SYSTEM VALUE VALUES
            (101,10,1,'{"col_1":"V1","col_2":"2026-04-03","col_3":9007199254740993.10,"col_4":"=literal _x0041_ <safe>","col_5":"001 DR","other":false,"blank":null}', '2026-04-03',10,11,11,2,'V1'),
            (102,10,1,'{"col_1":"AMB","col_3":12,"col_4":"Same"}','2026-04-03',12,40,40,2,'AMB'),
            (103,10,1,'{"col_1":"AMB","col_3":13,"col_4":"Same"}','2026-04-03',13,40,40,3,'AMB'),
            (104,10,1,'{"col_1":"LEG","col_3":12,"col_4":"Unique"}','2026-04-03',12,40,40,4,'LEG'),
            (105,10,1,'{"col_1":"TWIN","col_3":12}','2026-04-03',12,40,40,5,'TWIN'),
            (201,10,2,'{"col_1":"L","col_3":2,"col_5":"100 DR"}','2026-04-03',2,21,21,2,'L'),
            (901,20,5,'{"sentinel":"unchanged"}','2026-04-03',0,NULL,NULL,NULL,NULL);
            SELECT setval(pg_get_serial_sequence('public.client_data','id'),1000);
            INSERT INTO public.client_data(client_id,template_id,row_data,extract_date,extract_amount,source_order_upload_id,source_row_number)
                SELECT 10,4,jsonb_build_object('col_1','V'||n,'col_3',0.01),'2026-04-03'::date,0.01,100,n FROM generate_series(1,10001) n;
            INSERT INTO public.client_data_history(id,client_id,template_id,client_data_id,action,changed_at,changed_by,source_upload_id,replacing_upload_id,row_data,changed_fields,ledger_account_code,ledger_account_name,source_metadata)
            VALUES
            (1,10,1,101,'changed','2026-04-01 01:00Z','Owner',11,40,'{"col_1":"V1","col_3":9007199254740993.01,"archived_key":"kept"}','{"col_3":{"old_value":9007199254740993.01,"new_value":9007199254740993.10}}',NULL,NULL,'{"version":1}'),
            (2,10,1,301,'deleted','2026-04-01 02:00Z','Owner',11,40,'{"col_1":"DELETED","col_3":50}',NULL,NULL,NULL,NULL),
            (3,10,1,302,'deleted','2026-04-01 03:00Z','Owner',11,40,'{"col_1":"AMB","col_3":10,"col_4":"Same"}',NULL,NULL,NULL,NULL),
            (4,10,1,303,'deleted','2026-04-01 04:00Z','Owner',11,40,'{"col_1":"LEG","col_3":10,"col_4":"Unique"}',NULL,NULL,NULL,NULL),
            (5,10,1,101,'changed','2026-04-01 05:00Z','Earlier',11,11,'{"col_1":"V1","col_3":9007199254740993.00}',NULL,NULL,NULL,NULL),
            (6,10,2,401,'deleted','2026-04-01 06:00Z','Owner',999,NULL,'{"col_1":"OLD","col_3":1,"col_5":"001 DR"}',NULL,'000OLD','Archived account',NULL),
            (7,10,2,201,'changed','2026-04-01 07:00Z','Owner',21,21,'{"col_1":"L","col_3":1}','{"col_3":{"old_value":1,"new_value":2}}','001','Current account',NULL),
            (8,10,3,999,'deleted','2026-04-01 08:00Z','Owner',NULL,NULL,'{"col_1":"ONLY","col_3":0,"col_4":"=literal _x0041_ <safe>"}',NULL,NULL,NULL,NULL),
            (9,10,1,304,'deleted','2026-04-01 09:00Z','Owner',11,40,'{"col_1":"TWIN","col_3":10}',NULL,NULL,NULL,NULL),
            (10,10,1,305,'deleted','2026-04-01 10:00Z','Owner',11,40,'{"col_1":"TWIN","col_3":11}',NULL,NULL,NULL,NULL);
            INSERT INTO public.data_audit_log(client_data_id,"user",action,old_row_data,new_row_data,changed_at) VALUES(101,'Legacy','UPDATE','{"col_3":1}','{"col_3":2}','2026-03-01'),(101,'Legacy','INSERT',NULL,'{"col_1":"V1"}','2026-02-01');
            """);
        await using var app = Host(fixture); app.UseKestrel(server => server.Listen(IPAddress.Loopback,0));
        string address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single(); app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin = address;
        using var http=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
        var sessions=app.Services.GetRequiredService<PostgresSessionStore>(); var actor=(await sessions.FindAsync(new string('a',64),Ct))!; var staff=(await sessions.FindAsync(new string('c',64),Ct))!;
        http.DefaultRequestHeaders.Add("Cookie","session_token="+actor.Token);
        http.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(actor));
        var reads=app.Services.GetRequiredService<DataBankService>(); var exports=app.Services.GetRequiredService<DataBankExportService>();
        var flags=app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value; var bounds=app.Services.GetRequiredService<IOptions<DataBankExportOptions>>().Value;
        DataBankQuery History(int template=1)=>new("history",10,template,Limit:2);
        DataBankQuery Preview(int template=1)=>new("preview",10,template,"2026-04-01","2026-04-30",Limit:200);
        Assert.Equal("unavailable",(await reads.ReadAsync(actor,History(),Ct)).Status); flags.HistoryEnabled=true;
        Assert.Equal("unavailable",(await exports.DownloadAsync(actor,Preview(),false,Ct)).Status); bounds.Enabled=true;
        async Task<JsonElement> Read(DataBankQuery query) {var result=await reads.ReadAsync(actor,query,Ct);Assert.Equal("success",result.Status);return JsonSerializer.SerializeToElement(result.Value);}
        async Task<string> Snapshot() {await using var c=new NpgsqlCommand("SELECT (SELECT jsonb_agg(to_jsonb(c) ORDER BY id)::text FROM public.client_data c)||'/'||(SELECT jsonb_agg(to_jsonb(h) ORDER BY id)::text FROM public.client_data_history h)",connection);return (string)(await c.ExecuteScalarAsync(Ct))!;}
        string before=await Snapshot(); var cases=new List<object>();
        foreach(var query in new[]{History(),History() with {Page=2},History() with {Action="changed",Limit=100},History() with {Action="deleted",Limit=100},History(2) with {Account="000OLD"},History(3)}) cases.Add(new {query,value=await Read(query)});
        Assert.Equal(3,(await Read(History() with {Action="changed"})).GetProperty("total").GetInt32());
        Assert.Equal(4,(await Read(History() with {Action="deleted"})).GetProperty("total").GetInt32());
        Assert.Equal("invalid",(await reads.ReadAsync(actor,History() with {Limit=101},Ct)).Status);
        Assert.Equal("invalid",(await reads.ReadAsync(actor,History(2) with {Account="unknown"},Ct)).Status);
        await Sql("UPDATE public.platform_tenant_columns SET display_name='Renamed amount <safe>' WHERE tenant_id=101 AND column_id=3");
        var renamed=await Read(History() with {Action="changed",Limit=100});
        var changed=renamed.GetProperty("rows").EnumerateArray().Single(r=>r.GetProperty("id").GetInt32()==1);
        Assert.Equal("9007199254740993.01",changed.GetProperty("changed_fields").GetProperty("Renamed amount <safe>").GetProperty("old_value").GetRawText());
        Assert.Contains(renamed.GetProperty("columns").EnumerateArray(),c=>c.GetProperty("column_name").GetString()=="archived_key");
        await Sql("UPDATE public.platform_tenant_columns SET display_name='Amount' WHERE tenant_id=101 AND column_id=3");
        foreach(string source in new[]{"uploads","legacy"}) Assert.Equal(2,(await Read(new("revisions",10,1,RowId:101,RevisionSource:source))).GetProperty("total").GetInt32());
        Assert.Equal("not_found",(await reads.ReadAsync(actor,new("revisions",10,1,RowId:901),Ct)).Status);
        Assert.Contains("000OLD",(await Read(new("history-accounts",10,2))).GetRawText());
        Assert.Equal("success",(await exports.DownloadAsync(staff,Preview(),false,Ct)).Status);
        Assert.Equal("forbidden",(await exports.DownloadAsync(staff,new("backup",10,1),true,Ct)).Status);
        Assert.Equal("forbidden",(await reads.ReadAsync((await sessions.FindAsync(new string('b',64),Ct))!,History(),Ct)).Status);
        Assert.Equal("forbidden",(await reads.ReadAsync((await sessions.FindAsync(new string('d',64),Ct))!,History(),Ct)).Status);
        string root=Root(), directory=Path.Combine(root,".tools","p31-"+Guid.NewGuid().ToString("N")); Directory.CreateDirectory(directory);
        var timer=Stopwatch.StartNew();var large=await exports.DownloadAsync(actor,Preview(4),false,Ct);Assert.Equal("success",large.Status);double milliseconds=timer.Elapsed.TotalMilliseconds;
        await File.WriteAllBytesAsync(Path.Combine(directory,"large.xlsx"),large.Bytes!,Ct);
        foreach(int template in new[]{1,2,3}) {var result=await exports.DownloadAsync(actor,new("backup",10,template),true,Ct);Assert.Equal("success",result.Status);await File.WriteAllBytesAsync(Path.Combine(directory,$"backup-{template}.xlsx"),result.Bytes!,Ct);}
        bounds.MaximumRows=2;Assert.Equal("too_large",(await exports.DownloadAsync(actor,Preview(),false,Ct)).Status);bounds.MaximumRows=100000;
        bounds.MaximumInputBytes=4096;Assert.Equal("too_large",(await exports.DownloadAsync(actor,Preview(4),false,Ct)).Status);bounds.MaximumInputBytes=33554432;
        bounds.MaximumExpandedBytes=4096;Assert.Equal("too_large",(await exports.DownloadAsync(actor,Preview(4),false,Ct)).Status);bounds.MaximumExpandedBytes=67108864;
        bounds.MaximumWorkbookBytes=4096;Assert.Equal("too_large",(await exports.DownloadAsync(actor,Preview(4),false,Ct)).Status);bounds.MaximumWorkbookBytes=8388608;
        await Sql("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_4}',to_jsonb(repeat('x',32768))) WHERE id=101");
        Assert.Equal("too_large",(await exports.DownloadAsync(actor,Preview(),false,Ct)).Status);
        await Sql("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_4}','\"=literal _x0041_ <safe>\"') WHERE id=101");
        await using(var blocker=await fixture.OpenControlConnectionAsync(Ct))
        {
            await using var transaction=await blocker.BeginTransactionAsync(Ct);
            await using(var command=new NpgsqlCommand("LOCK TABLE public.client_data IN ACCESS EXCLUSIVE MODE",blocker,transaction)) await command.ExecuteNonQueryAsync(Ct);
            var pending=exports.DownloadAsync(staff,Preview(),false,Ct);await Locked(connection);
            Assert.Equal("busy",(await exports.DownloadAsync(actor,Preview(),false,Ct)).Status);
            await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3");await transaction.RollbackAsync(Ct);
            Assert.Equal("forbidden",(await pending).Status);await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE user_id=3");
        }
        await using(var blocker=await fixture.OpenControlConnectionAsync(Ct))
        {
            await using var transaction=await blocker.BeginTransactionAsync(Ct);
            await using(var command=new NpgsqlCommand("LOCK TABLE public.client_data_history IN ACCESS EXCLUSIVE MODE",blocker,transaction)) await command.ExecuteNonQueryAsync(Ct);
            var pending=exports.DownloadAsync(actor,new("backup",10,1),true,Ct);await Locked(connection);
            await using(var change=new NpgsqlCommand("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_3}','999') WHERE id=101; INSERT INTO public.client_data_history(id,client_id,template_id,client_data_id,action,changed_at,row_data) VALUES(99,10,1,101,'changed',now(),'{\"col_1\":\"CONCURRENT\"}')",blocker,transaction)) await change.ExecuteNonQueryAsync(Ct);
            await transaction.CommitAsync(Ct);var stable=await pending;Assert.Equal("success",stable.Status);await File.WriteAllBytesAsync(Path.Combine(directory,"consistent.xlsx"),stable.Bytes!,Ct);
            await Sql("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_3}','9007199254740993.10') WHERE id=101; DELETE FROM public.client_data_history WHERE id=99");
        }
        await using(var blocker=await fixture.OpenControlConnectionAsync(Ct))
        {
            await using var transaction=await blocker.BeginTransactionAsync(Ct);await using(var command=new NpgsqlCommand("LOCK TABLE public.client_data IN ACCESS EXCLUSIVE MODE",blocker,transaction)) await command.ExecuteNonQueryAsync(Ct);
            using var cancel=CancellationTokenSource.CreateLinkedTokenSource(Ct);cancel.CancelAfter(200);await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>exports.DownloadAsync(actor,Preview(),false,cancel.Token));
            bounds.TimeoutSeconds=1;Assert.Equal("timeout",(await exports.DownloadAsync(actor,Preview(),false,Ct)).Status);bounds.TimeoutSeconds=60;await transaction.RollbackAsync(Ct);
        }
        var dataBankPage=await http.GetAsync("/static/databank.html",Ct);
        Assert.Equal(HttpStatusCode.OK,dataBankPage.StatusCode);
        Assert.Equal("text/html",dataBankPage.Content.Headers.ContentType?.MediaType);
        flags.HistoryEnabled=false;bounds.Enabled=false;
        var disabled=await exports.DownloadAsync(actor,Preview(),false,Ct);
        Assert.Equal("unavailable",disabled.Status);Assert.Equal("databank_exports_disabled",disabled.Code);
        Assert.Equal(before,await Snapshot());TestContext.Current.TestOutputHelper!.WriteLine("P31 owned evidence: "+directory);
    }
    private static async Task Locked(NpgsqlConnection connection)
    {
        var watch=Stopwatch.StartNew();while(watch.Elapsed<TimeSpan.FromSeconds(5)) {await using var c=new NpgsqlCommand("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'",connection);if((long)(await c.ExecuteScalarAsync(Ct))!>0)return;await Task.Delay(20,Ct);}Assert.Fail("Owned read did not reach the table lock.");
    }
    private static WebApplicationFactory<Program> Host(PostgresFixture f)=>new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>
    {
        s.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=f.ConnectionString;});s.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
        s.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);s.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);s.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
        s.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);s.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);s.PostConfigure<SettingsManagementOptions>(o=>o.Enabled=true);s.PostConfigure<DataBankOptions>(o=>o.Enabled=true);
    }));
    private static string Root(){DirectoryInfo? f=new(AppContext.BaseDirectory);while(f is not null&&!File.Exists(Path.Combine(f.FullName,"AuditPlatform.slnx"))) f=f.Parent;return f!.FullName;}
}
