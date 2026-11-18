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
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ValidationPlanGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public async ValueTask InitializeAsync()
    {
        DirectoryInfo? root=new(AppContext.BaseDirectory);
        while(root is not null&&!File.Exists(Path.Combine(root.FullName,"AuditPlatform.slnx"))) root=root.Parent;
        if(root is null) throw new InvalidOperationException("Solution not found");
        DirectoryPath=Path.Combine(root.FullName,".tools","p24-tests-"+Guid.NewGuid().ToString("N"));
        var start=new ProcessStartInfo(Path.Combine(root.FullName,"..","venv","Scripts","python.exe"))
            {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root.FullName,"tests","p24_validation_plan_oracle.py"));start.ArgumentList.Add(DirectoryPath);
        using var process=Process.Start(start)!;var output=process.StandardOutput.ReadToEndAsync();var errors=process.StandardError.ReadToEndAsync();
        using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(60));
        try {await process.WaitForExitAsync(deadline.Token);} catch {process.Kill(entireProcessTree:true);throw;}
        Assert.True(process.ExitCode==0,await output+await errors);
        Oracle=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(DirectoryPath,"oracle.json")));
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadValidationPlanTests(ValidationPlanGoldenFixture golden) : IClassFixture<ValidationPlanGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static IdentityProfile Profile(bool line=false,string? account=null)=>new(7,
        [new("Document","Text"),new("Date","Date"),new("Amount","Numeric"),new("Description","Text",Description:true),
            new("Balance","Text",false),new("Line ID","Text")],"Document","Date",line?"Line ID":null,account);
    private static IdentityRequest Request(string sheet="Base",string mode="replace_range")=>
        new(new(new(new(new(10,1,sheet,"rows",RequestId:Guid.NewGuid()),mode,5),true),"2026-04-01","2026-04-30",true));
    private static CoverageOutput Proof(IdentityRequest input)
    {
        var columns=new[]{new TemplateColumn(1,"Document","col_1","Text","General","document_id",true,false,0,1,true),
            new TemplateColumn(2,"Date","col_2","Date","General","document_date",true,false,1,1,true),
            new TemplateColumn(3,"Amount","col_3","Numeric","General",null,true,false,2,1,true)};
        var template=new TemplateDefinition(1,"Fixture","Admin",new DateTime(2026,1,1),false,false,"standard",[],null,"standard","native",1,columns);
        var maps=columns.Select(c=>new PreparedMapping(c.Id,c.StorageKey,c.Name,c.Type,c.Role,true,c.Id,1,"manual")).ToArray();
        var coverage=input.Coverage with {Footing=input.Coverage.Footing with {ControlTotalConfirmed=false,Validation=input.Coverage.Footing.Validation with {ControlTotalRow=null}}};
        return UploadCoverageService.Evaluate(template,maps,new(Guid.NewGuid(),1,10,1,"Proof","hash",template.ConfigurationKey,0,"selection"),coverage,
            [new("Proof",2,[new(2,1,WorkbookCellKind.Text,Text:"V"),new(2,2,WorkbookCellKind.Text,Text:"03/04/2026"),new(2,3,WorkbookCellKind.Number,NumberToken:"1")])],new(),new(),new(),ct:Token);
    }


    private static UploadValidationPlanOptions Settings()=>new(){Enabled=true,SigningKey=Convert.ToBase64String(Enumerable.Range(0,32).Select(i=>(byte)i).ToArray())};
    [Fact]
    public void SignaturesMatchPythonAndBindEveryEnvelopeField()
    {
        var signer=new UploadValidationPlanSigner(Options.Create(Settings()));
        var envelope=JsonSerializer.Deserialize<ValidationPlanEnvelope>(golden.Oracle.RootElement.GetProperty("envelope"))!;
        string signature=signer.Sign(envelope);Assert.Equal(golden.Oracle.RootElement.GetProperty("signature").GetString(),signature);
        Assert.True(signer.Verify(envelope,signature));Assert.False(signer.Verify(envelope with {User=2},signature));
        Assert.False(signer.Verify(envelope with {ExpiresAt=envelope.ExpiresAt.AddSeconds(1)},signature));
        Assert.False(signer.Verify(envelope,new string('0',64)));Assert.False(signer.Verify(envelope,new string('z',64)));
        var changed=Settings();changed.SigningKey=Convert.ToBase64String(new byte[32]);
        Assert.False(new UploadValidationPlanSigner(Options.Create(changed)).Verify(envelope,signature));
        var request=new CorrectionRequest(Request().Coverage);string hash=UploadValidationPlanSigner.RequestHash(1,request);
        Assert.Equal(hash,UploadValidationPlanSigner.RequestHash(1,request with {Page=2,PageSize=1}));
        Assert.NotEqual(hash,UploadValidationPlanSigner.RequestHash(2,request));
        Assert.NotEqual(hash,UploadValidationPlanSigner.RequestHash(1,request with {Coverage=request.Coverage with {CoverageEnd="2026-04-29"}}));
        Assert.Equal(12,golden.Oracle.RootElement.GetProperty("vectors").GetArrayLength());
    }
    [Fact]
    public async Task StartupRequiresSecretAndCorrectionPrerequisites()
    {
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(
            services=>services.PostConfigure<UploadValidationPlanOptions>(o=>o.Enabled=true)));
        Assert.Throws<OptionsValidationException>(()=>app.CreateClient());
        await using var missing=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(
            services=>services.PostConfigure<UploadValidationPlanOptions>(o=>{o.Enabled=true;o.SigningKey=Settings().SigningKey;})));
        Assert.Throws<OptionsValidationException>(()=>missing.CreateClient());
        Assert.True(new UploadValidationPlanOptions().ValidKey());Assert.False(new UploadValidationPlanOptions{Enabled=true,SigningKey="bad"}.ValidKey());
    }
    [Fact]
    public async Task OwnedPlansBindWholeActionsAndRejectStaleOrConcurrentRequestsWithoutImportWrites()
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
        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadValidationPlansAsync(Token));
        await using(var conflict=await fixture.OpenEmptyOwnedDatabaseAsync(Token))
        {
            await using(var c=new NpgsqlCommand("CREATE TABLE public.upload_validation_plans(plan_id TEXT,client_id INTEGER,template_id INTEGER,status TEXT); INSERT INTO public.upload_validation_plans VALUES('a',10,1,'ready'),('b',10,1,'ready')",conflict)) await c.ExecuteNonQueryAsync(Token);
            await using var migration=await conflict.BeginTransactionAsync(Token);
            await using(var c=new NpgsqlCommand(await PostgresFixture.ReadUploadValidationPlansAsync(Token),conflict,migration))
                Assert.Equal("23505",(await Assert.ThrowsAsync<PostgresException>(()=>c.ExecuteNonQueryAsync(Token))).SqlState);
            await migration.RollbackAsync(Token);
            await using(var c=new NpgsqlCommand("SELECT count(*) FROM public.upload_validation_plans",conflict)) Assert.Equal(2L,(long)(await c.ExecuteScalarAsync(Token))!);
        }
        var corrections=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true}));
        var settings=Options.Create(Settings());var signer=new UploadValidationPlanSigner(settings);
        var store=new UploadValidationPlanStore(tx,settings,clock);var plans=new UploadValidationPlanService(corrections,store,signer,settings,clock);
        async Task<CorrectionRequest> Review(string sheet="Changed",bool manual=false)=>new((await Bound(sheet,manual)).Coverage);
        async Task<ValidationPlanResult> Create(ValidationPlanRequest r,VerifiedSession? actor=null)=>await plans.CreateAsync(actor??owner,id,r,Token);
        async Task<ValidationPlanResult> Check(ValidationPlanOutput p,CorrectionRequest r)=>await plans.CheckAsync(owner,id,new(p.PlanId!,p.Signature!,r),Token);
        async Task<string> Canonical()
        {await using var c=new NpgsqlCommand("SELECT jsonb_agg(to_jsonb(cd) ORDER BY id)::text FROM public.client_data cd",connection);return (string)(await c.ExecuteScalarAsync(Token))!;}
        var request=new ValidationPlanRequest(Guid.NewGuid(),await Review());string canonical=await Canonical();
        long audits=await Count("SELECT count(*) FROM public.audit_log");
        var first=await Create(request);Assert.Equal("success",first.Status);var issued=first.Output!;Assert.Equal("ready",issued.Status);Assert.False(issued.CanImport);
        Assert.Equal(1,issued.Counts.Changed);Assert.Equal(2,issued.Counts.Unchanged);
        Assert.Equal(issued,(await Create(request)).Output);Assert.Equal("valid",(await Check(issued,request.Review)).Output!.Status);
        string alias=Guid.NewGuid().ToString("N");Guid aliasRequest=Guid.NewGuid();
        await Sql($"""
            UPDATE public.upload_validation_plans SET status='superseded' WHERE plan_id='{issued.PlanId}';
            INSERT INTO public.upload_validation_plans(plan_id,file_id,client_id,template_id,file_sha256,request_signature,plan_json,status,created_at,expires_at)
            SELECT '{alias}',file_id,client_id,template_id,file_sha256,request_signature,plan_json,'ready',created_at,expires_at
            FROM public.upload_validation_plans WHERE plan_id='{issued.PlanId}';
            INSERT INTO public.platform_upload_validation_plans(request_id,plan_id,tenant_id,user_id,client_id,template_id,file_id,envelope,signature,created_at,expires_at)
            SELECT '{aliasRequest}','{alias}',tenant_id,user_id,client_id,template_id,file_id,envelope,signature,created_at,expires_at
            FROM public.platform_upload_validation_plans WHERE plan_id='{issued.PlanId}';
            """);
        Assert.Equal("plan_identity_changed",(await plans.CheckAsync(owner,id,new(alias,issued.Signature!,request.Review),Token)).Code);
        await Sql($"DELETE FROM public.upload_validation_plans WHERE plan_id='{alias}'; UPDATE public.upload_validation_plans SET status='ready' WHERE plan_id='{issued.PlanId}'");
        var paged=request with {Review=request.Review with {Page=2,PageSize=1}};Assert.Equal(issued,(await Create(paged)).Output);
        await using(var c=new NpgsqlCommand("SELECT plan_json->>'payload' FROM public.upload_validation_plans WHERE plan_id=$1",connection))
        {
            c.Parameters.AddWithValue(issued.PlanId!);using var payload=JsonDocument.Parse((string)(await c.ExecuteScalarAsync(Token))!);
            Assert.Equal(2,payload.RootElement.GetProperty("Actions").GetArrayLength());Assert.Equal(3,payload.RootElement.GetProperty("Guards").GetArrayLength());
            Assert.Equal(102,payload.RootElement.GetProperty("Actions")[0].GetProperty("Action").GetProperty("ExistingId").GetInt32());
            Assert.Equal(JsonValueKind.Null,payload.RootElement.GetProperty("Actions")[1].GetProperty("Values").ValueKind);
        }
        Assert.Equal("conflict",(await plans.CheckAsync(owner,id,new(issued.PlanId!,new string('0',64),request.Review),Token)).Status);
        await Sql("UPDATE public.client_data SET version=version+1 WHERE id=101");Assert.Equal("conflict",(await Check(issued,request.Review)).Status);
        Assert.Equal("conflict",(await Create(request)).Status);await Sql("UPDATE public.client_data SET version=version-1 WHERE id=101");
        await Sql($"UPDATE public.upload_validation_plans SET plan_json=jsonb_set(plan_json,'{{payload}}','\"tampered\"') WHERE plan_id='{issued.PlanId}'");
        Assert.Equal("conflict",(await Check(issued,request.Review)).Status);
        var second=(await Create(request with {RequestId=Guid.NewGuid()})).Output!;
        Assert.Equal("conflict",(await Check(issued,request.Review)).Status);Assert.Equal("conflict",(await Create(request)).Status);
        var parallel=await Task.WhenAll(Create(request with {RequestId=Guid.NewGuid()}),Create(request with {RequestId=Guid.NewGuid()}));
        Assert.All(parallel,r=>Assert.Equal("success",r.Status));Assert.Equal(1L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE status='ready'"));
        var same=request with {RequestId=Guid.NewGuid()};var retries=await Task.WhenAll(Create(same),Create(same));Assert.Equal(retries[0].Output,retries[1].Output);
        var active=retries[0].Output!;
        clock.Current=clock.Current.AddMinutes(31);Assert.Equal("conflict",(await Check(active,request.Review)).Status);clock.Current=DateTimeOffset.UtcNow;
        Assert.Equal("forbidden",(await Create(request with {RequestId=Guid.NewGuid()},other)).Status);
        Assert.Equal("not_found",(await Create(request with {RequestId=Guid.NewGuid()},staff)).Status);
        var noOp=await Create(new(Guid.NewGuid(),await Review("Base")));Assert.Equal("no_changes",noOp.Output!.Status);Assert.Null(noOp.Output.PlanId);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE status='ready'"));
        Assert.Equal("review",(await Create(new(Guid.NewGuid(),await Review("BindingReview")))).Status);
        // Configuration mutations already supersede rows in the common plan table.
        await Sql("UPDATE public.platform_tenant_columns SET display_name='Renamed memo' WHERE column_id=4");
        Assert.Equal("conflict",(await Create(request with {RequestId=Guid.NewGuid()})).Status);
        request=new(Guid.NewGuid(),await Review(manual:true));
        var boundedSettings=Settings();boundedSettings.MaximumPayloadBytes=4096;
        var bounded=new UploadValidationPlanService(corrections,store,signer,Options.Create(boundedSettings),clock);
        await Sql("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_4}',to_jsonb(repeat('m',6000))) WHERE id IN(101,102)");
        var large=new ValidationPlanRequest(Guid.NewGuid(),await Review("Large",true));
        Assert.Equal("plan_payload_capacity",(await bounded.CreateAsync(owner,id,large,Token)).Code);
        await Sql("UPDATE public.client_data SET row_data=jsonb_set(row_data,'{col_4}','\"Office  supplies\"') WHERE id IN(101,102)");
        var capacity=Settings();capacity.MaximumDatabaseBytes=1;
        var noSpace=new UploadValidationPlanService(corrections,new UploadValidationPlanStore(tx,Options.Create(capacity),clock),signer,Options.Create(capacity),clock);
        Assert.Equal("plan_storage_capacity",(await noSpace.CreateAsync(owner,id,request,Token)).Code);
        clock.OnRead=()=>{clock.OnRead=null;Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE user_id=1").GetAwaiter().GetResult();};
        Assert.Equal("forbidden",(await Create(request)).Status);await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE user_id=1");
        var fresh=(await Create(request)).Output!;
        var tinyRows=Settings();tinyRows.MaximumRows=2;
        Assert.Equal("plan_row_capacity",(await new UploadValidationPlanService(corrections,store,signer,Options.Create(tinyRows),clock).CreateAsync(owner,id,request with {RequestId=Guid.NewGuid()},Token)).Code);
        var receiptLimit=Settings();receiptLimit.MaximumReceiptsPerScope=1;
        var limitedStore=new UploadValidationPlanStore(tx,Options.Create(receiptLimit),clock);
        Assert.Equal("plan_receipt_capacity",(await new UploadValidationPlanService(corrections,limitedStore,signer,Options.Create(receiptLimit),clock).CreateAsync(owner,id,request with {RequestId=Guid.NewGuid()},Token)).Code);
        await Sql($"UPDATE public.upload_validation_plans SET expires_at=now()-interval '1 minute' WHERE plan_id='{fresh.PlanId}'");
        Assert.Equal("conflict",(await Check(fresh,request.Review)).Status);
        var metadata=(await Create(new(Guid.NewGuid(),await Review("Unchecked",true)))).Output!;
        Assert.Equal(1,metadata.Counts.InformationalUpdates);Assert.Equal(0,metadata.Counts.Changed);
        await using(var c=new NpgsqlCommand("SELECT plan_json->>'payload' FROM public.upload_validation_plans WHERE plan_id=$1",connection))
        {
            c.Parameters.AddWithValue(metadata.PlanId!);using var payload=JsonDocument.Parse((string)(await c.ExecuteScalarAsync(Token))!);
            var values=payload.RootElement.GetProperty("Actions")[0].GetProperty("Values");Assert.Single(values.EnumerateObject());Assert.True(values.TryGetProperty("col_5",out _));
        }
        var lateClock=new Clock();lateClock.OnRead=()=>{lateClock.OnRead=null;Sql("UPDATE public.client_data SET version=version+1 WHERE id=101").GetAwaiter().GetResult();};
        var late=new UploadValidationPlanService(corrections,new UploadValidationPlanStore(tx,settings,lateClock),signer,settings,clock);
        Assert.Equal("plan_data_changed",(await late.CreateAsync(owner,id,request with {RequestId=Guid.NewGuid()},Token)).Code);
        await Sql("UPDATE public.client_data SET version=version-1 WHERE id=101");
        await using(var proxy=fixture.CreateCommitProxy())
        using(var proxiedPool=fixture.CreatePool(proxy:proxy))
        {
            var proxied=new UploadValidationPlanService(corrections,new UploadValidationPlanStore(new PostgresTransactions(proxiedPool,Options.Create(new PostgresOptions())),settings,clock),signer,settings,clock);
            var lost=request with {RequestId=Guid.NewGuid()};proxy.Arm();
            var recovered=await proxied.CreateAsync(owner,id,lost,Token);await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Token);
            Assert.True(recovered.Status=="success"||recovered.Status=="conflict"&&recovered.Code=="plan_outcome_unknown_retry_same_request",$"{recovered.Status}: {recovered.Code}");
            var retried=await Create(lost);Assert.Equal("success",retried.Status);
            if(recovered.Output is not null) Assert.Equal(recovered.Output,retried.Output);
            Assert.Equal(1L,await Count($"SELECT count(*) FROM public.platform_upload_validation_plans WHERE request_id='{lost.RequestId}'"));
        }
        Assert.Equal(canonical,await Canonical());Assert.Equal(audits,await Count("SELECT count(*) FROM public.audit_log"));
        request=request with {RequestId=Guid.NewGuid()};
        await using(var disabled=new WebApplicationFactory<Program>()) using(var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.PostAsJsonAsync($"/api/upload/file/{id}/validation-plan",request,Token)).StatusCode);
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
        })))
        {
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
            string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
            string route=$"/api/upload/file/{id}/validation-plan";client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);client.DefaultRequestHeaders.Remove("Origin");
            Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
            var response=await client.PostAsJsonAsync(route,request,Token);Assert.Equal(HttpStatusCode.OK,response.StatusCode);Assert.Contains("no-store",response.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
            Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsJsonAsync(route,request with {RequestId=Guid.Empty},Token)).StatusCode);
            string bad=JsonSerializer.Serialize(request,new JsonSerializerOptions(JsonSerializerDefaults.Web)).Replace("\"controlTotalRow\":5","\"controlTotalRow\":\"5\"",StringComparison.Ordinal);
            using(var body=new StringContent(bad,System.Text.Encoding.UTF8,"application/json")) Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(route,body,Token)).StatusCode);
            using(var oversized=new HttpRequestMessage(HttpMethod.Post,route))
            {
                oversized.Headers.TransferEncodingChunked=true;oversized.Content=new StringContent("{\"padding\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json");
                Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(oversized,Token)).StatusCode);
            }
            Assert.Equal(HttpStatusCode.OK,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
        }


        Assert.Equal(canonical,await Canonical());Assert.Equal(audits,await Count("SELECT count(*) FROM public.audit_log"));
        Assert.Equal(golden.Oracle.RootElement.GetProperty("sha256").GetString(),Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(Path.Combine(golden.DirectoryPath,"plans.xlsx")))).ToLowerInvariant());
        await Sql("SELECT pg_advisory_lock(746102)");
        try
        {
            var shortOptions=Settings();shortOptions.MaximumConcurrentRequests=1;shortOptions.TimeoutSeconds=1;
            var shortPlans=new UploadValidationPlanService(corrections,store,signer,Options.Create(shortOptions),clock);
            var waiting=shortPlans.CreateAsync(owner,id,request with {RequestId=Guid.NewGuid()},Token);
            Assert.Equal("busy",(await shortPlans.CreateAsync(owner,id,request with {RequestId=Guid.NewGuid()},Token)).Status);
            Assert.Equal("timeout",(await waiting).Status);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Token);
            var stopped=shortPlans.CreateAsync(owner,id,request with {RequestId=Guid.NewGuid()},cancelled.Token);await cancelled.CancelAsync();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>stopped);
        }
        finally {await Sql("SELECT pg_advisory_unlock(746102)");}
        await Sql($"UPDATE public.uploaded_files SET status='Imported' WHERE id={id}");
        Assert.Equal("plan_source_unavailable",(await Create(request with {RequestId=Guid.NewGuid()})).Code);
        await Sql($"UPDATE public.uploaded_files SET status='Pending' WHERE id={id}");
        await Sql("""
            UPDATE public.master_columns SET data_type='Numeric' WHERE id=5;
            UPDATE public.platform_tenant_columns SET display_name='Credit' WHERE column_id=5;
            UPDATE public.platform_tenant_columns SET display_name='Balance' WHERE column_id=6;
            UPDATE public.templates SET data_purpose='account_ledger',ledger_columns='{"voucher":1,"date":2,"description":4,"debit":3,"credit":5,"balance":6}' WHERE id=1;
            CREATE TABLE public.trial_balance_uploads(id INTEGER PRIMARY KEY,client_id INTEGER,period_end DATE,uploaded_at TIMESTAMPTZ,status TEXT);
            CREATE TABLE public.trial_balance_accounts(upload_id INTEGER,account_code TEXT,account_name TEXT,classification TEXT);
            INSERT INTO public.trial_balance_uploads VALUES(1,10,'2026-04-30',now(),'parsed');
            INSERT INTO public.trial_balance_accounts VALUES(1,'A-1','Cash','leaf');
            """);
        var ledger=new CorrectionRequest((await Bound("Ledger",true,true)).Coverage);
        ledger=ledger with {Coverage=ledger.Coverage with {LedgerAccountCode="A-1",LedgerPeriodStart="2026-04-01",LedgerPeriodEnd="2026-04-30",
            Footing=ledger.Coverage.Footing with {Validation=ledger.Coverage.Footing.Validation with {ControlTotalRow=6}}}};
        Assert.Equal("ledger_plan_proof_required",(await Create(new(Guid.NewGuid(),ledger))).Code);
        await Sql("""
            UPDATE public.master_columns SET data_type='Text' WHERE id=5;
            UPDATE public.platform_tenant_columns SET display_name='Extra' WHERE column_id=6;
            UPDATE public.platform_tenant_columns SET display_name='Balance' WHERE column_id=5;
            UPDATE public.templates SET data_purpose='standard',ledger_columns='{}' WHERE id=1;
            """);
        request=new(Guid.NewGuid(),await Review(manual:true));
        clock.Current=clock.Current.AddHours(3);Assert.Equal("gone",(await Create(request with {RequestId=Guid.NewGuid()})).Status);clock.Current=DateTimeOffset.UtcNow;
        foreach(string path in Directory.GetFiles(directory,"*.bin")) await File.WriteAllTextAsync(path,"tampered",Token);
        Assert.Equal("gone",(await Create(request with {RequestId=Guid.NewGuid()})).Status);
    }
}
