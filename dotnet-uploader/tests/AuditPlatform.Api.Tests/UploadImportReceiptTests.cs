using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text.Json;
using System.Xml.Linq;
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

public sealed class ReceiptGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public ValueTask InitializeAsync() {
        DirectoryPath = NativeCompatibilityFixtureFactory.CreateWorkspace("p27", "p27-tests");
        Oracle = NativeCompatibilityFixtureFactory.ReadOracle(DirectoryPath);
        return ValueTask.CompletedTask;
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadImportReceiptTests(ReceiptGoldenFixture golden) : IClassFixture<ReceiptGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static UploadValidationPlanOptions Settings()=>new(){Enabled=true,SigningKey=Convert.ToBase64String(Enumerable.Range(0,32).Select(i=>(byte)i).ToArray())};
    private static IdentityRequest Request(string sheet="Base",string mode="replace_range")=>
        new(new(new(new(new(10,1,sheet,"rows",RequestId:Guid.NewGuid()),mode,5),true),"2026-04-01","2026-04-30",true));
    [Fact]
    public void ExactControlsMatchExtractedPython()
    {
        var expected=new FootingSummary(3,2,0,new Dictionary<int,string>{{3,"0.30"}},true);
        var source=new ReceiptSourceControls(expected,expected,[new(3,"col_3","Amount")],[],"col_1",true,"2026-04-01","2026-04-30",5,false);
        foreach(var vector in golden.Oracle.RootElement.GetProperty("controls").EnumerateArray())
        {
            var actual=vector.GetProperty("stored");FootingSummary? stored=actual.ValueKind==JsonValueKind.Null?null:new(actual.GetProperty("row_count").GetInt32(),actual.GetProperty("document_count").GetInt32(),actual.GetProperty("missing_document_rows").GetInt32(),
                new Dictionary<int,string>{{3,actual.GetProperty("footing_totals").GetProperty("Amount").GetString()!}},true);
            var result=UploadStoredControlService.Compare(source with {CompletePeriod=vector.GetProperty("complete").GetBoolean()},stored,DateTimeOffset.UnixEpoch,vector.GetProperty("unavailable").GetBoolean());
            Assert.Equal(vector.GetProperty("status").GetString(),result.Status);
            Assert.Equal(vector.GetProperty("difference").ValueKind==JsonValueKind.Null?null:vector.GetProperty("difference").GetString(),result.Metrics[2].Difference);
        }
        Assert.Equal("0.03",UploadStoredControlService.Difference("1000000000000000000000000000000.03","1000000000000000000000000000000"));
        Assert.Equal("0",UploadStoredControlService.Difference("1"+new string('0',1100)+".1","1"+new string('0',1100)+".1"));
        Assert.Equal("0",UploadStoredControlService.Difference("-0.00","0"));
        var empty=new FootingSummary(0,0,0,new Dictionary<int,string>{{3,"0"}},true);
        Assert.Equal("matched",UploadStoredControlService.Compare(source with {ApprovedDetail=empty},empty,DateTimeOffset.UnixEpoch).Status);
        var noFooting=source with {Footings=[],ApprovedDetail=expected with {Totals=new Dictionary<int,string>()}};
        var countsOnly=UploadStoredControlService.Compare(noFooting,noFooting.ApprovedDetail,DateTimeOffset.UnixEpoch);
        Assert.Equal("matched",countsOnly.Status);Assert.False(countsOnly.FootingConfigured);Assert.Equal(2,countsOnly.Metrics.Count);
    }
    [Fact]
    public async Task ReceiptRoutesRemainDisabledByDefault()
    {
        await using var app=new WebApplicationFactory<Program>();using var client=app.CreateClient();
        string route=$"/api/upload/file/1/receipt/{Guid.NewGuid()}";
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync(route,Token)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync(route+"/workbook",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.PostAsJsonAsync("/api/upload/file/1/receipt/check",new {},Token)).StatusCode);
    }
    [Fact]
    public async Task OwnedReceiptsAreAtomicHistoricalScopedAndExportable()
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
        using var pool=fixture.CreatePool(timeoutSeconds:10);var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        await Sql("CREATE ROLE p27_receipt_reader NOLOGIN; GRANT USAGE ON SCHEMA public TO p27_receipt_reader; GRANT SELECT ON public.templates,public.platform_tenant_templates,public.template_column_mappings,public.master_columns,public.platform_tenant_columns TO p27_receipt_reader; DO $$ BEGIN IF to_regclass('public.platform_template_upload_bindings') IS NOT NULL THEN GRANT SELECT ON public.platform_template_upload_bindings TO p27_receipt_reader; END IF; END $$");
        await tx.ExecuteAsync(async(db,ct)=>
        {
            await using(var role=db.CreateCommand("SET LOCAL ROLE p27_receipt_reader")) await role.ExecuteNonQueryAsync(ct);
            await using(var savepoint=db.CreateCommand("SAVEPOINT p27_receipt_lock")) await savepoint.ExecuteNonQueryAsync(ct);
            var denied=await Assert.ThrowsAsync<PostgresException>(()=>TenantTemplateService.LoadAsync(db,101,1,ct));
            Assert.Equal("42501",denied.SqlState);
            await using(var rollback=db.CreateCommand("ROLLBACK TO SAVEPOINT p27_receipt_lock")) await rollback.ExecuteNonQueryAsync(ct);
            var definition=Assert.Single(await TenantTemplateService.LoadAsync(db,101,1,ct,lockRows:false));
            Assert.Equal(1,definition.Id);return true;
        },Token);
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
        var stagedReceipt=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",stream,Token);Assert.Equal("success",stagedReceipt.Status);int id=stagedReceipt.FileId!.Value;
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
        await fixture.ApplyUploadImportsAsync(Token);await fixture.ApplyUploadImportReceiptsAsync(Token);
        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadImportsAsync(Token));
        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadImportReceiptsAsync(Token));
        await Sql("""UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_4}','"=SOURCE()"') WHERE client_id=10""");
        var corrections=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true}));
        var settings=Options.Create(Settings());var signer=new UploadValidationPlanSigner(settings);var store=new UploadValidationPlanStore(tx,settings,clock);
        var plans=new UploadValidationPlanService(corrections,store,signer,settings,clock);var importOptions=Options.Create(new UploadImportOptions{Enabled=true,BatchSize=1});
        var imports=new UploadImportService(tx,store,signer,staging,new UploadImportWriter(importOptions),audit,importOptions,clock);
        var receipts=new UploadImportReceiptService(tx,store,corrections,staging,importOptions,clock);
        async Task<ValidationPlanRequest> Review(string sheet="Changed",string mode="replace_range")
        {
            var source=new PreparationRequest(10,1,sheet,RequestId:Guid.NewGuid(),Mappings:[new(1,1),new(2,2),new(3,3),new(4,4),new(5,5),new(6,6)]);
            var prepared=await preparation.PrepareAsync(owner,id,source,Token);Assert.Equal("success",prepared.Status);
            source=source with {Sha256=prepared.Output!.Context.Sha256,ConfigurationKey=prepared.Output.Context.ConfigurationKey,MappingRevision=prepared.Output.Context.MappingRevision};
            return new(Guid.NewGuid(),new(new(new(new(source,mode,5),true),"2026-04-01","2026-04-30",true)));
        }
        async Task<UploadImportRequest> Plan(string sheet="Changed",string mode="replace_range")
        {
            var request=await Review(sheet,mode);var created=await plans.CreateAsync(owner,id,request,Token);Assert.True(created.Status=="success",created.Code);
            return new(Guid.NewGuid(),created.Output!.PlanId!,created.Output.Signature!,request.Review);
        }
        async Task Stage()
        {using var file=File.OpenRead(Path.Combine(golden.DirectoryPath,"plans.xlsx"));var staged=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",file,Token);Assert.Equal("success",staged.Status);id=staged.FileId!.Value;}
        var input=await Plan();
        await Sql("CREATE FUNCTION public.p27_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic receipt failure'; END $$; CREATE TRIGGER p27_fail AFTER INSERT ON public.platform_upload_import_receipts FOR EACH ROW EXECUTE FUNCTION public.p27_fail()");
        await Assert.ThrowsAsync<PostgresException>(()=>imports.ImportAsync(owner,id,input,Token));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_imports"));Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
        await Sql("DROP TRIGGER p27_fail ON public.platform_upload_import_receipts");
        var imported=await imports.ImportAsync(owner,id,input,Token);Assert.True(imported.Status=="success",imported.Code);Assert.Equal("matched",imported.Output!.ControlStatus);
        Assert.Equal(input.OperationId,imported.Output.ReceiptId);
        var receipt=(await receipts.GetAsync(owner,id,input.OperationId,Token)).Receipt!;Assert.Equal("matched",receipt.Reconciliation.Status);Assert.Equal("0",receipt.Reconciliation.Metrics[2].Difference);
        Assert.Equal(3,receipt.Source.ApprovedDetail.RowCount);Assert.Equal(2,receipt.Source.ApprovedDetail.DocumentCount);
        Assert.Equal(imported.Output,(await imports.ImportAsync(owner,id,input,Token)).Output);
        var workbook=await receipts.WorkbookAsync(owner,id,input.OperationId,Token);Assert.True(workbook.Status=="success",workbook.Code);
        string outputPath=Path.Combine(golden.DirectoryPath,"receipt.xlsx");await File.WriteAllBytesAsync(outputPath,workbook.Bytes!,Token);
        await VerifyWorkbook(outputPath,Token);
        Assert.Equal(workbook.Bytes,(await receipts.WorkbookAsync(owner,id,input.OperationId,Token)).Bytes);
        var edgeRow=new ReceiptEvidenceRow(1,"1-0000123","2026-04-03","""{"col_1":"0000123","col_2":"=HYPERLINK(unsafe)","col_3":123456789012345678901234567890.12345,"col_4":"_x000D_ & < >","col_5":"-00123","col_6":"+SUM(A1:A9)"}""","unused");
        string edgePath=Path.Combine(golden.DirectoryPath,"edges.xlsx");
        await File.WriteAllBytesAsync(edgePath,UploadIssueWorkbookWriter.Write(new(receipt,[],[edgeRow,edgeRow with {Id=2,Raw="""{"col_3":1e-1000}"""}]),importOptions.Value,Token),Token);
        await VerifyWorkbook(edgePath,Token,"--verify-edges");
        var oversizedRow=edgeRow with {Raw=JsonSerializer.Serialize(new {col_1=new string('x',32768)})};
        Assert.Throws<InvalidDataException>(()=>UploadIssueWorkbookWriter.Write(new(receipt,[],[oversizedRow]),importOptions.Value,Token));
        Assert.Throws<InvalidDataException>(()=>UploadIssueWorkbookWriter.Write(new(receipt,[],[edgeRow]),new UploadImportOptions{MaximumWorkbookBytes=4096},Token));
        using(var cancelled=new CancellationTokenSource())
        {cancelled.Cancel();Assert.Throws<OperationCanceledException>(()=>UploadIssueWorkbookWriter.Write(new(receipt,[],[edgeRow]),importOptions.Value,cancelled.Token));}

        Assert.Equal("not_found",(await receipts.GetAsync(other,id,input.OperationId,Token)).Status);
        Assert.Equal("not_found",(await receipts.GetAsync(staff,id,input.OperationId,Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='member' WHERE tenant_id=101 AND user_id=1");
        Assert.Equal("forbidden",(await receipts.GetAsync(owner,id,input.OperationId,Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='admin' WHERE tenant_id=101 AND user_id=1");
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("UPDATE public.platform_upload_import_receipts SET kind='no_changes'"));
        await Sql("UPDATE public.platform_tenant_columns SET display_name='Renamed amount' WHERE column_id=3");
        Assert.Equal("Amount",(await receipts.GetAsync(owner,id,input.OperationId,Token)).Receipt!.Source.Footings[0].Name);
        Assert.Equal("success",(await receipts.WorkbookAsync(owner,id,input.OperationId,Token)).Status);
        await Sql("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_3}','121') WHERE id=102");
        Assert.Equal("gone",(await receipts.WorkbookAsync(owner,id,input.OperationId,Token)).Status);
        Assert.Equal("matched",(await receipts.GetAsync(owner,id,input.OperationId,Token)).Receipt!.Reconciliation.Status);
        await Sql("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_3}','120') WHERE id=102");
        clock.Current=clock.Current.AddHours(3);Assert.Equal("gone",(await receipts.WorkbookAsync(owner,id,input.OperationId,Token)).Status);
        Assert.Equal("success",(await receipts.GetAsync(owner,id,input.OperationId,Token)).Status);clock.Current=DateTimeOffset.UtcNow;
        await Stage();var noChanges=await Review();string canonical;
        await using(var c=new NpgsqlCommand("SELECT jsonb_agg(to_jsonb(c) ORDER BY id)::text FROM public.client_data c",connection)) canonical=(string)(await c.ExecuteScalarAsync(Token))!;
        var noOp=await receipts.CheckNoChangesAsync(owner,id,noChanges,Token);Assert.True(noOp.Status=="success",noOp.Code);Assert.Equal("no_changes",noOp.Receipt!.Kind);Assert.Equal("matched",noOp.Receipt.Reconciliation.Status);
        Assert.Equal(noOp.Receipt.Id,(await receipts.CheckNoChangesAsync(owner,id,noChanges,Token)).Receipt!.Id);
        await using(var c=new NpgsqlCommand("SELECT jsonb_agg(to_jsonb(c) ORDER BY id)::text FROM public.client_data c",connection)) Assert.Equal(canonical,(string)(await c.ExecuteScalarAsync(Token))!);
        await Sql("UPDATE public.client_data SET version=version+1 WHERE id=101");
        Assert.Equal(noOp.Receipt.CompletedAt,(await receipts.CheckNoChangesAsync(owner,id,noChanges,Token)).Receipt!.CompletedAt);
        await Sql("UPDATE public.client_data SET version=version-1 WHERE id=101");
        await using(var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);services.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true);services.PostConfigure<UploadIdentityOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadCorrectionOptions>(o=>o.Enabled=true);services.PostConfigure<UploadImportOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadValidationPlanOptions>(o=>{o.Enabled=true;o.SigningKey=Settings().SigningKey;});
        })))
        {
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
            string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
            string route=$"/api/upload/file/{receipt.FileId}/receipt/{receipt.Id}";
            Assert.Equal(HttpStatusCode.Unauthorized,(await client.GetAsync(route,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);
            Assert.Equal(HttpStatusCode.Conflict,(await client.GetAsync(route,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
            var metadata=await client.GetAsync(route,Token);Assert.Equal(HttpStatusCode.OK,metadata.StatusCode);
            Assert.Contains("no-store",metadata.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
            var download=await client.GetAsync(route+"/workbook",Token);Assert.Equal(HttpStatusCode.OK,download.StatusCode);
            Assert.Equal("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",download.Content.Headers.ContentType!.MediaType);
            string check=$"/api/upload/file/{id}/receipt/check";
            Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(check,noChanges,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsJsonAsync(check,noChanges with {RequestId=Guid.Empty},Token)).StatusCode);
            using(var oversized=new HttpRequestMessage(HttpMethod.Post,check))
            {
                oversized.Headers.TransferEncodingChunked=true;oversized.Content=new StringContent("{\"padding\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json");
                Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(oversized,Token)).StatusCode);
            }
            string bad=JsonSerializer.Serialize(noChanges,new JsonSerializerOptions(JsonSerializerDefaults.Web)).Replace("\"controlTotalRow\":5","\"controlTotalRow\":\"5\"",StringComparison.Ordinal);
            using(var body=new StringContent(bad,System.Text.Encoding.UTF8,"application/json")) Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(check,body,Token)).StatusCode);
            var fresh=await client.PostAsJsonAsync(check,noChanges with {RequestId=Guid.NewGuid()},Token);Assert.Equal(HttpStatusCode.OK,fresh.StatusCode);
            Assert.Equal("success",(await fresh.Content.ReadFromJsonAsync<ReceiptResult>(Token))!.Status);
            client.DefaultRequestHeaders.Remove("Cookie");client.DefaultRequestHeaders.Add("Cookie","session_token="+staff.Token);
            client.DefaultRequestHeaders.Remove("X-App-Context");client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(staff));
            Assert.Equal(HttpStatusCode.NotFound,(await client.GetAsync(route,Token)).StatusCode);
        }
        var sqlFailure=await tx.ExecuteAsync(async(db,ct)=>
        {
            await using(var before=db.CreateCommand("SAVEPOINT p27_test")) await before.ExecuteNonQueryAsync(ct);
            await using(var invalid=db.CreateCommand("""UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_3}','"1e999999999"') WHERE id=101""")) await invalid.ExecuteNonQueryAsync(ct);
            var result=await UploadStoredControlService.VerifyAsync(db,receipt.Source,10,1,DateTimeOffset.UtcNow,ct);
            await using(var alive=db.CreateCommand("SELECT 42")) Assert.Equal(42,(int)(await alive.ExecuteScalarAsync(ct))!);
            await using(var rollback=db.CreateCommand("ROLLBACK TO SAVEPOINT p27_test")) await rollback.ExecuteNonQueryAsync(ct);
            return result;
        },Token);Assert.Equal("unavailable",sqlFailure.Status);
        var concurrent=noChanges with {RequestId=Guid.NewGuid()};long beforeConcurrent=await Count("SELECT count(*) FROM public.platform_upload_import_receipts");
        var concurrentResults=await Task.WhenAll(receipts.CheckNoChangesAsync(owner,id,concurrent,Token),receipts.CheckNoChangesAsync(owner,id,concurrent,Token));
        Assert.All(concurrentResults,r=>Assert.True(r.Status=="success",r.Code));
        Assert.Equal(beforeConcurrent+1,await Count("SELECT count(*) FROM public.platform_upload_import_receipts"));
        var countLimited=new UploadImportReceiptService(tx,store,corrections,staging,Options.Create(new UploadImportOptions{Enabled=true,MaximumNoChangeReceiptsPerFile=1}),clock);
        Assert.Equal("receipt_count_capacity",(await countLimited.CheckNoChangesAsync(owner,id,noChanges with {RequestId=Guid.NewGuid()},Token)).Code);
        var capacity=new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string,string?>{{"Platform:DatabaseCapacityLimitBytes","1"}}).Build();
        var storageLimited=new UploadImportReceiptService(tx,store,corrections,staging,importOptions,clock,capacity);
        Assert.Equal("receipt_storage_capacity",(await storageLimited.CheckNoChangesAsync(owner,id,noChanges with {RequestId=Guid.NewGuid()},Token)).Code);
        Assert.Equal(beforeConcurrent+1,await Count("SELECT count(*) FROM public.platform_upload_import_receipts"));
        var changedRequest=await Review("Base");
        Assert.Equal("receipt_request_reused",(await receipts.CheckNoChangesAsync(owner,id,changedRequest with {RequestId=noOp.Receipt.Id},Token)).Code);
        var noOpLost=noChanges with {RequestId=Guid.NewGuid()};long beforeLost=await Count("SELECT count(*) FROM public.platform_upload_import_receipts");
        await using(var proxy=fixture.CreateCommitProxy()) using(var proxiedPool=fixture.CreatePool(proxy:proxy))
        {
            var proxied=new UploadImportReceiptService(new PostgresTransactions(proxiedPool,Options.Create(new PostgresOptions())),store,corrections,staging,importOptions,clock);
            clock.OnRead=proxy.Arm;var unknown=await proxied.CheckNoChangesAsync(owner,id,noOpLost,Token);clock.OnRead=null;
            await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Token);
            Assert.Equal("receipt_outcome_unknown_retry_same_request",unknown.Code);
        }
        Assert.Equal("success",(await receipts.CheckNoChangesAsync(owner,id,noOpLost,Token)).Status);
        Assert.Equal(beforeLost+1,await Count("SELECT count(*) FROM public.platform_upload_import_receipts"));
        var missingColumn=receipt.Source with {Footings=[new(3,"col_4","Invalid text")]};
        var diagnostic=await tx.ExecuteAsync(async(db,ct)=>
        {
            var result=await UploadStoredControlService.VerifyAsync(db,missingColumn,10,1,DateTimeOffset.UtcNow,ct);
            await using var alive=db.CreateCommand("SELECT 42");Assert.Equal(42,(int)(await alive.ExecuteScalarAsync(ct))!);return result;
        },Token);Assert.Equal("unavailable",diagnostic.Status);
        var brokenKey=receipt.Source with {Footings=[new(3,"does_not_exist","Missing")]};
        var mismatch=await tx.ExecuteAsync((db,ct)=>UploadStoredControlService.VerifyAsync(db,brokenKey,10,1,DateTimeOffset.UtcNow,ct),Token);
        Assert.Equal("mismatch",mismatch.Status);
        var partial=await tx.ExecuteAsync((db,ct)=>UploadStoredControlService.VerifyAsync(db,receipt.Source with {CompletePeriod=false},10,1,DateTimeOffset.UtcNow,ct),Token);Assert.Equal("not_comparable",partial.Status);
        var tooSmall=Options.Create(new UploadImportOptions{Enabled=true,MaximumWorkbookRows=1});
        var bounded=new UploadImportReceiptService(tx,store,corrections,staging,tooSmall,clock);
        Assert.Equal("conflict",(await bounded.WorkbookAsync(owner,receipt.FileId,receipt.Id,Token)).Status);
        await Stage();var lost=await Plan("Base");long count=await Count("SELECT count(*) FROM public.platform_upload_import_receipts");
        await using(var proxy=fixture.CreateCommitProxy()) using(var proxiedPool=fixture.CreatePool(proxy:proxy))
        {
            var proxied=new UploadImportService(new PostgresTransactions(proxiedPool,Options.Create(new PostgresOptions())),store,signer,staging,new UploadImportWriter(importOptions),audit,importOptions,clock);
            clock.OnRead=proxy.Arm;var result=await proxied.ImportAsync(owner,id,lost,Token);clock.OnRead=null;await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Token);
            Assert.True(result.Status=="success"||result.Code=="import_outcome_unknown_retry_same_operation",result.Code);
        }
        Assert.Equal("success",(await imports.ImportAsync(owner,id,lost,Token)).Status);Assert.Equal(count+1,await Count("SELECT count(*) FROM public.platform_upload_import_receipts"));
        await Stage();var mismatchInput=await Plan("Changed");
        await Sql("""
            CREATE FUNCTION public.p27_distort() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.client_id=10 AND NEW.row_data->>'col_3'='120' THEN NEW.row_data=jsonb_set(NEW.row_data,'{col_3}','119'); END IF; RETURN NEW; END $$;
            CREATE TRIGGER p27_distort BEFORE UPDATE ON public.client_data FOR EACH ROW EXECUTE FUNCTION public.p27_distort();
            """);
        var committedMismatch=await imports.ImportAsync(owner,id,mismatchInput,Token);
        Assert.True(committedMismatch.Status=="success",committedMismatch.Code);Assert.Equal("mismatch",committedMismatch.Output!.ControlStatus);
        Assert.Equal("-1",(await receipts.GetAsync(owner,id,mismatchInput.OperationId,Token)).Receipt!.Reconciliation.Metrics[2].Difference);
        await Sql("DROP TRIGGER p27_distort ON public.client_data");
        await Stage();var unavailableInput=await Plan("Changed");
        await Sql("""
            CREATE OR REPLACE FUNCTION public.p27_distort() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.client_id=10 AND NEW.row_data->>'col_3'='120' THEN NEW.row_data=jsonb_set(NEW.row_data,'{col_3}','"1e999999999"'); END IF; RETURN NEW; END $$;
            CREATE TRIGGER p27_distort BEFORE UPDATE ON public.client_data FOR EACH ROW EXECUTE FUNCTION public.p27_distort();
            """);
        var unavailable=await imports.ImportAsync(owner,id,unavailableInput,Token);
        Assert.True(unavailable.Status=="success",unavailable.Code);Assert.Equal("unavailable",unavailable.Output!.ControlStatus);
        Assert.Equal("unavailable",(await receipts.GetAsync(owner,id,unavailableInput.OperationId,Token)).Receipt!.Reconciliation.Status);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'"));
    }
    private static Task VerifyWorkbook(string path,CancellationToken ct,string mode="--verify")
    {
        ct.ThrowIfCancellationRequested();
        using var archive=ZipFile.OpenRead(path);
        XNamespace ns="http://schemas.openxmlformats.org/spreadsheetml/2006/main";
        var workbook=Load("xl/workbook.xml");
        var relationships=Load("xl/_rels/workbook.xml.rels");
        var targets=relationships.Root!.Elements().ToDictionary(e=>e.Attribute("Id")!.Value,e=>"xl/"+e.Attribute("Target")!.Value.TrimStart('/'));
        var sheets=workbook.Root!.Element(ns+"sheets")!.Elements(ns+"sheet").ToArray();
        var names=sheets.Select(sheet=>sheet.Attribute("name")!.Value).ToArray();
        Assert.Equal(new[]{"Summary","Reconciliation","Issues","Previous rows","Stored rows"},names);
        var worksheets=sheets.ToDictionary(sheet=>sheet.Attribute("name")!.Value,
            sheet=>Load(targets[sheet.Attribute(XName.Get("id","http://schemas.openxmlformats.org/officeDocument/2006/relationships"))!.Value]));
        var shared=archive.GetEntry("xl/sharedStrings.xml") is { } sharedEntry
            ? XDocument.Load(sharedEntry.Open()).Root!.Elements(ns+"si").Select(item=>string.Concat(item.Descendants(ns+"t").Select(t=>t.Value))).ToArray()
            : [];
        string? Cell(string sheet,string reference)
        {
            var cell=worksheets[sheet].Root!.Descendants(ns+"c").SingleOrDefault(e=>e.Attribute("r")?.Value==reference);
            if(cell is null) return null;
            return cell.Attribute("t")?.Value switch
            {
                "s" => shared[int.Parse(cell.Element(ns+"v")!.Value,System.Globalization.CultureInfo.InvariantCulture)],
                "inlineStr" => string.Concat(cell.Descendants(ns+"t").Select(t=>t.Value)),
                _ => cell.Element(ns+"v")?.Value
            };
        }
        foreach(var sheet in worksheets.Values)
        {
            Assert.Equal("A6",sheet.Root!.Descendants(ns+"pane").FirstOrDefault()?.Attribute("topLeftCell")?.Value);
            Assert.StartsWith("A5:",sheet.Root.Descendants(ns+"autoFilter").FirstOrDefault()?.Attribute("ref")?.Value);
            Assert.Equal("landscape",sheet.Root.Descendants(ns+"pageSetup").FirstOrDefault()?.Attribute("orientation")?.Value);
            Assert.Empty(sheet.Root.Descendants(ns+"f"));
        }
        Assert.Equal(7,worksheets["Previous rows"].Root!.Descendants(ns+"row").Count());
        Assert.Equal(7,worksheets["Stored rows"].Root!.Descendants(ns+"row").Count());
        Assert.Equal("100",Cell("Previous rows","F6"));Assert.Equal("100",Cell("Previous rows","F7"));
        Assert.Equal("100",Cell("Stored rows","F6"));Assert.Equal("120",Cell("Stored rows","F7"));
        Assert.Equal("270",Cell("Reconciliation","C8"));Assert.Equal("0",Cell("Reconciliation","D8"));
        Assert.Contains("not manager-reviewed",Cell("Summary","B16")!,StringComparison.OrdinalIgnoreCase);

        if(mode=="--verify-edges")
        {
            Assert.Equal("0000123",Cell("Stored rows","D6"));Assert.Equal("=HYPERLINK(unsafe)",Cell("Stored rows","E6"));
            Assert.Equal("123456789012345678901234567890.12345",Cell("Stored rows","F6"));
            Assert.Equal("1e-1000",Cell("Stored rows","F7"));Assert.Equal("-00123",Cell("Stored rows","H6"));
            Assert.Equal("+SUM(A1:A9)",Cell("Stored rows","I6"));
            Assert.Equal("_x005F_x000D_ & < >",Cell("Stored rows","G6"));
        }
        return Task.CompletedTask;

        XDocument Load(string entryName)
        {
            var entry=archive.GetEntry(entryName)??throw new InvalidDataException($"Workbook entry is missing: {entryName}");
            using var stream=entry.Open();return XDocument.Load(stream);
        }
    }
}
