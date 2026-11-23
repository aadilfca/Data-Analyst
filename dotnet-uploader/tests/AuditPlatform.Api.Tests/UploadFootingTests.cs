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

public sealed class FootingGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public ValueTask InitializeAsync() {
        DirectoryPath = NativeCompatibilityFixtureFactory.CreateWorkspace("p20", "p20-tests");
        Oracle = NativeCompatibilityFixtureFactory.ReadOracle(DirectoryPath);
        return ValueTask.CompletedTask;
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadFootingTests(FootingGoldenFixture golden) : IClassFixture<FootingGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static TemplateDefinition Definition(bool configured=true)=>new(1,"Fixture","Admin",new DateTime(2026,1,1),false,false,"standard",[],null,"standard","native",1,
        new[]{("Reference","Text",(string?)null),("Document","Text","document_id"),("Date","Date","document_date"),
            ("Debit","Numeric",configured?"footing":null),("Credit","Numeric",configured?"footing":null)}
            .Select((c,i)=>new TemplateColumn(i+1,c.Item1,"col_"+(i+1),c.Item2,"General",c.Item3,null,false,i,1,true)).ToArray());
    private FootingOutput Inspect(string sheet,int? control=5,bool confirmed=true,bool acknowledge=false,string? key=null,
        int[]? excluded=null,string mode="correction",bool configured=true,Guid? request=null,bool ledger=false)
    {
        var template=Definition(configured);
        if(ledger) template=template with {Purpose="account_ledger",Ledger=new(){{"voucher",2},{"date",3},{"description",6},{"debit",4},{"credit",5},{"balance",7}},
            Columns=new[]{(2,"Document","Text",(string?)"document_id"),(3,"Date","Date","document_date"),(6,"Description","Text",null),
                (4,"Debit","Numeric","footing"),(5,"Credit","Numeric","footing"),(7,"Balance","Text",null)}
                .Select((c,i)=>new TemplateColumn(c.Item1,c.Item2,"col_"+c.Item1,c.Item3,"General",c.Item4,null,false,i,1,true)).ToArray()};
        var rows=WorkbookReader.Read(Path.Combine(golden.DirectoryPath,"footing.xlsx"),".xlsx",sheet,ct:Token).ToArray();
        var maps=UploadPreparationService.AutoMap(template,UploadPreparationService.Headers(rows[0].Cells));
        var source=new PreparationRequest(10,1,sheet,"rows",ExcludedRows:excluded,RequestId:request??Guid.NewGuid());
        string selection=Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(new {sheet,excluded})));
        var context=new PreparationContext(source.RequestId,1,10,1,sheet,"hash",template.ConfigurationKey,0,selection);
        return UploadFootingService.Evaluate(template,maps,context,new(new(source,mode,control),confirmed,acknowledge,key),rows.Skip(1),
            new UploadFootingOptions(),new UploadInputValidationOptions(),Token);
    }

    [Fact]
    public void ExactParsingAndArithmeticMatchHighPrecisionPythonBeyondDecimalCapacity()
    {
        foreach(var item in golden.Oracle.RootElement.GetProperty("parse").EnumerateArray())
        {
            string text=item.GetProperty("text").GetString()!;
            if(item.GetProperty("valid").GetBoolean()) Assert.Equal(item.GetProperty("formatted").GetString(),UploadExactAmount.Parse(text).ToFixedString());
            else Assert.Throws<FormatException>(()=>UploadExactAmount.Parse(text));
        }
        foreach(var item in golden.Oracle.RootElement.GetProperty("arithmetic").EnumerateArray())
        {
            var result=UploadExactAmount.Zero;
            foreach(var value in item.GetProperty("values").EnumerateArray()) result+=UploadExactAmount.Parse(value.GetString()!);
            Assert.Equal(item.GetProperty("total").GetString(),result.ToFixedString());
        }
        Assert.Equal(0,UploadExactAmount.Parse("1.000").CompareTo(UploadExactAmount.Parse("1")));
        Assert.Throws<FormatException>(()=>UploadExactAmount.FromCell(new(2,1,WorkbookCellKind.Boolean,Boolean:true),true));
    }

    [Fact]
    public void ThresholdsAndAcknowledgementsMatchPythonOnBothSigns()
    {
        int index=0;
        foreach(var item in golden.Oracle.RootElement.GetProperty("thresholds").EnumerateArray())
        {
            var output=Inspect("Threshold"+index,3);index++;
            Assert.Equal(item.GetProperty("status").GetString(),output.Status);
            Assert.Equal(item.GetProperty("check").GetProperty("difference").GetString(),output.Checks[0].Difference);
            Assert.False(output.CanImport);
            if(output.Status=="confirmation_required")
            {
                var accepted=Inspect("Threshold"+(index-1),3,acknowledge:true,key:output.CheckKey);
                Assert.Equal("confirmed_variance",accepted.Status);Assert.True(accepted.FootingPassed);Assert.True(accepted.VarianceConfirmed);
                Assert.Equal(output.CheckKey,accepted.CheckKey); // New request ID is correlation only.
            }
            if(output.Status=="mismatch") Assert.False(Inspect("Threshold"+(index-1),3,acknowledge:true,key:output.CheckKey).FootingPassed);
        }
        var mixed=Inspect("Mixed",3);Assert.Equal("mismatch",mixed.Status);
        Assert.Equal("mismatch",Inspect("Mixed",3,acknowledge:true,key:mixed.CheckKey).Status);
    }

    [Fact]
    public void FullSourceAndSelectedSubsetKeepEveryOccurrenceAndVerticalControlChoice()
    {
        var full=Inspect("Full");Assert.Equal("matched",full.Status);Assert.Equal(3,full.FullSheet.RowCount);Assert.Equal(2,full.FullSheet.DocumentCount);
        Assert.Equal("0.30",full.FullSheet.Totals[4]);Assert.True(full.FullSheet.TotalsComplete);
        var subset=Inspect("Full",excluded:[2]);Assert.Equal("matched",subset.Status);
        Assert.Equal(full.FullSheet.RowCount,subset.FullSheet.RowCount);Assert.Equal(full.FullSheet.DocumentCount,subset.FullSheet.DocumentCount);
        Assert.Equal(full.FullSheet.Totals.OrderBy(k=>k.Key),subset.FullSheet.Totals.OrderBy(k=>k.Key));Assert.Equal(2,subset.SelectedDetail.RowCount);
        Assert.Equal("0.20",subset.SelectedDetail.Totals[4]);Assert.Equal("0.30",subset.SelectedDetail.Totals[5]);
        Assert.Equal("matched",Inspect("Middle",3).Status);Assert.Equal("matched",Inspect("Unlabelled").Status);
        Assert.Equal(3,Inspect("Repeated").FullSheet.RowCount);Assert.Equal("0.30",Inspect("Repeated").FullSheet.Totals[4]);
        Assert.Equal("invalid_source",Inspect("Full",confirmed:false).Status);
        Assert.Equal("invalid_source",Inspect("Full",control:1).Status);
        Assert.Equal("invalid_source",Inspect("Full",control:99).Status);
        Assert.Equal("not_configured",Inspect("Full",control:null,confirmed:false,configured:false).Status);
        Assert.Equal("invalid_source",Inspect("Full",configured:false).Status);
        var ledger=Inspect("Ledger",8,ledger:true);Assert.Equal("matched",ledger.Status);Assert.Equal(3,ledger.FullSheet.RowCount);
        Assert.Equal(1,ledger.InputValidation.Counts.ContinuationRows);Assert.Equal("0.30",ledger.FullSheet.Totals[4]);
        Assert.Equal("matched",Inspect("LedgerSelected",7,ledger:true).Status);
    }

    [Fact]
    public void CachedFormulasPrecisionReviewAndStaleAcknowledgementsCannotBeBypassed()
    {
        Assert.Equal("matched",Inspect("Cached").Status);
        Assert.Equal("invalid_source",Inspect("MissingCache").Status);
        Assert.Equal("invalid_source",Inspect("BooleanTotal").Status);
        var bad=Inspect("BadDetail",excluded:[2]);Assert.False(bad.FootingPassed);Assert.False(bad.FullSheet.TotalsComplete);
        var precise=Inspect("Precision");Assert.Equal("precision_review_required",precise.Status);Assert.False(precise.FootingPassed);
        Assert.Contains(precise.Issues,i=>i.Kind=="precision_review_required");
        Assert.NotEqual(golden.Oracle.RootElement.GetProperty("precision").GetProperty("raw").GetString(),golden.Oracle.RootElement.GetProperty("precision").GetProperty("legacy_first").GetString());
        var pending=Inspect("Threshold3",3);Assert.Equal("confirmation_required",pending.Status);
        Assert.Equal("confirmation_changed",Inspect("Threshold3",3,acknowledge:true,key:new string('0',64)).Status);
        Assert.Equal("confirmation_changed",Inspect("Threshold3",3,acknowledge:true,key:pending.CheckKey,excluded:[2]).Status);
        Assert.Equal("confirmation_changed",Inspect("Threshold3",3,acknowledge:true,key:pending.CheckKey,mode:"replace_range").Status);
        Assert.Equal("invalid_confirmation",Inspect("Full",acknowledge:true,key:pending.CheckKey).Status);
        Assert.Equal(golden.Oracle.RootElement.GetProperty("sha256").GetString(),Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(Path.Combine(golden.DirectoryPath,"footing.xlsx")))).ToLowerInvariant());
    }

    [Fact]
    public async Task OwnedHttpFootingIsDisabledByDefaultFreshAndWritesNoCanonicalRowsOrPlans()
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
            INSERT INTO public.master_columns VALUES(1,'col_1','Text',TRUE,'General',NULL),(2,'col_2','Text',TRUE,'General',NULL),(3,'col_3','Date',TRUE,'General',NULL),(4,'col_4','Numeric',TRUE,'General',NULL),(5,'col_5','Numeric',TRUE,'General',NULL);
            INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture');
            """);
        await fixture.ApplyTenantFoundationAsync(Token);await fixture.ApplyEngagementFoundationAsync(Token);await fixture.ApplyTenantUserRolesAsync(Token);
        await fixture.ApplyTenantColumnsAsync(Token);await fixture.ApplyTenantTemplatesAsync(Token);await fixture.ApplySavedMappingsAsync(Token);await fixture.ApplyUploadStagingAsync(Token);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Reference','native'),(2,101,'Document','native'),(3,101,'Date','native'),(4,101,'Debit','native'),(5,101,'Credit','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Fixture','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key) VALUES(1,1,0,NULL,101,'col_1'),(1,2,1,'document_id',101,'col_2'),(1,3,2,'document_date',101,'col_3'),(1,4,3,'footing',101,'col_4'),(1,5,4,'footing',101,'col_5');
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            INSERT INTO public.client_data(client_id,row_data) VALUES(20,'{"sentinel":"unchanged"}');
            """);
        using var pool=fixture.CreatePool();var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var logs=Options.Create(new LogPersistenceOptions());var audit=new PostgresAuditWriter(new LogSanitizer(logs),logs);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions{Enabled=true}));
        var owner=(await sessions.FindAsync(new string('a',64),Token))!;var staff=(await sessions.FindAsync(new string('c',64),Token))!;
        var other=(await sessions.FindAsync(new string('b',64),Token))!;
        string directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P20-"+Guid.NewGuid().ToString("N"));var clock=new Clock();
        var staging=new UploadStagingService(tx,audit,Options.Create(new UploadStagingOptions{Enabled=true,RootDirectory=directory}),clock);
        var mappingService=new TenantMappingService(tx,audit);var preparation=new UploadPreparationService(staging,mappingService,Options.Create(new UploadPreparationOptions{Enabled=true}));
        var inputOptions=Options.Create(new UploadInputValidationOptions{Enabled=true});
        var footing=new UploadFootingService(preparation,Options.Create(new UploadFootingOptions{Enabled=true}),inputOptions);
        using var stream=File.OpenRead(Path.Combine(golden.DirectoryPath,"footing.xlsx"));
        var receipt=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",stream,Token);Assert.Equal("success",receipt.Status);int id=receipt.FileId!.Value;
        var source=new PreparationRequest(10,1,"Threshold3",RequestId:Guid.NewGuid());var snapshot=(await preparation.PrepareAsync(owner,id,source,Token)).Output!;
        source=source with {Sha256=snapshot.Context.Sha256,ConfigurationKey=snapshot.Context.ConfigurationKey,MappingRevision=snapshot.Context.MappingRevision};
        var request=new FootingRequest(new(source,ControlTotalRow:3),true);
        async Task<FootingResult> Run(FootingRequest r,VerifiedSession? actor=null)=>await footing.CheckAsync(actor??owner,id,r,Token);
        long audits=await Count("SELECT count(*) FROM public.audit_log");
        var pending=await Run(request);Assert.Equal("confirmation_required",pending.Output!.Status);
        var accept=request with {ConfirmVariance=true,ExpectedCheckKey=pending.Output.CheckKey,Validation=request.Validation with {Source=source with {RequestId=Guid.NewGuid()}}};
        Assert.True((await Run(accept)).Output!.FootingPassed);Assert.False((await Run(accept)).Output!.CanImport);
        Assert.Equal("conflict",(await Run(accept with {ExpectedCheckKey=new string('0',64)})).Status);
        Assert.Equal("conflict",(await Run(accept with {Validation=accept.Validation with {Source=source with {DateOrder="MDY"}}})).Status);
        Assert.Equal("forbidden",(await Run(request,other)).Status);Assert.Equal("not_found",(await Run(request,staff)).Status);
        Assert.Equal("invalid",(await Run(request with {Validation=request.Validation with {Source=source with {Sha256=null}}})).Status);
        await Sql("SELECT pg_advisory_lock(746102)");
        try
        {
            var bounded=new UploadFootingService(preparation,Options.Create(new UploadFootingOptions
                {Enabled=true,MaximumConcurrentRequests=1,TimeoutSeconds=1}),inputOptions);
            var waiting=bounded.CheckAsync(owner,id,request,Token);
            Assert.Equal("busy",(await bounded.CheckAsync(owner,id,request,Token)).Status);Assert.Equal("timeout",(await waiting).Status);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Token);
            var interrupted=bounded.CheckAsync(owner,id,request,cancelled.Token);await cancelled.CancelAsync();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>interrupted);
        }
        finally {await Sql("SELECT pg_advisory_unlock(746102)");}
        clock.OnRead=()=>{clock.OnRead=null;Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE user_id=1").GetAwaiter().GetResult();};
        Assert.Equal("forbidden",(await Run(request)).Status);await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE user_id=1");
        Assert.Equal(audits,await Count("SELECT count(*) FROM public.audit_log"));Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'"));Assert.Equal(0L,await Count("SELECT count(*) FROM public.upload_validation_plans"));
        await using(var disabled=new WebApplicationFactory<Program>()) using(var http=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.PostAsJsonAsync($"/api/upload/file/{id}/footing",request,Token)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);services.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true);
        }));
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
        string route=$"/api/upload/file/{id}/footing";client.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
        client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);client.DefaultRequestHeaders.Remove("Origin");
        Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
        var response=await client.PostAsJsonAsync(route,request,Token);Assert.Equal(HttpStatusCode.OK,response.StatusCode);Assert.Contains("no-store",response.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
        string strict=JsonSerializer.Serialize(request,new JsonSerializerOptions(JsonSerializerDefaults.Web)).Replace("\"controlTotalRow\":3","\"controlTotalRow\":\"3\"",StringComparison.Ordinal);
        using(var content=new StringContent(strict,System.Text.Encoding.UTF8,"application/json")) Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(route,content,Token)).StatusCode);
        foreach(string invalidRow in new[]{"true","2.5"})
        {
            string body=JsonSerializer.Serialize(request,new JsonSerializerOptions(JsonSerializerDefaults.Web)).Replace("\"controlTotalRow\":3","\"controlTotalRow\":"+invalidRow,StringComparison.Ordinal);
            using var content=new StringContent(body,System.Text.Encoding.UTF8,"application/json");
            Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(route,content,Token)).StatusCode);
        }
        using(var oversized=new HttpRequestMessage(HttpMethod.Post,route))
        {
            oversized.Headers.TransferEncodingChunked=true;
            oversized.Content=new StringContent("{\"padding\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json");
            Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(oversized,Token)).StatusCode);
        }
        Assert.Equal(HttpStatusCode.OK,(await client.PostAsJsonAsync(route,accept,Token)).StatusCode);
        await Sql("UPDATE public.platform_tenant_columns SET display_name='Changed debit' WHERE column_id=4");
        Assert.Equal("conflict",(await Run(accept)).Status);
        clock.Current=clock.Current.AddHours(3);var latest=(await mappingService.ReadAsync(owner,10,1,Token)).Snapshot!;
        var fresh=request with {Validation=request.Validation with {Source=source with {ConfigurationKey=latest.ConfigurationKey}}};
        Assert.Equal("gone",(await Run(fresh)).Status);clock.Current=DateTimeOffset.UtcNow;
        foreach(string path in Directory.GetFiles(directory,"*.bin")) await File.WriteAllTextAsync(path,"tampered",Token);
        Assert.Equal("gone",(await Run(fresh)).Status);
    }
}
