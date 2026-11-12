using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Uploads;
using AuditPlatform.Api.Workbooks;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ImportGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public async ValueTask InitializeAsync()
    {
        DirectoryInfo? root=new(AppContext.BaseDirectory);
        while(root is not null&&!File.Exists(Path.Combine(root.FullName,"AuditPlatform.slnx"))) root=root.Parent;
        if(root is null) throw new InvalidOperationException("Solution not found");
        DirectoryPath=Path.Combine(root.FullName,".tools","p25-tests-"+Guid.NewGuid().ToString("N"));
        var start=new ProcessStartInfo(Path.Combine(root.FullName,"..","venv","Scripts","python.exe"))
            {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root.FullName,"tests","p25_import_oracle.py"));start.ArgumentList.Add(DirectoryPath);
        using var process=Process.Start(start)!;var output=process.StandardOutput.ReadToEndAsync();var errors=process.StandardError.ReadToEndAsync();
        using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(60));
        try {await process.WaitForExitAsync(deadline.Token);} catch {process.Kill(entireProcessTree:true);throw;}
        Assert.True(process.ExitCode==0,await output+await errors);
        Oracle=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(DirectoryPath,"oracle.json")));
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadImportTests(ImportGoldenFixture golden) : IClassFixture<ImportGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static UploadValidationPlanOptions Settings()=>new(){Enabled=true,SigningKey=Convert.ToBase64String(Enumerable.Range(0,32).Select(i=>(byte)i).ToArray())};
    private static IdentityRequest Request(string sheet="Base",string mode="replace_range")=>
        new(new(new(new(new(10,1,sheet,"rows",RequestId:Guid.NewGuid()),mode,5),true),"2026-04-01","2026-04-30",true));
    [Fact]
    public void RawGuardsMatchExtractedPython()
    {
        foreach(var vector in golden.Oracle.RootElement.GetProperty("fingerprints").EnumerateArray())
            Assert.Equal(vector.GetProperty("hash").GetString(),UploadImportWriter.RawFingerprint(UploadIdentityValue.FromJson(vector.GetProperty("values").GetRawText())));
    }
    [Fact]
    public async Task DefaultEndpointAndPrerequisitesStayDisabled()
    {
        await using var app=new WebApplicationFactory<Program>();using var client=app.CreateClient();
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.PostAsJsonAsync("/api/upload/file/1/atomic-import",new {},Token)).StatusCode);
        await using var missing=new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>s.PostConfigure<UploadImportOptions>(o=>o.Enabled=true)));
        Assert.Throws<OptionsValidationException>(()=>missing.CreateClient());
    }
    [Fact]
    public async Task AtomicHistoryProtectionRecoveryAndScope()
    {
        await using var fixture=new PostgresFixture();await fixture.InitializeAsync();await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection=await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string sql) {await using var command=new NpgsqlCommand(sql,connection);await command.ExecuteNonQueryAsync(Token);}
        async Task<long> Count(string sql) {await using var command=new NpgsqlCommand(sql,connection);return (long)(await command.ExecuteScalarAsync(Token))!;}
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
        await fixture.ApplyTenantFoundationAsync(Token);await fixture.ApplyEngagementFoundationAsync(Token);await fixture.ApplyTenantUserRolesAsync(Token);
        await fixture.ApplyTenantColumnsAsync(Token);await fixture.ApplyTenantTemplatesAsync(Token);await fixture.ApplySavedMappingsAsync(Token);await fixture.ApplyUploadStagingAsync(Token);
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
        using var pool=fixture.CreatePool();var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var logs=Options.Create(new LogPersistenceOptions());var audit=new PostgresAuditWriter(new LogSanitizer(logs),logs);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions{Enabled=true}));
        var owner=(await sessions.FindAsync(new string('a',64),Token))!;var staff=(await sessions.FindAsync(new string('c',64),Token))!;
        var other=(await sessions.FindAsync(new string('b',64),Token))!;
        string directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P24-"+Guid.NewGuid().ToString("N"));var clock=new Clock();
        var staging=new UploadStagingService(tx,audit,Options.Create(new UploadStagingOptions{Enabled=true,RootDirectory=directory}),clock);
        var mappingService=new TenantMappingService(tx,audit);var preparation=new UploadPreparationService(staging,mappingService,Options.Create(new UploadPreparationOptions{Enabled=true}));
        var inputOptions=Options.Create(new UploadInputValidationOptions{Enabled=true});
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
        var footingOptions=Options.Create(new UploadFootingOptions{Enabled=true});
        var coverage=new UploadCoverageService(preparation,tx,Options.Create(new UploadCoverageOptions{Enabled=true}),footingOptions,inputOptions);
        var snapshots=new PostgresReadSnapshot(pool,Options.Create(new PostgresOptions()));
        var identity=new UploadIdentityService(coverage,snapshots,Options.Create(new UploadIdentityOptions{Enabled=true}));
        using var stream=File.OpenRead(Path.Combine(golden.DirectoryPath,"plans.xlsx"));
        var receipt=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",stream,Token);Assert.Equal("success",receipt.Status);int id=receipt.FileId!.Value;
        async Task<IdentityRequest> Bound(string sheet="Base",bool manual=false,bool all=false)
        {
            var source=new PreparationRequest(10,1,sheet,RequestId:Guid.NewGuid(),Mappings:manual?
                [new(1,1),new(2,2),new(3,3),new(4,4),new(5,5),new(6,all?6:null)]:null);
            var prepared=(await preparation.PrepareAsync(owner,id,source,Token)).Output!;
            source=source with {Sha256=prepared.Context.Sha256,ConfigurationKey=prepared.Context.ConfigurationKey,MappingRevision=prepared.Context.MappingRevision};
            return Request(sheet) with {Coverage=Request(sheet).Coverage with {Footing=new(new(source,"replace_range",5),true)}};
        }


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
        await fixture.ApplyUploadImportsAsync(Token);
        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadImportsAsync(Token));
        var corrections=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true}));
        var settings=Options.Create(Settings());var signer=new UploadValidationPlanSigner(settings);
        var store=new UploadValidationPlanStore(tx,settings,clock);var plans=new UploadValidationPlanService(corrections,store,signer,settings,clock);
        var importOptions=Options.Create(new UploadImportOptions{Enabled=true,BatchSize=1});var writer=new UploadImportWriter(importOptions);
        UploadImportService Importer(PostgresTransactions? transaction=null)=>new(transaction??tx,store,signer,staging,writer,audit,importOptions,clock);
        var imports=Importer();
        async Task<UploadImportRequest> Plan(string sheet="Changed",string mode="replace_range")
        {
            var bound=await Bound(sheet);bound=bound with {Coverage=bound.Coverage with {Footing=bound.Coverage.Footing with {Validation=bound.Coverage.Footing.Validation with {ControlTotalRow=sheet=="Removed"?4:sheet=="Added"?6:5}}}};var review=new CorrectionRequest(bound.Coverage with {Footing=bound.Coverage.Footing with {Validation=bound.Coverage.Footing.Validation with {ComparisonMode=mode}}});
            var p=await plans.CreateAsync(owner,id,new(Guid.NewGuid(),review),Token);Assert.True(p.Status=="success",p.Code);Assert.Equal("ready",p.Output!.Status);
            return new(Guid.NewGuid(),p.Output.PlanId!,p.Output.Signature!,review);
        }
        async Task<UploadImportResult> Import(UploadImportRequest p)=>await imports.ImportAsync(owner,id,p,Token);
        async Task<string> State()
        {
            await using var c=new NpgsqlCommand("SELECT jsonb_agg(to_jsonb(c) ORDER BY id)::text FROM public.client_data c",connection);
            return (string)(await c.ExecuteScalarAsync(Token))!;
        }
        string original=await State();
        async Task Reset()
        {
            await Sql("DELETE FROM public.verification_details;DELETE FROM public.voucher_line_documents;DELETE FROM public.voucher_line_physical_bill_items;DELETE FROM public.cashflow_prepared_lines;DELETE FROM public.client_data_history;DELETE FROM public.platform_upload_imports;DELETE FROM public.client_data WHERE client_id=10");
            await using(var c=new NpgsqlCommand("INSERT INTO public.client_data OVERRIDING SYSTEM VALUE SELECT * FROM jsonb_populate_recordset(NULL::public.client_data,$1::jsonb) WHERE client_id=10",connection))
            {c.Parameters.AddWithValue(original);await c.ExecuteNonQueryAsync(Token);}
            await Sql($"UPDATE public.uploaded_files SET status='Pending' WHERE id={id}");
        }
        var request=await Plan();string before=await State();long audits=await Count("SELECT count(*) FROM public.audit_log");
        Assert.Equal("conflict",(await Import(request with {Signature=new string('0',64)})).Status);
        await Sql("UPDATE public.client_data SET version=version+1 WHERE id=101");Assert.Equal("plan_data_changed",(await Import(request)).Code);
        await Sql("UPDATE public.client_data SET version=version-1 WHERE id=101");
        clock.Current=clock.Current.AddMinutes(31);Assert.Equal("plan_expired",(await Import(request)).Code);clock.Current=DateTimeOffset.UtcNow;
        await Sql("INSERT INTO public.client_data(id,client_id,template_id,row_data,version,document_group_id,source_identity,extract_date) OVERRIDING SYSTEM VALUE VALUES(104,10,1,'{}',1,'1-V1','outside','2026-05-01')");
        Assert.Equal("plan_data_changed",(await Import(request)).Code);await Sql("DELETE FROM public.client_data WHERE id=104");
        string privatePath=Directory.GetFiles(directory,"*.bin").Single();byte[] originalBytes=await File.ReadAllBytesAsync(privatePath,Token);
        await File.WriteAllTextAsync(privatePath,"tampered",Token);Assert.Equal("gone",(await Import(request)).Status);await File.WriteAllBytesAsync(privatePath,originalBytes,Token);
        var lowConfiguration=new Microsoft.Extensions.Configuration.ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string,string?>{{"Platform:DatabaseCapacityLimitBytes","1"}}).Build();
        var lowCapacity=new UploadImportService(tx,store,signer,staging,new UploadImportWriter(importOptions,lowConfiguration),audit,importOptions,clock);
        Assert.Equal("import_storage_capacity",(await lowCapacity.ImportAsync(owner,id,request,Token)).Code);
        await Sql("DROP INDEX public.idx_client_data_active_identity_unique;CREATE UNIQUE INDEX idx_client_data_active_identity_unique ON public.client_data(client_id,template_id,source_identity) WHERE client_id=-1");
        Assert.Equal("import_identity_index_required",(await Import(request)).Code);
        await Sql("DROP INDEX public.idx_client_data_active_identity_unique;CREATE UNIQUE INDEX idx_client_data_active_identity_unique ON public.client_data(client_id,template_id,source_identity) WHERE is_deleted=FALSE AND change_detection_status<>'deleted' AND source_identity IS NOT NULL AND source_identity<>''");
        await Sql("SELECT pg_advisory_lock(746102)");
        try
        {
            var shortOptions=Options.Create(new UploadImportOptions{Enabled=true,MaximumConcurrentRequests=1,TimeoutSeconds=1});
            var shortImport=new UploadImportService(tx,store,signer,staging,writer,audit,shortOptions,clock);
            var waiting=shortImport.ImportAsync(owner,id,request,Token);Assert.Equal("busy",(await shortImport.ImportAsync(owner,id,request,Token)).Status);
            Assert.Equal("timeout",(await waiting).Status);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Token);
            var stopped=shortImport.ImportAsync(owner,id,request,cancelled.Token);await cancelled.CancelAsync();await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>stopped);
        }
        finally {await Sql("SELECT pg_advisory_unlock(746102)");}
        // Fail after history insertion, then after the current-row update. Neither
        // leaves history, statuses, audits, consumption or recovery receipts.
        await Sql("CREATE FUNCTION public.p25_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic import failure'; END $$");
        foreach(string table in new[]{"client_data_history","client_data","platform_upload_imports"})
        {
            await Sql($"CREATE TRIGGER p25_fail AFTER {(table=="client_data"?"UPDATE":"INSERT")} ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.p25_fail()");
            await Assert.ThrowsAsync<PostgresException>(()=>Import(request));
            Assert.Equal(before,await State());Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
            Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_imports"));Assert.Equal(audits,await Count("SELECT count(*) FROM public.audit_log"));
            Assert.Equal(1L,await Count("SELECT count(*) FROM public.uploaded_files WHERE status='Pending'"));
            Assert.Equal(1L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE status='ready'"));
            await Sql($"DROP TRIGGER p25_fail ON public.{table}");
        }
        await Sql("INSERT INTO public.verification_details VALUES(1,102,'Cleared')");
        var deliveries=await Task.WhenAll(Import(request),Import(request with {OperationId=Guid.NewGuid()}));
        Assert.All(deliveries,r=>Assert.True(r.Status=="success",r.Code));Assert.Equal(deliveries[0].Output,deliveries[1].Output);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_imports"));Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data_history"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.verification_details WHERE id=1 AND client_data_id=102 AND validation_status='Pending'"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE id=102 AND version=2 AND created_at='2026-01-01'"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data_history WHERE client_data_id=102 AND row_data->>'col_3'='100' AND source_metadata->>'extract_date'='2026-04-03' AND source_metadata->>'source_row_number'='3'"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE status='ready'"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'"));
        Assert.Equal(deliveries[0].Output,(await Import(request)).Output);
        Assert.Equal("import_operation_reused",(await Import(request with {Signature=new string('0',64)})).Code);
        Assert.Equal("forbidden",(await imports.ImportAsync(other,id,request,Token)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3");
        Assert.Equal("forbidden",(await imports.ImportAsync(staff,id,request,Token)).Status);
        await Reset();
        var removal=await Plan("Removed");
        foreach(var (table,column) in new[]{("verification_details","client_data_id"),("voucher_line_documents","client_data_id"),("voucher_line_physical_bill_items","source_document_row_id"),("cashflow_prepared_lines","source_client_data_id")})
        {
            await Sql(table=="verification_details"?"INSERT INTO public.verification_details VALUES(1,103,'Cleared')":$"INSERT INTO public.{table}({column}) VALUES(103)");
            Assert.Equal("import_protected_evidence",(await Import(removal)).Code);Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
            await Sql($"DELETE FROM public.{table}");
        }
        var removed=await Import(removal);Assert.True(removed.Status=="success",removed.Code);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data WHERE id=103"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data_history WHERE action='deleted' AND client_data_id=103"));
        await Reset();
        var partial=await Plan("Removed","correction");
        Assert.Equal(0,partial.Review.Coverage.Footing.Validation.ComparisonMode=="replace_range"?1:0);
        var retained=await Import(partial);Assert.True(retained.Status=="success",retained.Code);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE id=103"));Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
        await Reset();
        var information=await Plan("Info");var infoResult=await Import(information);Assert.True(infoResult.Status=="success",infoResult.Code);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));Assert.Equal(3L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND version=1"));
        Assert.Equal(3L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND row_data->>'col_5'='updated'"));
        await Reset();
        var added=await Plan("Added");var addResult=await Import(added);Assert.True(addResult.Status=="success",addResult.Code);
        Assert.Equal(4L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10"));Assert.Equal(2L,await Count("SELECT count(*) FROM public.client_data WHERE row_data->>'col_1'='V1'"));
        await Reset();
        var gated=await Plan();await Sql("UPDATE public.client_data SET derived_account_code='A' WHERE id=102");
        // Guard predates derived metadata, so the writer itself must block it.
        Assert.Equal("import_derived_fields_required",(await Import(gated)).Code);
        await Reset();
        var httpRequest=await Plan();
        await using(var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);services.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true);services.PostConfigure<UploadIdentityOptions>(o=>o.Enabled=true);services.PostConfigure<UploadCorrectionOptions>(o=>o.Enabled=true);services.PostConfigure<UploadValidationPlanOptions>(o=>{o.Enabled=true;o.SigningKey=Settings().SigningKey;});
            services.PostConfigure<UploadImportOptions>(o=>o.Enabled=true);
        })))
        {
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
            string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
            string route=$"/api/upload/file/{id}/atomic-import";client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,httpRequest,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);client.DefaultRequestHeaders.Remove("Origin");
            Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,httpRequest,Token)).StatusCode);client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,httpRequest,Token)).StatusCode);client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));

            Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsJsonAsync(route,httpRequest with {OperationId=Guid.Empty},Token)).StatusCode);
            string bad=JsonSerializer.Serialize(httpRequest,new JsonSerializerOptions(JsonSerializerDefaults.Web)).Replace("\"controlTotalRow\":5","\"controlTotalRow\":\"5\"",StringComparison.Ordinal);
            using(var body=new StringContent(bad,System.Text.Encoding.UTF8,"application/json")) Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(route,body,Token)).StatusCode);
            using(var oversized=new HttpRequestMessage(HttpMethod.Post,route))
            {
                oversized.Headers.TransferEncodingChunked=true;oversized.Content=new StringContent("{\"padding\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json");
                Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(oversized,Token)).StatusCode);
            }
            var response=await client.PostAsJsonAsync(route,httpRequest,Token);Assert.Equal(HttpStatusCode.OK,response.StatusCode);Assert.Contains("no-store",response.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
        }
        await Reset();
        var lost=await Plan();await using var proxy=fixture.CreateCommitProxy();using var proxiedPool=fixture.CreatePool(proxy:proxy);
        var proxiedTx=new PostgresTransactions(proxiedPool,Options.Create(new PostgresOptions()));
        clock.OnRead=()=>proxy.Arm();var lostResult=await Importer(proxiedTx).ImportAsync(owner,id,lost,Token);clock.OnRead=null;
        await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Token);
        Assert.True(lostResult.Status=="success"||lostResult.Code=="import_outcome_unknown_retry_same_operation",lostResult.Code);
        Assert.Equal("success",(await Import(lost)).Status);Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_imports"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data_history"));
        // Recovery does not depend on source availability or unexpired plans.
        foreach(string path in Directory.GetFiles(directory,"*.bin")) File.Delete(path);
        clock.Current=clock.Current.AddHours(3);Assert.Equal("success",(await Import(lost)).Status);clock.Current=DateTimeOffset.UtcNow;
    }
}
