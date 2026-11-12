using System.IO.Compression;
using System.Text;
using System.Security.Cryptography;
using System.Net;
using System.Net.Http.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Uploads;
using Microsoft.Extensions.Options;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class UploadStagingTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private sealed class Clock : TimeProvider
    {
        public DateTimeOffset Current=DateTimeOffset.UtcNow;
        public override DateTimeOffset GetUtcNow()=>Current;
    }
    private sealed class ArmedStream(byte[] bytes,Action arm) : MemoryStream(bytes)
    {
        public override async ValueTask<int> ReadAsync(Memory<byte> buffer,CancellationToken ct=default)
        { int count=await base.ReadAsync(buffer,ct); if(count==0) arm(); return count; }
    }
    private static byte[] Workbook(bool oversized=false)
    {
        using var bytes=new MemoryStream();
        using(var zip=new ZipArchive(bytes,ZipArchiveMode.Create,true))
        {
            void Add(string path,string xml) { using var writer=new StreamWriter(zip.CreateEntry(path).Open(),Encoding.UTF8); writer.Write(xml); }
            Add("[Content_Types].xml","""
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>
                """);
            Add("_rels/.rels","""
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>
                """);
            Add("xl/workbook.xml","""
                <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="First" sheetId="1" r:id="rId1"/><sheet name="Second" sheetId="2" r:id="rId2"/></sheets></workbook>
                """);
            Add("xl/_rels/workbook.xml.rels","""
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>
                """);
            const string sheet="""
                <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:D2"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>ERP &lt;b&gt;Reference&lt;/b&gt;</t></is></c><c r="B1"/><c r="C1" t="inlineStr"><is><t>ERP &lt;b&gt;Reference&lt;/b&gt;</t></is></c><c r="D1"><v>1</v></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>private detail</t></is></c></row></sheetData></worksheet>
                """;
            Add("xl/worksheets/sheet1.xml",sheet); Add("xl/worksheets/sheet2.xml",sheet);
            if(oversized) Add("large.xml",new string('x',2000000));
        }
        return bytes.ToArray();
    }
    private static byte[] BinaryWorkbook()
    {
        // Minimal standalone BIFF2 worksheet: BOF, dimensions, one LABEL, EOF.
        using var bytes=new MemoryStream(); using var writer=new BinaryWriter(bytes);
        void U(int value)=>writer.Write((ushort)value);
        U(0x0009); U(4); U(0); U(0x0010);
        U(0x0000); U(8); U(0); U(1); U(0); U(1);
        U(0x0004); U(11); U(0); U(0); writer.Write(new byte[3]); writer.Write((byte)3); writer.Write(Encoding.ASCII.GetBytes("ERP"));
        U(0x000A); U(0); return bytes.ToArray();
    }

    [Fact]
    public async Task ReceiptDiscoveryOwnershipExpiryRecoveryAndHttpAreGuarded()
    {
        var ct=TestContext.Current.CancellationToken;
        await fixture.ApplyAuditSchemaAsync(ct);
        await using var connection=await fixture.OpenControlConnectionAsync(ct);
        async Task Sql(string sql) { await using var command=new NpgsqlCommand(sql,connection); await command.ExecuteNonQueryAsync(ct); }
        async Task<long> Count(string sql) { await using var command=new NpgsqlCommand(sql,connection); return (long)(await command.ExecuteScalarAsync(ct))!; }
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER,selected_tenant_id INTEGER,context_version BIGINT NOT NULL DEFAULT 0);
            CREATE TABLE public.master_columns(id INTEGER PRIMARY KEY,column_name TEXT,data_type TEXT,is_active BOOLEAN DEFAULT TRUE,category TEXT,notes TEXT);
            CREATE TABLE public.templates(id INTEGER PRIMARY KEY,template_name TEXT,created_by TEXT DEFAULT 'Admin',created_date TIMESTAMPTZ DEFAULT now(),is_active BOOLEAN DEFAULT TRUE,is_system BOOLEAN DEFAULT FALSE,has_subtotals_by_default BOOLEAN DEFAULT FALSE,data_purpose TEXT DEFAULT 'standard',ledger_columns TEXT DEFAULT '{}',rate_source TEXT);
            CREATE TABLE public.template_column_mappings(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id),sort_order INTEGER,reconciliation_role TEXT,compare_for_changes BOOLEAN,comparison_review_required BOOLEAN DEFAULT FALSE);
            CREATE TABLE public.uploaded_files(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,file_name TEXT,file_path TEXT,file_type TEXT,record_count INTEGER,status TEXT,uploaded_by TEXT,client_id INTEGER,template_id INTEGER);
            INSERT INTO public.users VALUES(1,'owner','unused',TRUE,'admin',NULL),(2,'other','unused',TRUE,'admin',NULL),(3,'auditor','unused',TRUE,'admin',NULL);
            INSERT INTO public.clients VALUES(10,'A','Alpha',TRUE),(20,'B','Beta',TRUE);
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0);
            INSERT INTO public.master_columns VALUES(1,'col_fixture','Text',TRUE,'General',NULL);
            INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture');
            """);
        await fixture.ApplyTenantFoundationAsync(ct); await fixture.ApplyEngagementFoundationAsync(ct); await fixture.ApplyTenantUserRolesAsync(ct);
        await fixture.ApplyTenantColumnsAsync(ct); await fixture.ApplyTenantTemplatesAsync(ct); await fixture.ApplyUploadStagingAsync(ct);
        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadStagingAsync(ct));
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Reference','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Fixture','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,tenant_id,storage_key) VALUES(1,1,0,101,'col_fixture');
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            """);
        using var pool=fixture.CreatePool(); var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var logOptions=Options.Create(new LogPersistenceOptions()); var audit=new PostgresAuditWriter(new LogSanitizer(logOptions),logOptions);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions{Enabled=true}));
        var owner=(await sessions.FindAsync(new string('a',64),ct))!; var other=(await sessions.FindAsync(new string('b',64),ct))!; var staff=(await sessions.FindAsync(new string('c',64),ct))!;
        Assert.NotNull(owner); Assert.NotNull(other); Assert.NotNull(staff);
        var directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P16-"+Guid.NewGuid().ToString("N"));
        var options=Options.Create(new UploadStagingOptions {Enabled=true,RootDirectory=directory,LifetimeMinutes=1});
        var clock=new Clock(); var service=new UploadStagingService(tx,audit,options,clock);
        var bytes=Workbook(); var request=Guid.NewGuid();
        async Task<StagingResult> Receive(Guid key,VerifiedSession actor,byte[]? data=null,string name="source.xlsx")
        { using var input=new MemoryStream(data??bytes); return await service.ReceiveAsync(actor,10,1,key,name,input,ct); }
        var result=await Receive(request,owner); Assert.Equal("success",result.Status); Assert.Equal("ready",result.State);
        Assert.Equal(Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant(),result.Sha256);
        Assert.Equal(2,result.Sheets!.Count); Assert.Equal(4,result.Sheets[0].Headers.Count);
        Assert.Equal("ERP <b>Reference</b>",result.Sheets[0].Headers[0].Value.GetString());
        Assert.Equal(result.Sheets[0].Headers[0].Value.GetString(),result.Sheets[0].Headers[2].Value.GetString());
        Assert.Equal("conflict",(await Receive(request,owner)).Status);
        Assert.Equal("forbidden",(await Receive(request,other)).Status);
        Assert.Equal("not_found",(await service.StatusAsync(staff,10,1,request,null,false,ct)).Status);
        Assert.Equal("forbidden",(await service.StatusAsync(owner with {UserId=2},10,1,request,null,false,ct)).Status);
        var staffUpload=await Receive(Guid.NewGuid(),staff); Assert.Equal("success",staffUpload.Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3");
        Assert.Equal("forbidden",(await service.StatusAsync(staff,10,1,staffUpload.RequestId,null,false,ct)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE user_id=3");
        var invalid=await Receive(Guid.NewGuid(),owner,Encoding.UTF8.GetBytes("<html><table>bad</table></html>")); Assert.Equal("invalid",invalid.Status);
        Assert.Equal("rejected",invalid.State);
        Assert.Equal("invalid",(await Receive(Guid.NewGuid(),owner,[],"empty.xlsx")).Status);
        Assert.Equal("conflict",(await service.StatusAsync(owner,10,1,request,null,true,ct)).Status);
        Assert.Equal("success",(await service.StatusAsync(owner,10,1,request,null,false,ct)).Status);
        var staged=Directory.GetFiles(directory,"*.bin"); Assert.Equal(2,staged.Length);
        foreach(var path in staged) await File.WriteAllTextAsync(path,"tampered",ct);
        Assert.Equal("gone",(await service.StatusAsync(owner,10,1,request,null,false,ct)).Status);
        clock.Current=clock.Current.AddMinutes(5);
        var expired=await service.StatusAsync(owner,10,1,request,null,true,ct);
        Assert.Equal("expired",expired.State); Assert.Equal("success",expired.Status);
        Assert.Equal(1L,await Count($"SELECT count(*) FROM public.platform_upload_staging WHERE request_id='{request}' AND discovery IS NULL"));
        Assert.Single(Directory.GetFiles(directory,"*.bin"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.audit_log WHERE details::text LIKE '%source.xlsx%' OR details::text LIKE '%private detail%'"));
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("UPDATE public.platform_upload_staging SET user_id=2"));
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_unavailable");
        var before=await Count("SELECT count(*) FROM public.platform_upload_staging");
        try { await Assert.ThrowsAsync<PostgresException>(()=>Receive(Guid.NewGuid(),owner)); }
        finally { await Sql("ALTER TABLE public.audit_unavailable RENAME TO audit_log"); }
        Assert.Equal(before,await Count("SELECT count(*) FROM public.platform_upload_staging"));
        clock.Current=DateTimeOffset.UtcNow;
        var binary=await Receive(Guid.NewGuid(),owner,BinaryWorkbook(),"legacy.xls");
        Assert.Equal("success",binary.Status); Assert.Equal("ERP",Assert.Single(Assert.Single(binary.Sheets!).Headers).Value.GetString());
        await using(var keyQuery=new NpgsqlCommand("SELECT storage_key FROM public.platform_upload_staging WHERE request_id=$1",connection))
        {
            keyQuery.Parameters.AddWithValue(binary.RequestId!.Value); var key=(Guid)(await keyQuery.ExecuteScalarAsync(ct))!;
            File.Delete(Path.Combine(directory,key.ToString("N")+".bin"));
            Assert.Equal("staged_file_missing",(await service.StatusAsync(owner,10,1,binary.RequestId,null,false,ct)).Code);
        }
        options.Value.MaximumFileBytes=1000000; options.Value.MaximumExpandedBytes=1000000;
        Assert.Equal("invalid",(await Receive(Guid.NewGuid(),owner,Workbook(true),"expansion.xls")).Status);
        options.Value.MaximumFileBytes=1;
        Assert.Equal("invalid",(await Receive(Guid.NewGuid(),owner)).Status);
        options.Value.MaximumFileBytes=64L*1024*1024; options.Value.MaximumExpandedBytes=256L*1024*1024;
        await Sql("CREATE FUNCTION public.reject_ready() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.module='UploadStaging' AND NEW.action='Ready' THEN RAISE EXCEPTION 'synthetic audit failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER reject_ready BEFORE INSERT ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.reject_ready()");
        var uncertain=Guid.NewGuid();
        try { await Assert.ThrowsAsync<PostgresException>(()=>Receive(uncertain,owner)); }
        finally { await Sql("DROP TRIGGER reject_ready ON public.audit_log; DROP FUNCTION public.reject_ready()"); }
        Assert.Equal("receiving",(await service.StatusAsync(owner,10,1,uncertain,null,false,ct)).State);
        await using(var relay=fixture.CreateCommitProxy())
        {
            using var proxiedPool=fixture.CreatePool(1,3,relay);
            var proxied=new UploadStagingService(new PostgresTransactions(proxiedPool,Options.Create(new PostgresOptions())),audit,options);
            var lost=Guid.NewGuid(); using var stream=new ArmedStream(bytes,relay.Arm);
            await Assert.ThrowsAsync<PostgresCommitOutcomeUnknownException>(()=>proxied.ReceiveAsync(owner,10,1,lost,"lost.xlsx",stream,ct));
            await relay.AcknowledgementDropped;
            Assert.Equal("success",(await service.StatusAsync(owner,10,1,lost,null,false,ct)).Status);
            Assert.Equal("conflict",(await Receive(lost,owner)).Status);
        }

        await using(var disabled=new WebApplicationFactory<Program>()) using(var http=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.GetAsync("/api/upload/file/1",ct)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
            services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true); services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true); services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});
        }));
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        var address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
        Assert.Equal(HttpStatusCode.Unauthorized,(await client.GetAsync("/api/upload/file/1?client_id=10&template_id=1",ct)).StatusCode);
        client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await client.GetAsync("/api/upload/file/1?client_id=10&template_id=1",ct)).StatusCode);
        client.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(owner));
        client.DefaultRequestHeaders.Add("X-Upload-Request",Guid.NewGuid().ToString());
        MultipartFormDataContent Body() { var content=new MultipartFormDataContent(); content.Add(new ByteArrayContent(bytes),"file","fixture.xlsx"); return content; }
        using(var body=Body()) Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsync("/api/import-excel?client_id=10&template_id=1",body,ct)).StatusCode);
        client.DefaultRequestHeaders.Add("Origin",address);
        using(var body=Body())
        {
            var response=await client.PostAsync("/api/import-excel?client_id=10&template_id=1",body,ct);
            Assert.Equal(HttpStatusCode.OK,response.StatusCode);
            var json=await response.Content.ReadFromJsonAsync<System.Text.Json.JsonElement>(ct);
            var id=json.GetProperty("file_id").GetInt32();
            Assert.Equal(HttpStatusCode.OK,(await client.GetAsync($"/api/upload/file/{id}/headers?client_id=10&template_id=1&sheet=First",ct)).StatusCode);
            Assert.Equal(HttpStatusCode.BadRequest,(await client.GetAsync($"/api/upload/file/{id}/headers?client_id=10&template_id=1&sheet=Missing",ct)).StatusCode);
        }
    }
}
