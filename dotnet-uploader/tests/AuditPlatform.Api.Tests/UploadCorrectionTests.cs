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

public sealed class CorrectionGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public ValueTask InitializeAsync() {
        DirectoryPath = NativeCompatibilityFixtureFactory.CreateWorkspace("p23", "p23-tests");
        Oracle = NativeCompatibilityFixtureFactory.ReadOracle(DirectoryPath);
        return ValueTask.CompletedTask;
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadCorrectionTests(CorrectionGoldenFixture golden) : IClassFixture<CorrectionGoldenFixture>
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

    private static CorrectionOutput Evaluate(IdentityProfile profile,IReadOnlyList<IdentitySourceRow> uploaded,IReadOnlyList<IdentityStoredRow> stored,
        CorrectionRequest? request=null,CorrectionBindings? bindings=null,IReadOnlySet<string>? incomplete=null,
        IReadOnlyDictionary<string,List<long>>? collisions=null,UploadCorrectionOptions? limits=null)
    {
        request??=new(Request().Coverage);
        return UploadCorrectionService.Build(profile,uploaded,stored,request,Proof(new(request.Coverage)),"store",
            collisions??new Dictionary<string,List<long>>(),incomplete??new HashSet<string>(),bindings??new(AllowSingleFallback:true),limits??new(),Token);
    }

    [Fact]
    public void ReferenceCorrectionVectorsPreserveCountsIdsFieldsAndOccurrenceMultiplicity()
    {
        foreach(var vector in golden.Oracle.RootElement.GetProperty("vectors").EnumerateArray())
        {
            var columns=vector.GetProperty("columns").EnumerateArray().Select(c=>new IdentityColumn(c[0].GetString()!,c[1].GetString()!,Description:c[0].GetString()=="Description")).ToArray();
            var profile=new IdentityProfile(7,columns,"Document","Date",vector.GetProperty("line").GetBoolean()?"Line ID":null);
            var incoming=vector.GetProperty("incoming").EnumerateArray().Select((r,i)=>new IdentitySourceRow(i+2,UploadIdentityValue.FromJson(r.GetRawText()),"2026-04-03")).ToArray();
            var existing=vector.GetProperty("current").EnumerateArray().Select((r,i)=>new IdentityStoredRow(100+i,UploadIdentityValue.FromJson(r.GetRawText()),
                "7-"+r.GetProperty("Document").GetString(),"keep-"+i,3,i+2,9,"2026-04-03")).ToArray();
            bool account=vector.GetProperty("account").GetBoolean();
            var request=new CorrectionRequest(Request(mode:vector.GetProperty("mode").GetString()!).Coverage);
            var result=Evaluate(profile,incoming,existing,request,new(account?"Account Code":null,account?"Debit":null,account?"Credit":null,AllowSingleFallback:!account));
            if(vector.GetProperty("ambiguous").GetBoolean())
            {Assert.False(result.CorrectionChecksPassed,vector.GetProperty("label").GetString());Assert.Contains(result.Issues,i=>i.Kind=="ambiguous_correction");continue;}
            Assert.True(result.CorrectionChecksPassed,vector.GetProperty("label").GetString());Assert.False(result.CanImport);
            Assert.Equal(vector.GetProperty("unchanged").GetInt32(),result.Counts.Unchanged);
            Assert.Equal(vector.GetProperty("changed").GetInt32(),result.Counts.Changed);
            Assert.Equal(vector.GetProperty("added").GetInt32(),result.Counts.Added);
            Assert.Equal(vector.GetProperty("removed").GetInt32(),result.Counts.Removed);
            foreach(var expected in vector.GetProperty("changes").EnumerateArray())
            {
                var changed=Assert.Single(result.Rows,r=>r.Action=="changed"&&r.ExistingId==expected.GetProperty("id").GetInt64());
                Assert.Equal(expected.GetProperty("fields").EnumerateArray().Select(f=>f.GetString()),changed.ChangedFields);
                Assert.Equal("keep-"+(changed.ExistingId-100),changed.Identity);Assert.Equal(3,changed.ExpectedVersion);
            }
            Assert.Equal(vector.GetProperty("removed_ids").EnumerateArray().Select(v=>v.GetInt64()).Order(),result.Rows.Where(r=>r.Action=="removed").Select(r=>r.ExistingId!.Value).Order());
        }
    }

    [Fact]
    public void MissingBindingsCollisionsAndVoucherBoundariesNeverCertifyCorrections()
    {
        var profile=Profile();var values=UploadIdentityValue.FromJson("{\"Document\":\"V1\",\"Amount\":100,\"Description\":\"Old\"}");
        var old=new IdentityStoredRow(100,values,"7-V1","preserved",3,2,9);
        var incoming=new IdentitySourceRow(2,UploadIdentityValue.FromJson("{\"Document\":\"V1\",\"Amount\":107,\"Description\":\"New\"}"));
        var gated=Evaluate(profile,[incoming],[old],bindings:new(RequiresBindingReview:true));
        Assert.False(gated.CorrectionChecksPassed);Assert.Contains(gated.Issues,i=>i.Kind=="correction_binding_review_required");
        var collision=Evaluate(profile,[incoming],[old],collisions:new Dictionary<string,List<long>>{{UploadIdentityValue.SourceIdentity(profile,incoming.Values),[999]}});
        Assert.False(collision.CorrectionChecksPassed);Assert.Contains(collision.Issues,i=>i.Kind=="active_identity_collision");
        // A proposed identity held only by the corrected row is legitimate.
        Assert.True(Evaluate(profile,[incoming],[old],collisions:new Dictionary<string,List<long>>{{UploadIdentityValue.SourceIdentity(profile,incoming.Values),[100]}}).CorrectionChecksPassed);
        var boundary=Evaluate(profile,[],[old],incomplete:new HashSet<string>{"7-V1"});
        Assert.False(boundary.CorrectionChecksPassed);Assert.Contains(boundary.Issues,i=>i.Kind=="voucher_outside_coverage");
        Assert.False(Evaluate(profile,[incoming],[old with {Identity=null}]).CorrectionChecksPassed);
        var line=Profile(true);var lineValues=UploadIdentityValue.FromJson("{\"Document\":\"V1\",\"Line ID\":\"A\",\"Amount\":100}");
        var duplicates=Evaluate(line,[new(2,lineValues),new(3,lineValues)],[]);
        Assert.Contains(duplicates.Issues,i=>i.Kind=="duplicate_line_id");Assert.False(duplicates.CorrectionChecksPassed);
        var manyDuplicates=Enumerable.Range(1,20).SelectMany(i=>new[]{
            new IdentitySourceRow(i*2,UploadIdentityValue.FromJson($"{{\"Document\":\"V{i}\",\"Line ID\":\"A\",\"Amount\":100}}")),
            new IdentitySourceRow(i*2+1,UploadIdentityValue.FromJson($"{{\"Document\":\"V{i}\",\"Line ID\":\"A\",\"Amount\":100}}"))}).ToArray();
        var bounded=Evaluate(line,manyDuplicates,[],limits:new(){MaximumIssueDetails=2});
        Assert.Equal(20,bounded.IssueCount);Assert.Equal(2,bounded.Issues.Count);Assert.True(bounded.DetailsTruncated);Assert.False(bounded.CorrectionChecksPassed);
        var calendar=UploadIdentityValue.FromJson("{\"Document\":\"V1\",\"Date\":\"03/04/2026\",\"Amount\":100}");
        var dateChange=Evaluate(profile,[new(2,calendar,"2026-04-03")],[old with {Values=calendar,PrimaryDate="2026-03-04"}]);
        Assert.Equal(1,dateChange.Counts.Changed);Assert.Equal(new[]{"Date"},Assert.Single(dateChange.Rows).ChangedFields);
        var ledger=profile with {AccountCode="A-1"};
        Assert.False(Evaluate(ledger,[new(2,values)],[old with {DocumentGroup="7-ledger:A-1:V1"}]).NoOp);
    }

    [Fact]
    public void ReviewKeysBindUnpagedActionsAndMetadataWhileDetailsStayBounded()
    {
        var profile=Profile();var values=UploadIdentityValue.FromJson("{\"Document\":\"V1\",\"Amount\":100,\"Description\":\"Same\",\"Balance\":\"100 DR\"}");
        var incoming=new IdentitySourceRow(2,values);var old=new IdentityStoredRow(100,values,"7-V1","keep",3,2,9);
        var initial=Evaluate(profile,[incoming],[old]);Assert.True(initial.NoOp);
        var request=new CorrectionRequest(Request().Coverage,initial.CorrectionKey);
        Assert.True(Evaluate(profile,[incoming],[old],request).NoOp);
        Assert.Equal("confirmation_changed",Evaluate(profile,[incoming],[old with {Version=4}],request).Status);
        var metadata=Evaluate(profile,[incoming],[old with {SourceRow=3}]);Assert.False(metadata.NoOp);Assert.Equal(1,metadata.Counts.SourceOrderUpdates);
        var informational=Evaluate(profile,[incoming with {Values=UploadIdentityValue.FromJson("{\"Document\":\"V1\",\"Amount\":100,\"Description\":\"Same\",\"Balance\":\"80 DR\"}")}],[old]);
        Assert.True(informational.NoTransactionChanges);Assert.False(informational.NoOp);Assert.Equal(1,informational.Counts.InformationalUpdates);
        var repeated=Enumerable.Range(2,200).Select(i=>new IdentitySourceRow(i,values)).ToArray();
        var paged=Evaluate(profile,repeated,[]);Assert.Equal(200,paged.Counts.Added);Assert.Equal(50,paged.Rows.Count);
        var firstPage=Evaluate(profile,repeated,[],new(Request().Coverage,PageSize:100));
        var secondPage=Evaluate(profile,repeated,[],new(Request().Coverage,Page:2,PageSize:100));
        Assert.Equal(firstPage.CorrectionKey,secondPage.CorrectionKey);
        Assert.Equal(200,firstPage.Rows.Concat(secondPage.Rows).Select(r=>r.Identity).Distinct().Count());
        Assert.Throws<IdentityReview>(()=>Evaluate(profile,repeated,[],limits:new(){MaximumRows=10}));
        Assert.Throws<IdentityReview>(()=>Evaluate(profile,repeated,[],limits:new(){MaximumComparisonBytes=4096}));
        using var cancelled=new CancellationTokenSource();cancelled.Cancel();
        Assert.ThrowsAny<OperationCanceledException>(()=>UploadCorrectionService.Build(profile,[incoming],[old],new(Request().Coverage),Proof(Request()),"store",
            new Dictionary<string,List<long>>(),new HashSet<string>(),new(),new(),cancelled.Token));
    }

    [Fact]
    public async Task StartupRequiresIdentityAndValidCorrectionLimits()
    {
        await using(var missing=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(
            services=>services.PostConfigure<UploadCorrectionOptions>(o=>o.Enabled=true))))
            Assert.Throws<OptionsValidationException>(()=>missing.CreateClient());
        await using(var invalid=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(
            services=>services.PostConfigure<UploadCorrectionOptions>(o=>o.MaximumRows=0))))
            Assert.Throws<OptionsValidationException>(()=>invalid.CreateClient());
    }

    [Fact]
    public async Task OwnedCorrectionSnapshotAndHttpReviewPreserveCanonicalData()
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
            CREATE TABLE public.upload_validation_plans(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id INTEGER,template_id INTEGER,status TEXT);
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
        string directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P23-"+Guid.NewGuid().ToString("N"));var clock=new Clock();
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
        using var stream=File.OpenRead(Path.Combine(golden.DirectoryPath,"corrections.xlsx"));
        var receipt=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",stream,Token);Assert.Equal("success",receipt.Status);int id=receipt.FileId!.Value;
        async Task<IdentityRequest> Bound(string sheet="Base",bool manual=false,bool all=false)
        {
            var source=new PreparationRequest(10,1,sheet,RequestId:Guid.NewGuid(),Mappings:manual?
                [new(1,1),new(2,2),new(3,3),new(4,4),new(5,5),new(6,all?6:null)]:null);
            var prepared=(await preparation.PrepareAsync(owner,id,source,Token)).Output!;
            source=source with {Sha256=prepared.Context.Sha256,ConfigurationKey=prepared.Context.ConfigurationKey,MappingRevision=prepared.Context.MappingRevision};
            return Request(sheet) with {Coverage=Request(sheet).Coverage with {Footing=new(new(source,"replace_range",5),true)}};
        }

        // P23-specific scenarios below use this same owned cluster and source boundary.
        var corrections=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true}));
        async Task<CorrectionRequest> BoundCorrection(string sheet="Base",bool manual=false)
            =>new((await Bound(sheet,manual)).Coverage);
        async Task<CorrectionResult> Run(CorrectionRequest r,VerifiedSession? actor=null)=>await corrections.CheckAsync(actor??owner,id,r,Token);
        async Task<string> Canonical()
        {await using var command=new NpgsqlCommand("SELECT jsonb_agg(to_jsonb(cd) ORDER BY id)::text FROM public.client_data cd",connection);return (string)(await command.ExecuteScalarAsync(Token))!;}
        var request=await BoundCorrection();long audits=await Count("SELECT count(*) FROM public.audit_log");string canonical=await Canonical();
        var initial=await Run(request);Assert.Equal("success",initial.Status);Assert.True(initial.Output!.NoOp);Assert.False(initial.Output.CanImport);
        Assert.Equal(3,initial.Output.Counts.Unchanged);
        var changed=await Run(await BoundCorrection("Changed"));Assert.True(changed.Output!.CorrectionChecksPassed);
        Assert.Equal(1,changed.Output.Counts.Changed);Assert.Equal(2,changed.Output.Counts.Unchanged);
        var changedRow=Assert.Single(changed.Output.Rows,r=>r.Action=="changed");Assert.Equal(102,changedRow.ExistingId);
        Assert.Equal("old-102",changedRow.Identity);Assert.Equal(new[]{"col_3"},changedRow.ChangedFields);
        var review=await Run(await BoundCorrection("BindingReview"));Assert.False(review.Output!.CorrectionChecksPassed);
        Assert.Contains(review.Output.Issues,i=>i.Kind=="correction_binding_review_required");
        var empty=await BoundCorrection("Empty");empty=empty with {Coverage=empty.Coverage with {ConfirmEmptyPeriod=true,
            Footing=empty.Coverage.Footing with {Validation=empty.Coverage.Footing.Validation with {ControlTotalRow=2}}}};
        var removal=await Run(empty);Assert.True(removal.Output!.CorrectionChecksPassed);Assert.Equal(3,removal.Output.Counts.Removed);
        var partial=request with {Coverage=request.Coverage with {CoverageStart=null,CoverageEnd=null,ConfirmReportCoverage=false,
            Footing=request.Coverage.Footing with {Validation=request.Coverage.Footing.Validation with {ComparisonMode="correction"}}}};
        Assert.Equal(0,(await Run(partial)).Output!.Counts.Removed);
        await Sql("INSERT INTO public.client_data(id,client_id,template_id,row_data,document_group_id,source_identity,extract_date) OVERRIDING SYSTEM VALUE VALUES(104,10,1,'{\"col_1\":\"ABSENT\",\"col_2\":\"03/04/2026\",\"col_3\":10}','1-ABSENT','absent-in','2026-04-03'),(105,10,1,'{\"col_1\":\"ABSENT\",\"col_2\":\"03/03/2026\",\"col_3\":20}','1-ABSENT','absent-out','2026-03-03')");
        var boundary=await Run(request);Assert.False(boundary.Output!.CorrectionChecksPassed);
        Assert.Contains(boundary.Output.Issues,i=>i.Kind=="voucher_outside_coverage");
        Assert.Equal(0,(await Run(partial)).Output!.Counts.Removed);await Sql("DELETE FROM public.client_data WHERE id IN (104,105)");
        Assert.Equal("forbidden",(await Run(request,other)).Status);Assert.Equal("not_found",(await Run(request,staff)).Status);
        Assert.Equal("conflict",(await Run(request with {ExpectedCorrectionKey=new string('0',64)})).Status);
        Assert.True((await Run(request with {ExpectedCorrectionKey=initial.Output.CorrectionKey})).Output!.NoOp);
        await Sql("UPDATE public.client_data SET version=version+1 WHERE id=101");
        Assert.Equal("conflict",(await Run(request with {ExpectedCorrectionKey=initial.Output.CorrectionKey})).Status);
        await Sql("UPDATE public.client_data SET version=version-1 WHERE id=101");
        var limited=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true,MaximumRows=2}));
        Assert.Equal("comparison_capacity_review_required",(await limited.CheckAsync(owner,id,request,Token)).Code);
        clock.OnRead=()=>{clock.OnRead=null;Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE user_id=1").GetAwaiter().GetResult();};
        Assert.Equal("forbidden",(await Run(request)).Status);await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE user_id=1");
        await using(var held=await connection.BeginTransactionAsync(Token))
        {
            await using(var block=new NpgsqlCommand("LOCK TABLE public.client_data IN ACCESS EXCLUSIVE MODE",connection,held))
                await block.ExecuteNonQueryAsync(Token);
            var bounded=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true,MaximumConcurrentRequests=1,TimeoutSeconds=1}));
            var waiting=bounded.CheckAsync(owner,id,request,Token);
            Assert.Equal("busy",(await bounded.CheckAsync(owner,id,request,Token)).Status);
            Assert.Equal("timeout",(await waiting).Status);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Token);
            var interrupted=bounded.CheckAsync(owner,id,request,cancelled.Token);await cancelled.CancelAsync();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>interrupted);
            await held.RollbackAsync(Token);
        }
        await Sql("""
            ALTER TABLE public.client_data RENAME TO p23_current_source;
            CREATE FUNCTION public.p23_write_attempt(value JSONB) RETURNS JSONB LANGUAGE plpgsql VOLATILE AS $$
            BEGIN UPDATE public.p23_current_source SET version=version+1 WHERE id=101;RETURN value;END $$;
            CREATE VIEW public.client_data AS SELECT id,client_id,template_id,public.p23_write_attempt(row_data) AS row_data,version,document_group_id,
                source_identity,source_row_number,source_order_upload_id,extract_date,is_deleted,change_detection_status,upload_id FROM public.p23_current_source;
            """);
        var readOnly=await Assert.ThrowsAsync<PostgresException>(()=>Run(request));Assert.Equal("25006",readOnly.SqlState);
        await Sql("DROP VIEW public.client_data; ALTER TABLE public.p23_current_source RENAME TO client_data");
        Assert.Equal(canonical,await Canonical());
        await using(var disabled=new WebApplicationFactory<Program>()) using(var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.PostAsJsonAsync($"/api/upload/file/{id}/corrections",request,Token)).StatusCode);
        await using(var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);services.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true);services.PostConfigure<UploadIdentityOptions>(o=>o.Enabled=true);services.PostConfigure<UploadCorrectionOptions>(o=>o.Enabled=true);
        })))
        {
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
            string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
            string route=$"/api/upload/file/{id}/corrections";client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);client.DefaultRequestHeaders.Remove("Origin");
            Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
            var response=await client.PostAsJsonAsync(route,request,Token);Assert.Equal(HttpStatusCode.OK,response.StatusCode);Assert.Contains("no-store",response.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
            Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,request with {ExpectedCorrectionKey=new string('0',64)},Token)).StatusCode);
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
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.upload_validation_plans"));
        await Sql("""
            UPDATE public.master_columns SET data_type='Numeric' WHERE id=5;
            UPDATE public.platform_tenant_columns SET display_name='Credit' WHERE column_id=5;
            UPDATE public.platform_tenant_columns SET display_name='Balance' WHERE column_id=6;
            UPDATE public.templates SET data_purpose='account_ledger',ledger_columns='{"voucher":1,"date":2,"description":4,"debit":3,"credit":5,"balance":6}' WHERE id=1;
            CREATE TABLE public.trial_balance_uploads(id INTEGER PRIMARY KEY,client_id INTEGER,period_end DATE,uploaded_at TIMESTAMPTZ,status TEXT);
            CREATE TABLE public.trial_balance_accounts(upload_id INTEGER,account_code TEXT,account_name TEXT,classification TEXT);
            INSERT INTO public.trial_balance_uploads VALUES(1,10,'2026-04-30',now(),'parsed');
            INSERT INTO public.trial_balance_accounts VALUES(1,'A-1','Cash','leaf'),(1,'A-2','Other','leaf');
            INSERT INTO public.uploaded_files(id,file_name,file_path,file_type,record_count,status,uploaded_by,client_id,template_id,ledger_account_code)
            OVERRIDING SYSTEM VALUE VALUES(300,'synthetic','','excel',1,'Imported','owner',10,1,'A-1'),(301,'synthetic','','excel',1,'Imported','owner',10,1,'A-2');
            INSERT INTO public.client_data(id,client_id,template_id,upload_id,row_data,document_group_id,source_identity,source_row_number,source_order_upload_id,extract_date)
            OVERRIDING SYSTEM VALUE VALUES(201,10,1,300,
            '{"col_1":"V1","col_2":"03/04/2026","col_3":10,"col_4":"Payment","col_5":0,"col_6":"110 DR","Ledger Source Notes":"Invoice # 1"}',
            '1-ledger:A-1:V1','ledger-old',3,300,'2026-04-03'),
            (203,10,1,301,'{"col_1":"V1","col_2":"03/04/2026","col_3":10,"col_4":"Other","col_5":0,"col_6":"110 DR"}',
            '1-ledger:A-2:V1','other-account',2,301,'2026-04-03');
            """);
        var ledger=new CorrectionRequest((await Bound("Ledger",true,true)).Coverage);
        ledger=ledger with {Coverage=ledger.Coverage with {LedgerAccountCode="A-1",LedgerPeriodStart="2026-04-01",LedgerPeriodEnd="2026-04-30",
            Footing=ledger.Coverage.Footing with {Validation=ledger.Coverage.Footing.Validation with {ControlTotalRow=6}}}};
        var ledgerResult=await Run(ledger);Assert.True(ledgerResult.Output!.NoTransactionChanges);Assert.False(ledgerResult.Output.NoOp);
        Assert.Equal(1,ledgerResult.Output.Counts.Existing);Assert.Equal("ledger-old",ledgerResult.Output.Rows.Single().Identity);
        await Sql("INSERT INTO public.client_data(id,client_id,template_id,upload_id,row_data,document_group_id,source_identity,extract_date) OVERRIDING SYSTEM VALUE VALUES(202,10,1,300,'{\"col_1\":\"V1\",\"col_2\":\"03/03/2026\",\"col_3\":10}','1-ledger:A-1:V1','ledger-out','2026-03-03')");
        var ledgerBoundary=await Run(ledger);Assert.False(ledgerBoundary.Output!.CorrectionChecksPassed);
        Assert.Contains(ledgerBoundary.Output.Issues,i=>i.Kind=="voucher_outside_coverage");
        await Sql("DELETE FROM public.client_data WHERE id IN (201,202,203); DELETE FROM public.uploaded_files WHERE id IN (300,301)");
        await Sql("""
            UPDATE public.master_columns SET data_type='Text' WHERE id=5;
            UPDATE public.platform_tenant_columns SET display_name='Extra' WHERE column_id=6;
            UPDATE public.platform_tenant_columns SET display_name='Balance' WHERE column_id=5;
            UPDATE public.templates SET data_purpose='standard',ledger_columns='{}' WHERE id=1;
            """);
        request=await BoundCorrection(manual:true);
        Assert.Equal(canonical,await Canonical());Assert.Equal(audits,await Count("SELECT count(*) FROM public.audit_log"));
        Assert.Equal(golden.Oracle.RootElement.GetProperty("sha256").GetString(),Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(Path.Combine(golden.DirectoryPath,"corrections.xlsx")))).ToLowerInvariant());
        clock.Current=clock.Current.AddHours(3);Assert.Equal("gone",(await Run(request)).Status);clock.Current=DateTimeOffset.UtcNow;
        foreach(string path in Directory.GetFiles(directory,"*.bin")) await File.WriteAllTextAsync(path,"tampered",Token);
        Assert.Equal("gone",(await Run(request)).Status);
    }
}
