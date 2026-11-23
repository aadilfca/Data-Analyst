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

public sealed class CoverageGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public ValueTask InitializeAsync() {
        DirectoryPath = NativeCompatibilityFixtureFactory.CreateWorkspace("p21", "p21-tests");
        Oracle = NativeCompatibilityFixtureFactory.ReadOracle(DirectoryPath);
        return ValueTask.CompletedTask;
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadCoverageTests(CoverageGoldenFixture golden) : IClassFixture<CoverageGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static TemplateDefinition Definition(bool footing=false,bool ledger=false,bool dateRole=true)=>
        new(1,"Fixture","Admin",new DateTime(2026,1,1),false,false,ledger?"account_ledger":"standard",
            ledger?new(){{"voucher",1},{"date",2},{"description",4},{"debit",3},{"credit",5},{"balance",6}}:[],null,"standard","native",1,
            new[]{("Document","Text",(string?)"document_id"),("Date","Date",dateRole?"document_date":null),
                ("Amount","Numeric",footing?"footing":null),("Description","Text",null),("Credit","Numeric",null),("Balance","Text",null)}
                .Select((c,i)=>new TemplateColumn(i+1,c.Item1,"col_"+(i+1),c.Item2,"General",c.Item3,null,false,i,1,true)).ToArray());
    private CoverageRequest Request(string sheet="Base",bool footing=true,int? control=4,string mode="replace_range",bool ledger=false)=>
        new(new(new(new(10,1,sheet,"rows",RequestId:Guid.NewGuid()),mode,footing?control:null),footing),
            "2026-04-01","2026-04-30",true,LedgerAccountCode:ledger?"A-1":null,
            LedgerPeriodStart:ledger?"2026-04-01":null,LedgerPeriodEnd:ledger?"2026-04-30":null);
    private CoverageOutput Inspect(CoverageRequest request,bool configured=true,bool ledger=false,bool dateRole=true,bool mapped=true,
        CoverageLedger? reference=null,UploadCoverageOptions? limits=null)
    {
        var template=Definition(configured,ledger,dateRole);
        var rows=WorkbookReader.Read(Path.Combine(golden.DirectoryPath,"coverage.xlsx"),".xlsx",request.Footing.Validation.Source.Sheet,ct:Token).ToArray();
        var maps=UploadPreparationService.AutoMap(template,UploadPreparationService.Headers(rows[0].Cells));
        if(!mapped) maps=maps.Select(m=>m.ColumnId==2?m with {SourcePosition=null}:m).ToArray();
        var source=request.Footing.Validation.Source;
        string selection=Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(new {source.Sheet,source.DateOrder,source.DateDelimiter,source.ExcludedRows,source.ForceIncludeRows})));
        var context=new PreparationContext(source.RequestId,1,10,1,source.Sheet,"hash",template.ConfigurationKey,0,selection);
        return UploadCoverageService.Evaluate(template,maps,context,request,rows.Skip(1),limits??new(),new(),new(),reference,Token);
    }

    [Fact]
    public void CompleteDatesAndSuggestionsMatchActualPythonCoverageHelpers()
    {
        foreach(var vector in golden.Oracle.RootElement.GetProperty("vectors").EnumerateArray())
        {
            string label=vector.GetProperty("label").GetString()!;
            string sheet=label is "SingleDay" or "Hyphen" or "Native" or "Serial"?label:"Base";
            bool footing=sheet=="Base";
            var request=Request(sheet,footing) with {CoverageStart=vector.GetProperty("start").GetString(),CoverageEnd=vector.GetProperty("end").GetString(),
                ConfirmReportCoverage=vector.GetProperty("confirmed").GetBoolean()};
            var source=request.Footing.Validation.Source with {DateOrder=vector.GetProperty("order").GetString(),DateDelimiter=vector.GetProperty("delimiter").GetString()};
            request=request with {Footing=request.Footing with {Validation=request.Footing.Validation with {Source=source}}};
            var result=Inspect(request,footing,dateRole:vector.GetProperty("role").GetBoolean(),mapped:vector.GetProperty("mapped").GetBoolean());
            Assert.Equal(vector.GetProperty("passed").GetBoolean(),result.CoverageChecksPassed);
            Assert.False(result.CanImport);
            if(result.CoverageChecksPassed)
            {
                Assert.Equal(request.CoverageStart,result.DeclaredPeriod!.Start);Assert.Equal(request.CoverageEnd,result.DeclaredPeriod.End);
                var suggestion=vector.GetProperty("suggestion");
                Assert.Equal(suggestion.GetProperty("start").GetString(),result.DateSuggestion!.Start);
                Assert.Equal(suggestion.GetProperty("end").GetString(),result.DateSuggestion.End);
                Assert.Equal(suggestion.GetProperty("transaction_dates").GetInt32(),result.DateSuggestion.TransactionDates);
            }
        }
    }

    [Fact]
    public void EmptyCompletePeriodsCannotBeManufacturedByExcludedRejectedOrInvalidRows()
    {
        Assert.Equal("coverage_checks_failed",Inspect(Request("Empty",false),false).Status);
        var empty=Inspect(Request("Empty",false) with {ConfirmEmptyPeriod=true},false);
        Assert.True(empty.EmptyPeriodConfirmed);Assert.Equal("empty_complete_period",empty.Status);
        Assert.Equal("empty_complete_period",Inspect(Request("EmptyControl",control:2) with {ConfirmEmptyPeriod=true}).Status);
        Assert.False(Inspect(Request("BadEmptyControl",control:2) with {ConfirmEmptyPeriod=true}).CoverageChecksPassed);
        foreach(string sheet in new[]{"Rejected","BadDate","SingleDay"})
        {
            var request=Request(sheet,false) with {ConfirmEmptyPeriod=true};
            Assert.False(Inspect(request,false).CoverageChecksPassed);
            var excluded=request with {Footing=request.Footing with {Validation=request.Footing.Validation with
                {Source=request.Footing.Validation.Source with {ExcludedRows=[2]}}}};
            Assert.False(Inspect(excluded,false).CoverageChecksPassed);
        }
        var forced=Request("Rejected",false);forced=forced with {Footing=forced.Footing with {Validation=forced.Footing.Validation with
            {Source=forced.Footing.Validation.Source with {ForceIncludeRows=[2]}}}};
        Assert.False(Inspect(forced,false).CoverageChecksPassed);
    }

    [Fact]
    public void PartialExtractsPreserveOccurrencesAndNeverDeclareCompleteReplacement()
    {
        var request=Request(mode:"correction");request=request with {Footing=request.Footing with {Validation=request.Footing.Validation with
            {Source=request.Footing.Validation.Source with {ExcludedRows=[2]}}}};
        var subset=Inspect(request);Assert.True(subset.CoverageChecksPassed);Assert.False(subset.CompletePeriodDeclared);
        Assert.Null(subset.DeclaredPeriod);Assert.Equal(2,subset.Footing.FullSheet.RowCount);Assert.Equal(1,subset.Footing.SelectedDetail.RowCount);
        Assert.Equal(2,subset.DateSuggestion!.TransactionDates);Assert.Equal(1,subset.DateSuggestion.SelectedDates);
        var repeated=Inspect(Request("Repeated",false,mode:"correction"),false);
        Assert.Equal(3,repeated.Footing.SelectedDetail.RowCount);Assert.Equal(2,repeated.Footing.SelectedDetail.DocumentCount);
        Assert.Equal("partial_extract",repeated.Status);Assert.False(repeated.CanImport);
    }

    [Fact]
    public void CoverageReviewAndVarianceAcknowledgementBindPeriodAccountAndChoices()
    {
        var request=Request("Variance");var pending=Inspect(request);
        Assert.Equal("confirmation_required",pending.Footing.Status);Assert.False(pending.CoverageChecksPassed);
        var accepted=request with {ExpectedCoverageKey=pending.CoverageKey,Footing=request.Footing with
            {ConfirmVariance=true,ExpectedCheckKey=pending.Footing.CheckKey,Validation=request.Footing.Validation with
                {Source=request.Footing.Validation.Source with {RequestId=Guid.NewGuid()}}}};
        Assert.True(Inspect(accepted).CoverageChecksPassed);
        Assert.Equal(pending.CoverageKey,Inspect(accepted).CoverageKey);
        Assert.Equal("confirmation_changed",Inspect(accepted with {ExpectedCoverageKey=null}).Status);
        Assert.Equal("confirmation_changed",Inspect(accepted with {CoverageEnd="2026-05-01"}).Status);
        Assert.Equal("confirmation_changed",Inspect(accepted with {ConfirmReportCoverage=false}).Status);
        Assert.Equal("confirmation_changed",Inspect(accepted with {ConfirmEmptyPeriod=true}).Status);
        var unconfirmed=Inspect(Request() with {Footing=Request().Footing with {ControlTotalConfirmed=false}});
        Assert.Null(unconfirmed.DateSuggestion!.Start);Assert.False(unconfirmed.CoverageChecksPassed);
        var ledger=Request("Ledger",control:6,ledger:true);var reference=new CoverageLedger(1,"A-1","Cash A","v1");
        var proof=Inspect(ledger,ledger:true,reference:reference);Assert.True(proof.CoverageChecksPassed);
        Assert.Equal(1,proof.DateSuggestion!.TransactionDates);Assert.Equal("2026-04-03",proof.DateSuggestion.Start);
        Assert.False(Inspect(ledger with {CoverageEnd="2026-04-29"},ledger:true,reference:reference).CoverageChecksPassed);
        Assert.Equal("confirmation_changed",Inspect(ledger with {ExpectedCoverageKey=proof.CoverageKey},ledger:true,
            reference:reference with {ReferenceKey="v2"}).Status);
        Assert.False(Inspect(ledger,ledger:true).CoverageChecksPassed);
        var partialLedger=ledger with {Footing=ledger.Footing with {Validation=ledger.Footing.Validation with
            {ComparisonMode="correction",Source=ledger.Footing.Validation.Source with {ForceIncludeRows=[3]}}}};
        Assert.False(Inspect(partialLedger,ledger:true,reference:reference).CoverageChecksPassed);
        Assert.False(Inspect(Request() with {LedgerAccountCode="A-1"}).CoverageChecksPassed);
    }

    [Fact]
    public void LedgerPartialScopeAndBoundedDetailsRetainBlockingFullCounts()
    {
        var reference=new CoverageLedger(1,"A-1","Cash A","v1");
        var request=Request("LedgerOutside",false,mode:"correction",ledger:true);
        Assert.False(Inspect(request,false,ledger:true,reference:reference).CoverageChecksPassed);
        request=request with {Footing=request.Footing with {Validation=request.Footing.Validation with
            {Source=request.Footing.Validation.Source with {ExcludedRows=[2]}}}};
        Assert.True(Inspect(request,false,ledger:true,reference:reference).CoverageChecksPassed);
        var template=Definition();var maps=template.Columns.Select(c=>new PreparedMapping(c.Id,c.StorageKey,c.Name,c.Type,c.Role,true,c.Id,1,"manual")).ToArray();
        var rows=Enumerable.Range(2,1000).Select(n=>new WorkbookRow("Many",n,[new(n,1,WorkbookCellKind.Text,Text:"A"),
            new(n,2,WorkbookCellKind.Text,Text:"31/03/2026"),new(n,3,WorkbookCellKind.Number,NumberToken:"1")]));
        var proof=UploadCoverageService.Evaluate(template,maps,new(Guid.NewGuid(),1,10,1,"Many","hash",template.ConfigurationKey,0,"selection"),
            Request("Many",false),rows,new(){MaximumIssueDetails=2},new(),new(),ct:Token);
        Assert.False(proof.CoverageChecksPassed);Assert.Equal(1000,proof.IssueCount);Assert.Equal(2,proof.Issues.Count);
        Assert.True(proof.DetailsTruncated);Assert.Equal(1000,proof.DateSuggestion!.OutsideRows);
        Assert.Equal(golden.Oracle.RootElement.GetProperty("sha256").GetString(),Convert.ToHexString(SHA256.HashData(
            File.ReadAllBytes(Path.Combine(golden.DirectoryPath,"coverage.xlsx")))).ToLowerInvariant());
    }
    [Fact]
    public async Task OwnedCoverageRejectsStaleSourcesReferencesAndUnsafeHttpWithoutWrites()
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
            INSERT INTO public.master_columns VALUES(1,'col_1','Text',TRUE,'General',NULL),(2,'col_2','Date',TRUE,'General',NULL),(3,'col_3','Numeric',TRUE,'General',NULL),(4,'col_4','Text',TRUE,'General',NULL),(5,'col_5','Numeric',TRUE,'General',NULL),(6,'col_6','Text',TRUE,'General',NULL);
            INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture');
            """);
        await fixture.ApplyTenantFoundationAsync(Token);await fixture.ApplyEngagementFoundationAsync(Token);await fixture.ApplyTenantUserRolesAsync(Token);
        await fixture.ApplyTenantColumnsAsync(Token);await fixture.ApplyTenantTemplatesAsync(Token);await fixture.ApplySavedMappingsAsync(Token);await fixture.ApplyUploadStagingAsync(Token);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Document','native'),(2,101,'Date','native'),(3,101,'Amount','native'),(4,101,'Description','native'),(5,101,'Credit','native'),(6,101,'Balance','native');
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
        string directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P21-"+Guid.NewGuid().ToString("N"));var clock=new Clock();
        var staging=new UploadStagingService(tx,audit,Options.Create(new UploadStagingOptions{Enabled=true,RootDirectory=directory}),clock);
        var mappingService=new TenantMappingService(tx,audit);var preparation=new UploadPreparationService(staging,mappingService,Options.Create(new UploadPreparationOptions{Enabled=true}));
        var inputOptions=Options.Create(new UploadInputValidationOptions{Enabled=true});
        var footingOptions=Options.Create(new UploadFootingOptions{Enabled=true});
        var coverage=new UploadCoverageService(preparation,tx,Options.Create(new UploadCoverageOptions{Enabled=true}),footingOptions,inputOptions);
        using var stream=File.OpenRead(Path.Combine(golden.DirectoryPath,"coverage.xlsx"));
        var receipt=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",stream,Token);Assert.Equal("success",receipt.Status);int id=receipt.FileId!.Value;
        async Task<PreparationRequest> Source(string sheet)
        {
            var source=new PreparationRequest(10,1,sheet,RequestId:Guid.NewGuid());
            var snapshot=(await preparation.PrepareAsync(owner,id,source,Token)).Output!;
            return source with {Sha256=snapshot.Context.Sha256,ConfigurationKey=snapshot.Context.ConfigurationKey,MappingRevision=snapshot.Context.MappingRevision};
        }
        var source=await Source("Base");var request=Request() with {Footing=new(new(source,"replace_range",4),true)};
        async Task<CoverageResult> Run(CoverageRequest r,VerifiedSession? actor=null)=>await coverage.CheckAsync(actor??owner,id,r,Token);
        long audits=await Count("SELECT count(*) FROM public.audit_log");
        var full=await Run(request);Assert.Equal("complete_period",full.Output!.Status);Assert.False(full.Output.CanImport);
        var reviewed=request with {ExpectedCoverageKey=full.Output.CoverageKey};
        Assert.Equal("conflict",(await Run(reviewed with {CoverageEnd="2026-05-01"})).Status);
        Assert.Equal("forbidden",(await Run(request,other)).Status);Assert.Equal("not_found",(await Run(request,staff)).Status);
        Assert.Equal("invalid",(await Run(request with {Footing=request.Footing with {Validation=request.Footing.Validation with {Source=source with {Sha256=null}}}})).Status);
        await Sql("SELECT pg_advisory_lock(746102)");
        try
        {
            var bounded=new UploadCoverageService(preparation,tx,Options.Create(new UploadCoverageOptions
                {Enabled=true,MaximumConcurrentRequests=1,TimeoutSeconds=1}),footingOptions,inputOptions);
            var waiting=bounded.CheckAsync(owner,id,request,Token);
            Assert.Equal("busy",(await bounded.CheckAsync(owner,id,request,Token)).Status);Assert.Equal("timeout",(await waiting).Status);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Token);
            var interrupted=bounded.CheckAsync(owner,id,request,cancelled.Token);await cancelled.CancelAsync();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>interrupted);
        }
        finally {await Sql("SELECT pg_advisory_unlock(746102)");}
        clock.OnRead=()=>{clock.OnRead=null;Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE user_id=1").GetAwaiter().GetResult();};
        Assert.Equal("forbidden",(await Run(request)).Status);await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE user_id=1");
        await using(var disabled=new WebApplicationFactory<Program>()) using(var http=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.PostAsJsonAsync($"/api/upload/file/{id}/coverage",request,Token)).StatusCode);
        await using(var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);services.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true);
        })))
        {
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
            string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
            string route=$"/api/upload/file/{id}/coverage";client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);client.DefaultRequestHeaders.Remove("Origin");
            Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("Origin",address);
            Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
            var response=await client.PostAsJsonAsync(route,request,Token);Assert.Equal(HttpStatusCode.OK,response.StatusCode);
            Assert.Contains("no-store",response.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
            foreach(string value in new[]{"\"true\"","1","null"})
            {
                string body=JsonSerializer.Serialize(request,new JsonSerializerOptions(JsonSerializerDefaults.Web))
                    .Replace("\"confirmReportCoverage\":true","\"confirmReportCoverage\":"+value,StringComparison.Ordinal);
                using var content=new StringContent(body,System.Text.Encoding.UTF8,"application/json");
                Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(route,content,Token)).StatusCode);
            }
            using(var oversized=new HttpRequestMessage(HttpMethod.Post,route))
            {
                oversized.Headers.TransferEncodingChunked=true;
                oversized.Content=new StringContent("{\"padding\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json");
                Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(oversized,Token)).StatusCode);
            }
            Assert.Equal(HttpStatusCode.OK,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
        }
        await Sql("""
            UPDATE public.templates SET data_purpose='account_ledger',ledger_columns='{"voucher":1,"date":2,"description":4,"debit":3,"credit":5,"balance":6}' WHERE id=1;
            """);
        var unavailableSource=await Source("Ledger");
        var unavailableRequest=Request("Ledger",control:6,ledger:true) with {Footing=new(new(unavailableSource,"replace_range",6),true)};
        Assert.Equal("ledger_reference_unavailable",(await Run(unavailableRequest)).Code);
        await Sql("""
            CREATE TABLE public.trial_balance_uploads(id INTEGER PRIMARY KEY,client_id INTEGER,period_end DATE,uploaded_at TIMESTAMPTZ,status TEXT);
            CREATE TABLE public.trial_balance_accounts(upload_id INTEGER,account_code TEXT,account_name TEXT,classification TEXT);
            INSERT INTO public.trial_balance_uploads VALUES(1,10,'2026-04-30',now(),'parsed'),(2,10,'2026-03-31',now(),'parsed'),(20,20,'2027-04-30',now(),'parsed');
            INSERT INTO public.trial_balance_accounts VALUES(1,'A-1','Cash A','leaf'),(1,'HEADER','Header','heading'),(2,'OLD','Old account','leaf'),(20,'OTHER','Other tenant','leaf');
            UPDATE public.templates SET data_purpose='account_ledger',ledger_columns='{"voucher":1,"date":2,"description":4,"debit":3,"credit":5,"balance":6}' WHERE id=1;
            """);
        Assert.Equal("conflict",(await Run(request)).Status);
        var ledgerSource=await Source("Ledger");var ledgerRequest=Request("Ledger",control:6,ledger:true) with {Footing=new(new(ledgerSource,"replace_range",6),true)};
        var ledgerProof=await Run(ledgerRequest);Assert.Equal("complete_period",ledgerProof.Output!.Status);
        Assert.Equal(1,ledgerProof.Output.Ledger!.UploadId);Assert.Equal("Cash A",ledgerProof.Output.Ledger.AccountName);
        foreach(string code in new[]{"OTHER","OLD","HEADER","", "missing"})
            Assert.Equal("review",(await Run(ledgerRequest with {LedgerAccountCode=code})).Status);
        await Sql("INSERT INTO public.trial_balance_accounts VALUES(1,'A-1','Duplicate','leaf')");
        Assert.Equal("review",(await Run(ledgerRequest)).Status);await Sql("DELETE FROM public.trial_balance_accounts WHERE account_name='Duplicate'");
        await Sql("INSERT INTO public.trial_balance_uploads VALUES(3,10,'2026-05-31',now(),'parsed'); INSERT INTO public.trial_balance_accounts VALUES(3,'A-1','Updated name','leaf')");
        Assert.Equal("conflict",(await Run(ledgerRequest with {ExpectedCoverageKey=ledgerProof.Output.CoverageKey})).Status);
        // A fixture-only volatile view changes the selected account during the
        // first reference SELECT. The second reference must reject old evidence.
        await Sql("""
            ALTER TABLE public.trial_balance_accounts RENAME TO p21_account_source;
            CREATE TABLE public.p21_change_once(done BOOLEAN);
            INSERT INTO public.p21_change_once VALUES(FALSE);
            CREATE FUNCTION public.p21_change_account(value TEXT) RETURNS TEXT LANGUAGE plpgsql VOLATILE AS $$
            BEGIN
                IF NOT (SELECT done FROM public.p21_change_once) THEN
                    UPDATE public.p21_change_once SET done=TRUE;
                    UPDATE public.p21_account_source SET account_name='Changed during read' WHERE upload_id=3 AND account_code='A-1';
                END IF;
                RETURN value;
            END $$;
            CREATE VIEW public.trial_balance_accounts AS SELECT upload_id,account_code,public.p21_change_account(account_name) AS account_name,classification FROM public.p21_account_source;
            """);
        Assert.Equal("ledger_reference_changed",(await Run(ledgerRequest)).Code);
        Assert.Equal("complete_period",(await Run(ledgerRequest)).Output!.Status);
        Assert.Equal(audits,await Count("SELECT count(*) FROM public.audit_log"));Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.upload_validation_plans"));
        clock.Current=clock.Current.AddHours(3);Assert.Equal("gone",(await Run(ledgerRequest)).Status);clock.Current=DateTimeOffset.UtcNow;
        foreach(string path in Directory.GetFiles(directory,"*.bin")) await File.WriteAllTextAsync(path,"tampered",Token);
        Assert.Equal("gone",(await Run(ledgerRequest)).Status);
    }

    [Fact]
    public async Task CoverageStartupRequiresFootingAndValidatedProcessingBounds()
    {
        await using(var missing=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(
            services=>services.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true))))
            Assert.Throws<OptionsValidationException>(()=>missing.CreateClient());
        await using(var invalid=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(
            services=>services.PostConfigure<UploadCoverageOptions>(o=>o.MaximumConcurrentRequests=0))))
            Assert.Throws<OptionsValidationException>(()=>invalid.CreateClient());
    }

}
