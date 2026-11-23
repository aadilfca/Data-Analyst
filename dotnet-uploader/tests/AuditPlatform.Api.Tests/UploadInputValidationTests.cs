using System.Diagnostics;
using System.Globalization;
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

public sealed class InputValidationGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public ValueTask InitializeAsync() {
        DirectoryPath = NativeCompatibilityFixtureFactory.CreateWorkspace("p19", "p19-tests");
        Oracle = NativeCompatibilityFixtureFactory.ReadOracle(DirectoryPath);
        return ValueTask.CompletedTask;
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadInputValidationTests(InputValidationGoldenFixture golden)
    : IClassFixture<InputValidationGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static TemplateColumn Column(int id,string name,string type="Text",string? role=null)=>new(id,name,"col_"+id,type,"General",role,null,false,id-1,1,true);
    private static TemplateDefinition Definition(bool ledger=false,bool footing=false)
    {
        var columns=ledger?new[]{Column(1,"Voucher",role:"document_id"),Column(2,"Date","Date","document_date"),Column(6,"Description"),
            Column(4,"Debit","Numeric"),Column(5,"Credit","Numeric"),Column(7,"Balance")}
            :new[]{Column(1,"Voucher",role:"document_id"),Column(2,"Date","Date","document_date"),Column(3,"Account"),
                Column(4,"Debit","Numeric",footing?"footing":null),Column(5,"Credit","Numeric")};
        return new(ledger?2:1,"Fixture","Admin",new DateTime(2026,1,1),false,false,ledger?"account_ledger":"standard",
            ledger?new(){{"voucher",1},{"date",2},{"description",6},{"debit",4},{"credit",5},{"balance",7}}:[],null,"standard","native",1,columns);
    }
    private InputValidationOutput Inspect(string sheet,bool ledger=false,bool footing=false,string mode="correction",
        int[]? exclude=null,int[]? force=null,int? control=null,int page=1,int size=50,int bytes=1048576,int[]? unmapped=null)
    {
        var template=Definition(ledger,footing);string path=Path.Combine(golden.DirectoryPath,"validation.xlsx");
        var rows=WorkbookReader.Read(path,".xlsx",sheet,ct:Token).ToArray();
        var maps=UploadPreparationService.AutoMap(template,UploadPreparationService.Headers(rows[0].Cells))
            .Select(m=>unmapped?.Contains(m.ColumnId)==true?m with {SourcePosition=null}:m).ToArray();
        var source=new PreparationRequest(10,template.Id,sheet,"rows",ExcludedRows:exclude,ForceIncludeRows:force,RequestId:Guid.NewGuid());
        var context=new PreparationContext(source.RequestId,1,10,template.Id,sheet,"hash",template.ConfigurationKey,0,"selection");
        return UploadInputValidationService.CheckRows(template,maps,context,new(source,mode,control,page,size),rows.Skip(1),
            new UploadInputValidationOptions{MaximumResponseBytes=bytes},Token);
    }

    [Fact]
    public void NumericSyntaxAndLedgerSignsMatchIndependentPythonWithoutNarrowingTokens()
    {
        foreach(var vector in golden.Oracle.RootElement.GetProperty("numbers").EnumerateArray())
        {
            string kind=vector.GetProperty("kind").GetString()!;var value=vector.GetProperty("value");
            var cell=new WorkbookCell(2,1,Enum.Parse<WorkbookCellKind>(kind),Text:kind=="Text"?value.GetString():null,
                NumberToken:kind=="Number"?value.GetString():null,Boolean:kind=="Boolean"?value.GetBoolean():null);
            var check=UploadNumericParser.Check(cell,vector.GetProperty("ledger").GetBoolean(),vector.GetProperty("balance").GetBoolean());
            Assert.True(check.Valid==vector.GetProperty("valid").GetBoolean(),vector.ToString()+" => "+JsonSerializer.Serialize(check));
            if(check.Valid && vector.GetProperty("ledger").GetBoolean() && vector.TryGetProperty("negative",out var negative))
            {Assert.Equal(negative.GetBoolean(),check.Negative);Assert.Equal(vector.GetProperty("zero").GetBoolean(),check.Zero);}
        }
        const string precise="0.1000000000000000000000000000000001";
        Assert.Equal(precise,UploadNumericParser.Check(new(2,1,WorkbookCellKind.Text,Text:precise)).Token);
        Assert.Equal("9007199254740993",UploadNumericParser.Check(new(2,1,WorkbookCellKind.Number,NumberToken:"9007199254740993")).Token);
    }

    [Fact]
    public void OriginalCellErrorsDatesAndCriticalBlanksMatchPython()
    {
        foreach(var item in golden.Oracle.RootElement.GetProperty("cases").EnumerateArray())
        {
            string sheet=item.GetProperty("sheet").GetString()!;var output=Inspect(sheet,footing:item.GetProperty("footing").GetBoolean());
            var actual=output.Issues.SelectMany(r=>r.Errors).Select(e=>e.ColumnId).Order().ToArray();
            var expected=item.GetProperty("error_columns").EnumerateArray().Select(e=>(int?)e.GetInt32()).Order().ToArray();
            Assert.True(expected.SequenceEqual(actual),sheet+": "+JsonSerializer.Serialize(output));
            var missing=output.Issues.SelectMany(r=>r.Missing).Select(e=>e.ColumnId).Order().ToArray();
            Assert.Equal(item.GetProperty("missing_columns").EnumerateArray().Select(e=>(int?)e.GetInt32()).Order(),missing);
            Assert.False(output.CanImport);Assert.True(output.ChecksCompleted);
            Assert.All(output.Issues,r=>Assert.Equal(2,r.Row));
            if(actual.Length>0) Assert.False(output.InputChecksPassed);
        }
        string path=Path.Combine(golden.DirectoryPath,"validation.xlsx");
        Assert.Equal(golden.Oracle.RootElement.GetProperty("sha256").GetString(),Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant());
    }

    [Fact]
    public void ExclusionsAndOverridesCannotBypassInvalidCellsOrCompleteReportRules()
    {
        var missing=Inspect("blank-voucher");Assert.Equal(1,missing.Counts.RejectedRows);Assert.Equal(0,missing.Counts.EligibleRows);
        var forced=Inspect("blank-voucher",force:[2]);Assert.True(forced.InputChecksPassed);Assert.Equal(1,forced.Counts.ForceIncludedRows);Assert.Equal(1,forced.Counts.EligibleRows);
        var bad=Inspect("invalid-number",force:[2]);Assert.False(bad.InputChecksPassed);Assert.Equal(0,bad.Counts.ForceIncludedRows);Assert.Equal(0,bad.Counts.EligibleRows);
        Assert.True(Inspect("excel-error",exclude:[2]).InputChecksPassed);
        Assert.False(Inspect("excel-error",footing:true,exclude:[2]).InputChecksPassed);
        Assert.False(Inspect("excel-error",mode:"replace_range",exclude:[2]).InputChecksPassed);
        Assert.False(Inspect("blank-voucher",mode:"replace_range",force:[2]).InputChecksPassed);
        Assert.True(Inspect("excel-error",unmapped:[3]).InputChecksPassed);
        Assert.False(Inspect("valid",unmapped:[1]).InputChecksPassed);
        var nan=Inspect("nan-number",force:[2]);Assert.Empty(Assert.Single(nan.Issues).Missing);Assert.Equal(0,nan.Counts.ForceIncludedRows);
        Assert.False(Inspect("valid",exclude:[99]).InputChecksPassed);
        var complete=Inspect("Rows",mode:"replace_range");Assert.Equal(4,complete.Counts.TransactionRows);
        Assert.Equal(2,complete.Counts.ControlRows);Assert.Equal(4,complete.Counts.EligibleRows);Assert.True(complete.InputChecksPassed);
        Assert.NotEqual(complete.ValidationKey,Inspect("Rows").ValidationKey);
        Assert.Equal(new[]{3,5},complete.Issues.Select(i=>i.Row));
        Assert.False(Inspect("valid",control:2).InputChecksPassed);
        var control=Inspect("formula-missing",footing:true,control:2);
        Assert.False(control.InputChecksPassed);Assert.Equal("selected_control",Assert.Single(control.Issues).Kind);
    }

    [Fact]
    public void LedgerNotesControlsAndBoundedDiagnosticsPreserveCountsAndPositions()
    {
        var ledger=Inspect("Ledger",ledger:true);
        Assert.Equal(7,ledger.Counts.SourceRows);Assert.Equal(2,ledger.Counts.TransactionRows);Assert.Equal(3,ledger.Counts.ControlRows);
        Assert.Equal(2,ledger.Counts.ContinuationRows);Assert.Equal(1,ledger.Counts.ErrorRows);Assert.Equal(2,ledger.Counts.EligibleRows);
        Assert.Equal(3,ledger.Issues.Single(i=>i.Row==4).ParentRow);Assert.Equal("ledger_note",Assert.Single(ledger.Issues.Single(i=>i.Row==6).Errors).Kind);
        Assert.False(ledger.InputChecksPassed);
        var first=Inspect("Many",page:1,size:10);var second=Inspect("Many",page:2,size:10);
        Assert.Equal(202,first.Counts.ErrorRows);Assert.Equal(202,first.IssueRows);Assert.Equal(202,first.Counts.TransactionRows);
        Assert.Equal(Enumerable.Range(2,10),first.Issues.Select(r=>r.Row));Assert.Equal(Enumerable.Range(12,10),second.Issues.Select(r=>r.Row));
        Assert.True(first.DetailsTruncated);Assert.False(first.InputChecksPassed);
        var small=Inspect("Many",size:100,bytes:4096);Assert.Equal(202,small.Counts.ErrorRows);Assert.True(small.DetailsTruncated);Assert.True(small.Issues.Count<100);
        Assert.All(first.Issues.SelectMany(r=>r.Errors),e=>{Assert.Equal(4,e.SourcePosition);Assert.Equal("col_4",e.StorageKey);});
    }

    [Fact]
    public async Task HttpValidationIsOwnerBoundFreshDisabledAndWritesNoCanonicalRowsOrPlans()
    {
        await using var fixture=new PostgresFixture();await fixture.InitializeAsync();
        await fixture.ApplyAuditSchemaAsync(Token);await using var connection=await fixture.OpenControlConnectionAsync(Token);
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
            INSERT INTO public.master_columns VALUES(1,'col_1','Text',TRUE,'General',NULL),(2,'col_2','Date',TRUE,'General',NULL),(3,'col_3','Text',TRUE,'General',NULL),(4,'col_4','Numeric',TRUE,'General',NULL),(5,'col_5','Numeric',TRUE,'General',NULL);
            INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture');
            """);
        await fixture.ApplyTenantFoundationAsync(Token);await fixture.ApplyEngagementFoundationAsync(Token);await fixture.ApplyTenantUserRolesAsync(Token);
        await fixture.ApplyTenantColumnsAsync(Token);await fixture.ApplyTenantTemplatesAsync(Token);await fixture.ApplySavedMappingsAsync(Token);await fixture.ApplyUploadStagingAsync(Token);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Voucher','native'),(2,101,'Date','native'),(3,101,'Account','native'),(4,101,'Debit','native'),(5,101,'Credit','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Fixture','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key) VALUES(1,1,0,'document_id',101,'col_1'),(1,2,1,'document_date',101,'col_2'),(1,3,2,NULL,101,'col_3'),(1,4,3,NULL,101,'col_4'),(1,5,4,NULL,101,'col_5');
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            INSERT INTO public.client_data(client_id,row_data) VALUES(20,'{"sentinel":"unchanged"}');
            """);
        using var pool=fixture.CreatePool();var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var logs=Options.Create(new LogPersistenceOptions());var audit=new PostgresAuditWriter(new LogSanitizer(logs),logs);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions{Enabled=true}));
        var owner=(await sessions.FindAsync(new string('a',64),Token))!;var staff=(await sessions.FindAsync(new string('c',64),Token))!;
        var other=(await sessions.FindAsync(new string('b',64),Token))!;
        string directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P19-"+Guid.NewGuid().ToString("N"));
        var clock=new Clock();var staging=new UploadStagingService(tx,audit,Options.Create(new UploadStagingOptions{Enabled=true,RootDirectory=directory}),clock);
        var mappings=new TenantMappingService(tx,audit);var preparation=new UploadPreparationService(staging,mappings,Options.Create(new UploadPreparationOptions{Enabled=true}));
        var validation=new UploadInputValidationService(preparation,Options.Create(new UploadInputValidationOptions{Enabled=true}));
        using var bytes=File.OpenRead(Path.Combine(golden.DirectoryPath,"validation.xlsx"));
        var receipt=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",bytes,Token);Assert.Equal("success",receipt.Status);int id=receipt.FileId!.Value;
        var source=new PreparationRequest(10,1,"valid",RequestId:Guid.NewGuid());var prepared=(await preparation.PrepareAsync(owner,id,source,Token)).Output!;
        source=source with {Sha256=prepared.Context.Sha256,ConfigurationKey=prepared.Context.ConfigurationKey,MappingRevision=prepared.Context.MappingRevision};
        var request=new InputValidationRequest(source);
        async Task<InputValidationResult> Run(InputValidationRequest r,VerifiedSession? actor=null)=>await validation.ValidateAsync(actor??owner,id,r,Token);
        long audits=await Count("SELECT count(*) FROM public.audit_log");
        Assert.Equal("input_checks_passed",(await Run(request)).Status);
        Assert.Equal("input_checks_failed",(await Run(request with {Source=source with {Sheet="excel-error"}})).Status);
        var missing=await Run(request with {Source=source with {Sheet="blank-voucher",ForceIncludeRows=[2]}});
        Assert.Equal(1,missing.Output!.Counts.ForceIncludedRows);Assert.False(missing.Output.CanImport);
        Assert.Equal("conflict",(await Run(request with {Source=source with {Sha256=new string('0',64)}})).Status);
        Assert.Equal("conflict",(await Run(request with {Source=source with {MappingRevision=99}})).Status);
        Assert.Equal("invalid",(await Run(request with {Source=source with {Sha256=null}})).Status);
        Assert.Equal("invalid",(await Run(request with {Source=source with {Mappings=[new(1,1),new(2,1)]}})).Status);
        Assert.Equal("forbidden",(await Run(request,other)).Status);Assert.Equal("not_found",(await Run(request,staff)).Status);
        await Sql("UPDATE public.sessions SET context_version=1 WHERE user_id=1");Assert.Equal("forbidden",(await Run(request)).Status);
        await Sql("UPDATE public.sessions SET context_version=0 WHERE user_id=1");
        await Sql("SELECT pg_advisory_lock(746102)");
        try
        {
            var bounded=new UploadInputValidationService(preparation,Options.Create(new UploadInputValidationOptions
                {Enabled=true,MaximumConcurrentRequests=1,TimeoutSeconds=1}));
            var waiting=bounded.ValidateAsync(owner,id,request,Token);
            Assert.Equal("busy",(await bounded.ValidateAsync(owner,id,request,Token)).Status);
            Assert.Equal("timeout",(await waiting).Status);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Token);
            var interrupted=bounded.ValidateAsync(owner,id,request,cancelled.Token);await cancelled.CancelAsync();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>interrupted);
        }
        finally {await Sql("SELECT pg_advisory_unlock(746102)");}
        clock.OnRead=()=>{clock.OnRead=null;Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE user_id=1").GetAwaiter().GetResult();};
        Assert.Equal("forbidden",(await Run(request)).Status);await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE user_id=1");
        Assert.Equal(audits,await Count("SELECT count(*) FROM public.audit_log"));Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.upload_validation_plans"));
        await using(var disabled=new WebApplicationFactory<Program>()) using(var http=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.PostAsJsonAsync($"/api/upload/file/{id}/validate-input",request,Token)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});
            services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);
        }));
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
        string route=$"/api/upload/file/{id}/validate-input";client.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
        client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);client.DefaultRequestHeaders.Remove("Origin");
        Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
        var response=await client.PostAsJsonAsync(route,request with {Source=source with {Sheet="invalid-date"}},Token);Assert.Equal(HttpStatusCode.OK,response.StatusCode);
        var json=await response.Content.ReadFromJsonAsync<JsonElement>(Token);Assert.Equal("input_checks_failed",json.GetProperty("status").GetString());
        Assert.False(json.GetProperty("result").GetProperty("canImport").GetBoolean());Assert.Contains("no-store",response.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
        using(var tooLarge=new HttpRequestMessage(HttpMethod.Post,route))
        {
            tooLarge.Headers.TransferEncodingChunked=true;
            tooLarge.Content=new StringContent("{\"padding\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json");
            Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(tooLarge,Token)).StatusCode);
        }
        Assert.Equal(HttpStatusCode.OK,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
        await Sql("UPDATE public.platform_tenant_columns SET display_name='Changed voucher' WHERE column_id=1");
        Assert.Equal("conflict",(await Run(request)).Status);
        clock.Current=clock.Current.AddHours(3);var fresh=(await preparation.PrepareAsync(owner,id,source with {ConfigurationKey=null},Token));Assert.Equal("gone",fresh.Status);clock.Current=DateTimeOffset.UtcNow;
        foreach(string path in Directory.GetFiles(directory,"*.bin")) await File.WriteAllTextAsync(path,"tampered",Token);
        var latest=(await mappings.ReadAsync(owner,10,1,Token)).Snapshot!;
        Assert.Equal("gone",(await Run(request with {Source=source with {ConfigurationKey=latest.ConfigurationKey}})).Status);
    }
}
