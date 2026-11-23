using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Http.Json;
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

public sealed class PreparationGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public ValueTask InitializeAsync() {
        DirectoryPath = NativeCompatibilityFixtureFactory.CreateWorkspace("p18", "p18-tests");
        Oracle = NativeCompatibilityFixtureFactory.ReadOracle(DirectoryPath);
        return ValueTask.CompletedTask;
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadPreparationTests(PostgresFixture fixture,PreparationGoldenFixture golden)
    : IClassFixture<PostgresFixture>,IClassFixture<PreparationGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {
        public DateTimeOffset Current=DateTimeOffset.UtcNow;
        public TimeSpan Step;
        public Action? OnRead;
        public override DateTimeOffset GetUtcNow()
        {OnRead?.Invoke();var value=Current;Current+=Step;return value;}
    }
    private static TemplateDefinition Template(IReadOnlyList<TemplateColumn> columns)=>
        new(1,"Fixture","Admin",new DateTime(2026,1,1),false,false,"standard",[],null,"standard","native",1,columns);
    private static TemplateColumn Column(int id,string name,string type="Text",string? role=null)=>
        new(id,name,"col_"+id,type,"General",role,null,false,id-1,1,true);

    [Fact]
    public void ExplicitDateChoicesMatchIsolatedPythonAndNativeCellsRemainNative()
    {
        foreach(var item in golden.Oracle.RootElement.GetProperty("dates").EnumerateArray())
        {
            var cell=new WorkbookCell(2,1,WorkbookCellKind.Text,Text:item.GetProperty("text").GetString());
            string separator=item.GetProperty("delimiter").GetString()!,order=item.GetProperty("order").GetString()!;
            if(item.TryGetProperty("error",out _)) Assert.Throws<FormatException>(()=>UploadDateParser.Parse(cell,separator,order));
            else Assert.Equal(item.GetProperty("parsed").ValueKind==JsonValueKind.Null?null:item.GetProperty("parsed").GetString(),
                UploadDateParser.Parse(cell,separator,order)?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture));
        }
        Assert.Equal(new DateTime(2026,4,13),UploadDateParser.Parse(new(2,1,WorkbookCellKind.DateTime,DateTime:new DateTime(2026,4,13)),"-","MDY"));
        Assert.Equal(new DateTime(2026,2,11),UploadDateParser.Parse(new(2,1,WorkbookCellKind.Number,NumberToken:"46064"),"/","DMY"));
        Assert.Equal(new DateTime(1899,12,31),UploadDateParser.Parse(new(2,1,WorkbookCellKind.Boolean,Boolean:true),"/","DMY"));
        Assert.Throws<FormatException>(()=>UploadDateParser.Parse(new(2,1,WorkbookCellKind.Error,Error:"#REF!"),"/","DMY"));
        Assert.Throws<FormatException>(()=>UploadDateParser.Parse(new(2,1,WorkbookCellKind.Time,Time:TimeSpan.Zero),"/","DMY"));
        Assert.Throws<FormatException>(()=>UploadDateParser.Parse(new(2,1,WorkbookCellKind.Number,NumberToken:"1e300"),"/","DMY"));
    }

    [Fact]
    public void HeaderPriorityAliasesAndFallbackMatchPythonWithoutGuessingAmbiguousNames()
    {
        foreach(var item in golden.Oracle.RootElement.GetProperty("mappings").EnumerateArray())
        {
            var columns=item.GetProperty("columns").EnumerateArray().Select(c=>Column(c.GetProperty("column_id").GetInt32(),
                c.GetProperty("column_name").GetString()!,role:c.GetProperty("reconciliation_role").GetString())).ToArray();
            var headers=item.GetProperty("headers").EnumerateArray().Select((h,index)=>new PreparationHeader(index+1,h.GetString()!,false)).ToArray();
            var actual=UploadPreparationService.AutoMap(Template(columns),headers);
            var expected=item.GetProperty("expected");
            for(int index=0;index<columns.Length;index++)
            {
                string? name=expected[index].GetProperty("excel_column").GetString();
                Assert.Equal(name,headers.SingleOrDefault(h=>h.Position==actual[index].SourcePosition)?.Label);
                Assert.Equal(expected[index].GetProperty("match_score").GetDouble(),actual[index].Score,6);
            }
        }
        var duplicate=UploadPreparationService.Headers([new(1,1,WorkbookCellKind.Text,Text:"Amount"),
            new(1,2,WorkbookCellKind.Text,Text:"Amount"),new(1,3,WorkbookCellKind.Blank)]);
        Assert.All(duplicate,h=>Assert.True(h.Ambiguous));
        Assert.Null(Assert.Single(UploadPreparationService.AutoMap(Template([Column(1,"Amount")]),duplicate)).SourcePosition);
    }

    [Fact]
    public void PreparationLimitsRejectInvalidConfiguration()
    {
        var options=new UploadPreparationOptions {MaximumConcurrentRequests=0};
        Assert.NotEmpty(options.Validate(new System.ComponentModel.DataAnnotations.ValidationContext(options)));
        Assert.False(new UploadPreparationOptions().Enabled);
    }

    [Fact]
    public async Task OwnedPreparationPreservesRowsAndRejectsObsoleteSourcesBindingsAndHttpContexts()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
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
            INSERT INTO public.users VALUES(1,'owner','unused',TRUE,'admin',NULL),(2,'other','unused',TRUE,'admin',NULL),(3,'auditor','unused',TRUE,'user',NULL);
            INSERT INTO public.clients VALUES(10,'A','Alpha',TRUE),(20,'B','Beta',TRUE);
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0);
            INSERT INTO public.master_columns VALUES(1,'col_1','Text',TRUE,'General',NULL),(2,'col_2','Date',TRUE,'General',NULL),(3,'col_3','Numeric',TRUE,'General',NULL);
            INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture');
            """);
        await fixture.ApplyTenantFoundationAsync(Token);await fixture.ApplyEngagementFoundationAsync(Token);await fixture.ApplyTenantUserRolesAsync(Token);
        await fixture.ApplyTenantColumnsAsync(Token);await fixture.ApplyTenantTemplatesAsync(Token);await fixture.ApplySavedMappingsAsync(Token);await fixture.ApplyUploadStagingAsync(Token);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Voucher','native'),(2,101,'Date','native'),(3,101,'Amount','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Fixture','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key) VALUES(1,1,0,'document_id',101,'col_1'),(1,2,1,'document_date',101,'col_2'),(1,3,2,NULL,101,'col_3');
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            """);
        using var pool=fixture.CreatePool();var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var logs=Options.Create(new LogPersistenceOptions());var audit=new PostgresAuditWriter(new LogSanitizer(logs),logs);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions{Enabled=true}));
        var owner=(await sessions.FindAsync(new string('a',64),Token))!;var other=(await sessions.FindAsync(new string('b',64),Token))!;
        var staff=(await sessions.FindAsync(new string('c',64),Token))!;
        string directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P18-"+Guid.NewGuid().ToString("N"));
        var stagingOptions=Options.Create(new UploadStagingOptions{Enabled=true,RootDirectory=directory});var clock=new Clock();
        var staging=new UploadStagingService(tx,audit,stagingOptions,clock);var mappingService=new TenantMappingService(tx,audit);
        var preparationOptions=Options.Create(new UploadPreparationOptions{Enabled=true});
        var service=new UploadPreparationService(staging,mappingService,preparationOptions);
        byte[] bytes=await File.ReadAllBytesAsync(Path.Combine(golden.DirectoryPath,"preparation.xlsx"),Token);
        async Task<StagingResult> Upload(VerifiedSession actor) {using var source=new MemoryStream(bytes);return await staging.ReceiveAsync(actor,10,1,Guid.NewGuid(),"synthetic.xlsx",source,Token);}
        var receipt=await Upload(owner);Assert.Equal("success",receipt.Status);int id=receipt.FileId!.Value;
        var initial=new PreparationRequest(10,1,"Data",RequestId:Guid.NewGuid());
        async Task<PreparationResult> Run(PreparationRequest r,VerifiedSession? actor=null)=>await service.PrepareAsync(actor??owner,id,r,Token);
        var mapped=await Run(initial);Assert.Equal("success",mapped.Status);var output=mapped.Output!;
        Assert.Equal(new int?[]{1,2,3},output.Mappings.Select(m=>m.SourcePosition));Assert.NotEmpty(output.Warnings);
        Assert.Equal(initial.RequestId,output.Context.RequestId);Assert.Equal(receipt.Sha256,output.Context.Sha256);
        var current=initial with {Sha256=output.Context.Sha256,ConfigurationKey=output.Context.ConfigurationKey,MappingRevision=output.Context.MappingRevision};
        var rows=await Run(current with {Operation="rows",PageSize=2,ExcludedRows=[3],ForceIncludeRows=[4]});
        Assert.Equal("success",rows.Status);Assert.Equal(6,rows.Output!.TotalRows);Assert.Equal(5,rows.Output.SelectedRows);
        Assert.Equal(new[]{2,3},rows.Output.Rows!.Select(r=>r.Number));Assert.Equal("00123",rows.Output.Rows![0].Cells[0].Text);
        Assert.Equal("0",rows.Output.Rows[0].Cells[2].NumberToken);Assert.True(rows.Output.Rows[1].Flagged);Assert.True(rows.Output.Rows[1].Excluded);
        var page=await Run(current with {Operation="rows",Page=2,PageSize=2,ForceIncludeRows=[4]});
        Assert.Equal(new[]{4,5},page.Output!.Rows!.Select(r=>r.Number));Assert.True(page.Output.Rows![0].ForceInclude);Assert.True(page.Output.Rows[0].Flagged);
        Assert.NotEqual(rows.Output.Context.SelectionKey,page.Output.Context.SelectionKey);
        var dates=await Run(current with {Operation="dates"});Assert.Equal("success",dates.Status);
        Assert.Equal(3,dates.Output!.Samples!.Count);Assert.Equal("2026-05-04",dates.Output.Samples[0].Parsed);
        Assert.Equal("2026-02-11",dates.Output.DateSuggestion!.Start);Assert.Equal("2026-06-07",dates.Output.DateSuggestion.End);Assert.Equal(5,dates.Output.DateSuggestion.Count);
        var us=await Run(current with {Operation="dates",DateOrder="MDY",RequestId=Guid.NewGuid()});Assert.Equal("2026-04-05",us.Output!.Samples![0].Parsed);
        Assert.NotEqual(us.Output.Context.RequestId,dates.Output.Context.RequestId);
        Assert.NotEqual(us.Output.Context.SelectionKey,dates.Output.Context.SelectionKey);
        var remap=await Run(current with {Operation="dates",Mappings=[new(1,1),new(2,4),new(3,3)]});
        Assert.Equal("2026-04-13",remap.Output!.Samples![0].Parsed);
        Assert.Equal("invalid",(await Run(current with {Mappings=[new(1,1),new(2,1)]})).Status);
        Assert.Equal("invalid",(await Run(current with {Operation="rows",ExcludedRows=[3],ForceIncludeRows=[3]})).Status);
        Assert.Equal("invalid",(await Run(current with {Operation="rows",ExcludedRows=[99]})).Status);
        Assert.Equal("invalid",(await Run(current with {Operation="dates",Sha256=null})).Status);
        Assert.Equal("invalid",(await Run(current with {Sheet="Missing"})).Status);
        Assert.Equal("conflict",(await Run(current with {Sha256=new string('0',64)})).Status);
        Assert.Equal("conflict",(await Run(current with {ConfigurationKey=new string('0',64)})).Status);
        Assert.Equal("conflict",(await Run(current with {MappingRevision=99})).Status);
        Assert.Equal("forbidden",(await Run(current,other)).Status);Assert.Equal("not_found",(await Run(current,staff)).Status);
        long readAudits=await Count("SELECT count(*) FROM public.audit_log");
        Assert.Equal("success",(await Run(initial)).Status);
        Assert.Equal(readAudits,await Count("SELECT count(*) FROM public.audit_log"));
        Assert.Equal("forbidden",(await Run(initial with {ClientId=20})).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE user_id=1");
        Assert.Equal("forbidden",(await Run(initial)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE user_id=1");
        await Sql("UPDATE public.templates SET is_active=FALSE WHERE id=1");
        Assert.Equal("forbidden",(await Run(initial)).Status);
        await Sql("UPDATE public.templates SET is_active=TRUE WHERE id=1");
        await Sql("UPDATE public.master_columns SET is_active=FALSE WHERE id=2");
        Assert.Equal("review",(await Run(initial)).Status);
        await Sql("UPDATE public.master_columns SET is_active=TRUE WHERE id=2");
        Assert.Equal("conflict",(await Run(current)).Status);
        output=(await Run(initial)).Output!;
        current=current with {ConfigurationKey=output.Context.ConfigurationKey,MappingRevision=output.Context.MappingRevision};
        preparationOptions.Value.MaximumResponseBytes=4096;
        Assert.Equal("invalid",(await Run(current with {Operation="rows",PageSize=6})).Status);
        Assert.Equal("invalid",(await Run(current with {Operation="dates",Sheet="LargeDates"})).Status);
        preparationOptions.Value.MaximumResponseBytes=1048576;
        // Hold the existing configuration lock in this owned fixture to make
        // concurrency/deadline/caller cancellation deterministic, without hooks.
        await Sql("SELECT pg_advisory_lock(746102)");
        try
        {
            var bounded=new UploadPreparationService(staging,mappingService,Options.Create(new UploadPreparationOptions
                {Enabled=true,MaximumConcurrentRequests=1,TimeoutSeconds=1}));
            var waiting=bounded.PrepareAsync(owner,id,initial,Token);
            Assert.Equal("busy",(await bounded.PrepareAsync(owner,id,initial,Token)).Status);
            Assert.Equal("timeout",(await waiting).Status);
            using var cancelled=CancellationTokenSource.CreateLinkedTokenSource(Token);
            var interrupted=bounded.PrepareAsync(owner,id,initial,cancelled.Token);
            await cancelled.CancelAsync();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>interrupted);
        }
        finally {await Sql("SELECT pg_advisory_unlock(746102)");}
        var staffReceipt=await Upload(staff);Assert.Equal("success",staffReceipt.Status);
        Assert.Equal("success",(await service.PrepareAsync(staff,staffReceipt.FileId!.Value,initial,Token)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3");
        Assert.Equal("forbidden",(await service.PrepareAsync(staff,staffReceipt.FileId.Value,initial,Token)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE user_id=3");
        // TimeProvider is an existing staging dependency. Revoke between the
        // initial snapshot and parsing to prove the second live check matters.
        clock.OnRead=()=>{clock.OnRead=null;Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3").GetAwaiter().GetResult();};
        Assert.Equal("forbidden",(await service.PrepareAsync(staff,staffReceipt.FileId.Value,initial,Token)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE user_id=3");
        clock.Step=TimeSpan.FromHours(3);
        Assert.Equal("gone",(await Run(initial)).Status);clock.Step=TimeSpan.Zero;clock.Current=DateTimeOffset.UtcNow;
        long audits=await Count("SELECT count(*) FROM public.audit_log");
        var saved=await mappingService.SaveAsync(owner,10,1,[new(2,"Alternative")],output.Context.MappingRevision,output.Context.ConfigurationKey,Token);
        Assert.Equal(MappingStatus.Success,saved.Status);
        Assert.Equal("conflict",(await Run(current)).Status);
        var refreshed=await Run(initial);Assert.Equal(4,refreshed.Output!.Mappings.Single(m=>m.ColumnId==2).SourcePosition);
        await Sql("UPDATE public.template_column_mappings SET reconciliation_role='footing' WHERE column_id=3");
        Assert.Equal("review",(await Run(initial)).Status);
        Assert.Equal(MappingStatus.Conflict,(await mappingService.ClearAsync(owner,10,1,saved.Snapshot!.Revision,saved.Snapshot.ConfigurationKey,Token)).Status);
        // Clear uses current configuration: obtain it after the approved fixture mutation.
        var definition=(await new TenantTemplateService(tx,audit).GetAsync(owner,1,Token)).Templates!.Single();
        var latest=(await mappingService.ReadAsync(owner,10,1,Token)).Snapshot!;
        await mappingService.ClearAsync(owner,10,1,latest.Revision,definition.ConfigurationKey,Token);
        var foot=await Run(initial);Assert.Equal("success",foot.Status);
        var footDates=await Run(current with {Operation="dates",ConfigurationKey=foot.Output!.Context.ConfigurationKey,MappingRevision=foot.Output.Context.MappingRevision});
        Assert.Equal("await_footing_validation",footDates.Output!.DateSuggestion!.Status);Assert.Null(footDates.Output.DateSuggestion.Start);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data"));Assert.Equal(0L,await Count("SELECT count(*) FROM public.upload_validation_plans"));
        Assert.True(await Count("SELECT count(*) FROM public.audit_log")>=audits); // Only explicit saved-mapping actions write audit.

        await using(var disabled=new WebApplicationFactory<Program>()) using(var http=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.PostAsJsonAsync($"/api/upload/file/{id}/prepare",initial,Token)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
        }));
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
        string route=$"/api/upload/file/{id}/prepare";
        client.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,initial,Token)).StatusCode);
        client.DefaultRequestHeaders.Remove("Origin");
        client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);
        Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,initial,Token)).StatusCode);
        client.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,initial,Token)).StatusCode);
        client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
        var response=await client.PostAsJsonAsync(route,initial,Token);Assert.Equal(HttpStatusCode.OK,response.StatusCode);
        Assert.Contains("no-store",response.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
        var body=await response.Content.ReadAsStringAsync(Token);Assert.DoesNotContain(directory,body,StringComparison.Ordinal);
        using(var oversized=new StringContent("{\"sheet\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json"))
            Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.PostAsync(route,oversized,Token)).StatusCode);
        using(var chunked=new HttpRequestMessage(HttpMethod.Post,route))
        {
            chunked.Headers.TransferEncodingChunked=true;
            chunked.Content=new StringContent("{\"sheet\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json");
            Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(chunked,Token)).StatusCode);
        }
        await Sql("UPDATE public.sessions SET context_version=context_version+1 WHERE user_id=1");
        Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,initial,Token)).StatusCode);
        await Sql("UPDATE public.sessions SET context_version=context_version-1 WHERE user_id=1");
        clock.Current=clock.Current.AddHours(3);Assert.Equal("gone",(await Run(initial)).Status);clock.Current=DateTimeOffset.UtcNow;
        var paths=Directory.GetFiles(directory,"*.bin");
        Assert.Equal(golden.Oracle.RootElement.GetProperty("sha256").GetString(),receipt.Sha256);
        foreach(string path in paths) await File.WriteAllTextAsync(path,"tampered",Token);
        Assert.Equal("gone",(await Run(initial)).Status);
        foreach(string path in paths) File.Delete(path);
        Assert.Equal("gone",(await Run(initial)).Status);
    }
}
