using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Collections.Concurrent;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Uploads;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

// Full browser acceptance uses only this fixture's SCRAM-protected loopback DB,
// local private-object simulator and generated non-client workbooks.
public sealed class UploadAcceptanceTests
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    [Fact]
    public async Task DisabledUiRequiresJobsAndShell()
    {
        await using var app = new WebApplicationFactory<Program>();
        Assert.False(app.Services.GetRequiredService<IOptions<UploadUiOptions>>().Value.Enabled);
        await using var invalid = new WebApplicationFactory<Program>().WithWebHostBuilder(b => b.ConfigureServices(s =>
            s.PostConfigure<UploadUiOptions>(o => o.Enabled = true)));
        Assert.Throws<OptionsValidationException>(() => invalid.CreateClient());
    }
    [Fact]
    public async Task NativeBrowserAcceptanceAndExactStoredResults()
    {
        await using var fixture = new PostgresFixture(); await fixture.InitializeAsync(); await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string sql) { await using var command = new NpgsqlCommand(sql,connection); await command.ExecuteNonQueryAsync(Token); }
        await Sql("""
CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,is_active BOOLEAN DEFAULT TRUE);
CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER,selected_tenant_id INTEGER,context_version BIGINT NOT NULL DEFAULT 0);
CREATE TABLE public.master_columns(id INTEGER PRIMARY KEY,column_name TEXT,data_type TEXT,is_active BOOLEAN DEFAULT TRUE,category TEXT,notes TEXT);
CREATE TABLE public.templates(id INTEGER PRIMARY KEY,template_name TEXT,created_by TEXT DEFAULT 'Admin',created_date TIMESTAMPTZ DEFAULT now(),is_active BOOLEAN DEFAULT TRUE,is_system BOOLEAN DEFAULT FALSE,has_subtotals_by_default BOOLEAN DEFAULT FALSE,data_purpose TEXT DEFAULT 'standard',ledger_columns TEXT DEFAULT '{}',rate_source TEXT);
CREATE TABLE public.template_column_mappings(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id),sort_order INTEGER,reconciliation_role TEXT,compare_for_changes BOOLEAN,comparison_review_required BOOLEAN DEFAULT FALSE);
CREATE TABLE public.uploaded_files(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,file_name TEXT,file_path TEXT,file_type TEXT,record_count INTEGER,status TEXT,uploaded_by TEXT,client_id INTEGER,template_id INTEGER);
CREATE TABLE public.upload_validation_plans(plan_id TEXT PRIMARY KEY,file_id INTEGER REFERENCES public.uploaded_files(id),client_id INTEGER,template_id INTEGER,
    file_sha256 TEXT,request_signature TEXT,plan_json JSONB,status TEXT CHECK(status IN('ready','applied','expired','superseded')),created_at TIMESTAMPTZ DEFAULT now(),expires_at TIMESTAMPTZ,applied_at TIMESTAMPTZ);
INSERT INTO public.users VALUES(1,'owner','unused',TRUE,'admin',NULL),(2,'other','unused',TRUE,'user',NULL),(3,'auditor','unused',TRUE,'user',NULL);
INSERT INTO public.clients VALUES(10,'A','Alpha',TRUE),(20,'B','Beta',TRUE);
INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0);
INSERT INTO public.master_columns VALUES(1,'col_1','Text',TRUE,'General',NULL),(2,'col_2','Date',TRUE,'General',NULL),(3,'col_3','Numeric',TRUE,'General',NULL),(4,'col_4','Text',TRUE,'General',NULL),(5,'col_5','Text',TRUE,'General',NULL),(6,'col_6','Text',TRUE,'General',NULL);
INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture');
""");
        await fixture.ApplyTenantFoundationAsync(Token); await fixture.ApplyEngagementFoundationAsync(Token); await fixture.ApplyTenantUserRolesAsync(Token);
        await fixture.ApplyTenantColumnsAsync(Token); await fixture.ApplyTenantTemplatesAsync(Token); await fixture.ApplySavedMappingsAsync(Token); await fixture.ApplyUploadStagingAsync(Token);
        await Sql("""
INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Document','native'),(2,101,'Date','native'),(3,101,'Amount','native'),(4,101,'Memo','native'),(5,101,'Balance','native'),(6,101,'Extra','native');
INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Fixture','native');
INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key) VALUES(1,1,0,'document_id',101,'col_1'),(1,2,1,'document_date',101,'col_2'),(1,3,2,'footing',101,'col_3'),(1,4,3,NULL,101,'col_4'),(1,5,4,NULL,101,'col_5'),(1,6,5,NULL,101,'col_6');
INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
INSERT INTO public.client_data(client_id,row_data) VALUES(20,'{"sentinel":"unchanged"}');
""");
        await Sql("""
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS template_id INTEGER;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS document_group_id TEXT;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS source_identity TEXT;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS source_row_number INTEGER;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS source_order_upload_id INTEGER;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS extract_date DATE;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS change_detection_status TEXT DEFAULT 'unchanged';
ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS upload_id INTEGER;
ALTER TABLE public.uploaded_files ADD COLUMN IF NOT EXISTS ledger_account_code TEXT;
UPDATE public.template_column_mappings SET compare_for_changes=FALSE WHERE column_id=5;
INSERT INTO public.client_data(id,client_id,template_id,row_data,document_group_id,source_identity,source_row_number,source_order_upload_id,extract_date)
OVERRIDING SYSTEM VALUE VALUES
(101,10,1,'{"col_1":"V1","col_2":"03/04/2026","col_3":100,"col_4":"Office  supplies","col_5":"100 DR","col_6":null}','1-V1','old-101',2,11,'2026-04-03'),
(102,10,1,'{"col_1":"V1","col_2":"03/04/2026","col_3":100,"col_4":"Office  supplies","col_5":"100 DR","col_6":null}','1-V1','old-102',3,11,'2026-04-03'),
(103,10,1,'{"col_1":"V2","col_2":"08/04/2026","col_3":50,"col_4":"Other","col_5":"50 DR","col_6":null}','1-V2','old-103',4,11,'2026-04-08');
""");
        await fixture.ApplyUploadValidationPlansAsync(Token);
        await Sql("""
ALTER TABLE public.client_data ADD COLUMN created_at TIMESTAMPTZ DEFAULT '2026-01-01';
ALTER TABLE public.client_data ADD COLUMN created_by TEXT;
ALTER TABLE public.client_data ADD COLUMN source_fingerprint TEXT;
ALTER TABLE public.client_data ADD COLUMN extract_amount REAL;
ALTER TABLE public.client_data ADD COLUMN derived_account_code TEXT;
CREATE UNIQUE INDEX idx_client_data_active_identity_unique ON public.client_data(client_id,template_id,source_identity)
    WHERE is_deleted=FALSE AND change_detection_status<>'deleted' AND source_identity IS NOT NULL AND source_identity<>'';
SELECT setval(pg_get_serial_sequence('public.client_data','id'),1000);
CREATE TABLE public.client_data_history(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id INTEGER,template_id INTEGER,client_data_id INTEGER,
    action TEXT,changed_at TIMESTAMPTZ DEFAULT now(),changed_by TEXT,source_upload_id INTEGER,replacing_upload_id INTEGER,source_identity TEXT,row_data JSONB,changed_fields JSONB);
CREATE TABLE public.verification_details(id INTEGER PRIMARY KEY,client_data_id INTEGER,validation_status TEXT);
CREATE TABLE public.voucher_line_documents(client_data_id INTEGER);
CREATE TABLE public.voucher_line_physical_bill_items(source_document_row_id INTEGER);
CREATE TABLE public.cashflow_prepared_lines(source_client_data_id INTEGER);
""");
        await fixture.ApplyUploadImportsAsync(Token); await fixture.ApplyUploadDerivedFieldsAsync(Token);
        await fixture.ApplyTenantSettingsAsync(Token); await fixture.ApplyUploadImportReceiptsAsync(Token); await fixture.ApplyUploadJobsAsync(Token);
        await Sql("""
            DELETE FROM public.client_data WHERE client_id=10;
            ALTER TABLE public.clients ADD COLUMN industry TEXT, ADD COLUMN contact_person TEXT,
                ADD COLUMN email TEXT, ADD COLUMN phone TEXT, ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
            ALTER TABLE public.client_data ADD COLUMN rate_source TEXT, ADD COLUMN derived_voucher_id TEXT,
                ADD COLUMN derived_account_name TEXT, ADD COLUMN derived_transaction_date DATE,
                ADD COLUMN derived_transaction_month TEXT, ADD COLUMN derived_transaction_fortnight TEXT,
                ADD COLUMN derived_debit DOUBLE PRECISION DEFAULT 0, ADD COLUMN derived_credit DOUBLE PRECISION DEFAULT 0,
                ADD COLUMN derived_treasury_type TEXT, ADD COLUMN derived_treasury_account_code TEXT,
                ADD COLUMN derived_classification_status TEXT, ADD COLUMN derived_version INTEGER DEFAULT 1;
            CREATE TABLE IF NOT EXISTS public.settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO public.platform_template_upload_bindings(tenant_id,template_id,bindings)
                VALUES(101,1,'{"amount":3,"description":4}');
            """);
        DirectoryInfo? root = new(AppContext.BaseDirectory);
        while(root is not null && !File.Exists(Path.Combine(root.FullName,"AuditPlatform.slnx"))) root=root.Parent;
        Assert.NotNull(root);
        string directory=Path.Combine(root.FullName,".tools","p29-upload-"+Guid.NewGuid().ToString("N"));
        await Python(root.FullName,"p29_upload_oracle.py",directory);
        // Staging is outside every served application asset directory.
        string staging=Path.Combine(Path.GetTempPath(),"AuditPlatform-P29-"+Guid.NewGuid().ToString("N"));
        await using var storage=await LocalStorage.Start(directory,Token);
        await using var host=new RestartableHost(fixture,staging,storage.Url);await host.Start();
        var app=host.Current!;string address=host.Address;
        var actor=await app.Services.GetRequiredService<PostgresSessionStore>().FindAsync(new string('a',64),Token);
        await app.Services.GetRequiredService<AuditPlatform.Api.Clients.TenantClientService>().SelectionAsync(actor!,null,Token);
        // The browser script invokes only this owned host; it never starts Python's app.
        await File.WriteAllTextAsync(Path.Combine(directory,"host.json"),JsonSerializer.Serialize(new { address }),Token);
        using var checkpoints=CancellationTokenSource.CreateLinkedTokenSource(Token);
        var verification=VerifyCheckpoints(fixture,directory,host.Restart,
            ()=>host.Current!.Services.GetRequiredService<IOptions<UploadUiOptions>>().Value.Enabled=false,checkpoints.Token);
        try { await Python(root.FullName,"p29_upload_browser.py",directory); }
        catch
        {
            // Owned synthetic diagnostics make fixture/schema failures reviewable.
            var names=new List<string>();
            await using(var catalog=new NpgsqlCommand("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE '%error%'",connection))
            {await using var r=await catalog.ExecuteReaderAsync(Token);while(await r.ReadAsync(Token)) names.Add(r.GetString(0));}
            foreach(var name in names)
            {
                await using var command=new NpgsqlCommand($"SELECT to_jsonb(e)::text FROM public.{new NpgsqlCommandBuilder().QuoteIdentifier(name)} e LIMIT 3",connection);
                await using var r=await command.ExecuteReaderAsync(Token);while(await r.ReadAsync(Token)) TestContext.Current.TestOutputHelper!.WriteLine(r.GetString(0));
            }
            throw;
        }
        finally {await checkpoints.CancelAsync();await verification;}
        using var results=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(directory,"browser-results.json"),Token));
        Assert.True(results.RootElement.GetProperty("accepted").GetBoolean());
        // Browser checkpoints pause between scenarios; the parent independently
        // inspects actual canonical rows/history before allowing the next case.
        await using(var c=new NpgsqlCommand("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'",connection))
            Assert.Equal(1L,(long)(await c.ExecuteScalarAsync(Token))!);
        await using(var c=new NpgsqlCommand("SELECT count(*) FROM public.platform_upload_imports",connection))
            Assert.Equal(results.RootElement.GetProperty("imports").GetInt64(),(long)(await c.ExecuteScalarAsync(Token))!);
        using var expected=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(directory,"oracle.json"),Token));
        var large=expected.RootElement.GetProperty("large");
        await using(var c=new NpgsqlCommand("SELECT count(*),COALESCE(sum((row_data->>'col_3')::numeric),0)::text FROM public.client_data WHERE client_id=10 AND NOT is_deleted",connection))
        { await using var r=await c.ExecuteReaderAsync(Token); await r.ReadAsync(Token); Assert.Equal(large.GetProperty("rows").GetInt64(),r.GetInt64(0)); Assert.Equal(decimal.Parse(large.GetProperty("total").GetString()!,CultureInfo.InvariantCulture),decimal.Parse(r.GetString(1),CultureInfo.InvariantCulture)); }
        Assert.Equal(1L,results.RootElement.GetProperty("repeatReceipts").GetInt64());
        await File.WriteAllTextAsync(Path.Combine(directory,"host-measurement.json"),JsonSerializer.Serialize(new {
            peakHostAndHarnessWorkingSetBytes=Process.GetCurrentProcess().PeakWorkingSet64,
            note="Combined owned Kestrel/xUnit host peak; not a production memory benchmark."}),Token);
        await File.WriteAllTextAsync(Path.Combine(directory,"verification.txt"),"Owned browser/API/SQL checks passed.\n",Token);
    }
    private static async Task VerifyCheckpoints(PostgresFixture fixture,string directory,Func<Task> restart,Action disable,CancellationToken ct)
    {
        try
        {
            using var oracle=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(directory,"oracle.json"),ct));
            await using var connection=await fixture.OpenControlConnectionAsync(ct);
            long[]? originalIds=null;
            foreach(var (name,expected,history) in new[]{("original","original",0L),("repeat","original",0L),
                ("cancelled","original",0L),("resumed","original",0L),("stale","original",0L),
                ("correction","correction",1L),("partial","correction",1L),("empty","empty",4L),
                ("oversize","empty",4L),("large","large",4L),("invalid","large",4L),("disabled","large",4L)})
            {
                while(!File.Exists(Path.Combine(directory,"checkpoint-"+name)))
                {
                    if(File.Exists(Path.Combine(directory,"restart-host"))&&!File.Exists(Path.Combine(directory,"restarted-host")))
                    {await restart();await File.WriteAllTextAsync(Path.Combine(directory,"restarted-host"),"ready",ct);}
                    if(File.Exists(Path.Combine(directory,"rotate-session"))&&!File.Exists(Path.Combine(directory,"rotated-session")))
                    {
                        await using var rotate=new NpgsqlCommand("""
                            INSERT INTO public.sessions(token,user_id,expiry,selected_client_id,selected_tenant_id,context_version)
                            SELECT repeat('d',64),user_id,now()+interval '1 hour',selected_client_id,selected_tenant_id,context_version
                            FROM public.sessions WHERE token=repeat('a',64);
                            DELETE FROM public.sessions WHERE token=repeat('a',64);
                            """,connection);
                        await rotate.ExecuteNonQueryAsync(ct);await File.WriteAllTextAsync(Path.Combine(directory,"rotated-session"),"ready",ct);
                    }
                    if(File.Exists(Path.Combine(directory,"disable-ui"))&&!File.Exists(Path.Combine(directory,"disabled-ui")))
                    {disable();await File.WriteAllTextAsync(Path.Combine(directory,"disabled-ui"),"ready",ct);}
                    await Task.Delay(100,ct);
                }
                static string Normalize(JsonElement value)=>value.ValueKind==JsonValueKind.Number
                    ? decimal.Parse(value.GetRawText(),CultureInfo.InvariantCulture).ToString("G29",CultureInfo.InvariantCulture)
                    : value.ValueKind==JsonValueKind.Null?"":value.GetString()??"";
                var actual=new List<string>();var ids=new List<long>();
                await using(var command=new NpgsqlCommand("SELECT id,row_data::text FROM public.client_data WHERE client_id=10 AND template_id=1 AND NOT is_deleted ORDER BY id",connection))
                {
                    await using var reader=await command.ExecuteReaderAsync(ct);
                    while(await reader.ReadAsync(ct))
                    {
                        ids.Add(reader.GetInt32(0));using var row=JsonDocument.Parse(reader.GetString(1));
                        actual.Add(string.Join('\u001f',Enumerable.Range(1,6).Select(i=>Normalize(row.RootElement.GetProperty("col_"+i)))));
                    }
                }
                var wanted=oracle.RootElement.GetProperty(expected).GetProperty("values").EnumerateArray().Select(row=>
                    string.Join('\u001f',row.EnumerateArray().Select((cell,i)=>i==2?decimal.Parse(cell.GetString()!,CultureInfo.InvariantCulture).ToString("G29",CultureInfo.InvariantCulture):cell.GetString()??""))).ToList();
                Assert.Equal(wanted.Order(StringComparer.Ordinal),actual.Order(StringComparer.Ordinal));
                if(name=="original") originalIds=ids.ToArray();
                if(name is "repeat" or "correction" or "partial" or "cancelled" or "resumed") Assert.Equal(originalIds,ids.ToArray());
                await using(var command=new NpgsqlCommand("SELECT count(*) FROM public.client_data_history WHERE client_id=10 AND template_id=1",connection))
                    Assert.Equal(history,(long)(await command.ExecuteScalarAsync(ct))!);
                if(history>0)
                {
                    var prior=new List<string>();
                    await using(var command=new NpgsqlCommand("SELECT row_data::text FROM public.client_data_history WHERE client_id=10 AND template_id=1",connection))
                    {
                        await using var reader=await command.ExecuteReaderAsync(ct);
                        while(await reader.ReadAsync(ct))
                        {using var row=JsonDocument.Parse(reader.GetString(0));prior.Add(string.Join('\u001f',Enumerable.Range(1,6).Select(i=>Normalize(row.RootElement.GetProperty("col_"+i)))));}
                    }
                    var expectedPrior=oracle.RootElement.GetProperty("original").GetProperty("values").EnumerateArray().Skip(2).ToList();
                    if(history==4) expectedPrior.AddRange(oracle.RootElement.GetProperty("correction").GetProperty("values").EnumerateArray());
                    var normalizedPrior=expectedPrior.Select(row=>string.Join('\u001f',row.EnumerateArray().Select((cell,i)=>i==2
                        ?decimal.Parse(cell.GetString()!,CultureInfo.InvariantCulture).ToString("G29",CultureInfo.InvariantCulture):cell.GetString()??"")));
                    Assert.Equal(normalizedPrior.Order(StringComparer.Ordinal),prior.Order(StringComparer.Ordinal));
                }
                await File.WriteAllTextAsync(Path.Combine(directory,"checked-"+name),"Exact canonical multiset, retained IDs and history count passed.",ct);
            }
        }
        catch(OperationCanceledException) when(ct.IsCancellationRequested) { }
    }
    private sealed class RestartableHost(PostgresFixture fixture,string staging,string storage):IAsyncDisposable
    {
        internal WebApplicationFactory<Program>? Current;
        internal string Address="";
        internal Task Start(int port=0)
        {
            Current=Host(fixture,staging,storage);Current.UseKestrel(server=>server.Listen(IPAddress.Loopback,port));
            Address=Current.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            Current.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=Address;return Task.CompletedTask;
        }
        internal async Task Restart()
        {int port=new Uri(Address).Port;await Current!.DisposeAsync();await Start(port);}
        public async ValueTask DisposeAsync() {if(Current is not null) await Current.DisposeAsync();}
    }
    private static WebApplicationFactory<Program> Host(PostgresFixture fixture,string staging,string storage) =>
        new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true); services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true); services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true); services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true); services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<SettingsManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=staging;}); services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true); services.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true); services.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadIdentityOptions>(o=>o.Enabled=true); services.PostConfigure<UploadCorrectionOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadValidationPlanOptions>(o=>{o.Enabled=true;o.SigningKey=Convert.ToBase64String(Enumerable.Range(0,32).Select(i=>(byte)i).ToArray());});
            services.PostConfigure<UploadImportOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadJobOptions>(o=>{o.Enabled=true;o.StorageUrl=storage;o.StorageBucket="jobs";o.StorageKey="synthetic-secret";});
            services.PostConfigure<UploadUiOptions>(o=>o.Enabled=true);
        }));
    private static async Task Python(string root,string script,string directory)
    {
        var start=new ProcessStartInfo(Path.Combine(root,"..","venv","Scripts","python.exe"))
            {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root,"tests",script));start.ArgumentList.Add(directory);
        using var process=Process.Start(start)!;var output=process.StandardOutput.ReadToEndAsync();var errors=process.StandardError.ReadToEndAsync();
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(Token);timeout.CancelAfter(TimeSpan.FromMinutes(8));
        try {await process.WaitForExitAsync(timeout.Token);} catch {process.Kill(entireProcessTree:true);throw;}
        Assert.True(process.ExitCode==0,await output+await errors);
    }
    private sealed class LocalStorage(WebApplication app,string directory):IAsyncDisposable
    {
        private readonly string controlDirectory=directory;
        internal string Url=>app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        private readonly ConcurrentDictionary<string,byte[]> objects=new();
        internal static async Task<LocalStorage> Start(string directory,CancellationToken ct)
        {
            var b=WebApplication.CreateSlimBuilder();b.Logging.ClearProviders();b.WebHost.ConfigureKestrel(s=>s.Listen(IPAddress.Loopback,0));var app=b.Build();var server=new LocalStorage(app,directory);
            app.Run(async context=>
            {
                if(context.Request.Headers.Authorization!="Bearer synthetic-secret"||context.Request.Headers["apikey"]!="synthetic-secret") {context.Response.StatusCode=403;return;}
                string path=context.Request.Path.Value!;
                if(path=="/storage/v1/bucket/jobs") {await context.Response.WriteAsJsonAsync(new {id="jobs",@public=false},context.RequestAborted);return;}
                string[] parts=path.Split('/');string key=parts.Length>=3?parts[^2]:"";
                if(!Guid.TryParseExact(key,"N",out _)) {context.Response.StatusCode=400;return;}
                if(context.Request.Method=="POST")
                {
                    if(context.Request.Headers["x-upsert"]!="false") {context.Response.StatusCode=400;return;}
                    using var output=new MemoryStream();await context.Request.Body.CopyToAsync(output,context.RequestAborted);
                    // Owned-file gates coordinate browser faults without adding application endpoints.
                    while(File.Exists(Path.Combine(server.controlDirectory,"hold-source"))) await Task.Delay(50,context.RequestAborted);
                    context.Response.StatusCode=server.objects.TryAdd(key,output.ToArray())?200:409;return;
                }
                if(context.Request.Method=="DELETE") {server.objects.TryRemove(key,out _);context.Response.StatusCode=200;return;}
                if(!server.objects.TryGetValue(key,out var bytes)) {context.Response.StatusCode=404;return;}
                context.Response.ContentLength=bytes.Length;await context.Response.Body.WriteAsync(bytes,context.RequestAborted);
            });
            await app.StartAsync(ct);return server;
        }
        public async ValueTask DisposeAsync() {await app.StopAsync();await app.DisposeAsync();}
    }
}
