using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Columns;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Tenancy;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TenantColumnTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    [Fact]
    public async Task Column_identity_is_stable_and_tenant_labels_cannot_bypass_dependency_guards()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection=await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string sql) { await using var command=new NpgsqlCommand(sql,connection); await command.ExecuteNonQueryAsync(Token); }
        async Task<long> Count(string sql) { await using var command=new NpgsqlCommand(sql,connection); return (long)(await command.ExecuteScalarAsync(Token))!; }
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT NOT NULL,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,industry TEXT,contact_person TEXT,email TEXT,phone TEXT,is_active BOOLEAN DEFAULT TRUE,created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER REFERENCES public.clients(id));
            CREATE TABLE public.master_columns(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,column_name TEXT UNIQUE NOT NULL,data_type TEXT NOT NULL,category TEXT DEFAULT 'General',is_active BOOLEAN DEFAULT TRUE,notes TEXT);
            CREATE TABLE public.templates(id INTEGER PRIMARY KEY,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.template_column_mappings(template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id));
            CREATE TABLE public.audit_rule_document_fields(id INTEGER PRIMARY KEY,document_id INTEGER,master_column_id INTEGER,field_name TEXT);
            CREATE TABLE public.audit_rule_cross_checks(id INTEGER PRIMARY KEY,compare_field TEXT);
            CREATE TABLE public.saved_column_mappings(id INTEGER PRIMARY KEY,template_column_id INTEGER);
            ALTER TABLE public.client_data ADD COLUMN source_identity TEXT,ADD COLUMN source_fingerprint TEXT;
            CREATE TABLE public.client_data_history(id INTEGER PRIMARY KEY,row_data JSONB,source_identity TEXT);
            INSERT INTO public.master_columns(id,column_name,data_type,category,notes) OVERRIDING SYSTEM VALUE VALUES
                (1,'Legacy Key','Text','Legacy category','Legacy notes'),(2,'Unmapped legacy','Numeric','Legacy',NULL);
            SELECT setval(pg_get_serial_sequence('public.master_columns','id'),100,true);
            INSERT INTO public.users VALUES (1,'column-user','pending',TRUE,NULL,NULL),(2,'other-admin','pending',TRUE,'admin',NULL);
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(30,'C','Gamma');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',NULL),
                (repeat('b',64),2,now()+interval '1 hour',NULL),(repeat('c',64),1,now()+interval '1 hour',NULL),
                (repeat('d',64),2,now()+interval '1 hour',NULL),(repeat('e',64),1,now()-interval '1 hour',NULL);
            INSERT INTO public.templates VALUES(501,TRUE);
            """);
        await using(var hash=new NpgsqlCommand("UPDATE public.users SET password_hash=$1 WHERE id=1",connection))
        { hash.Parameters.AddWithValue(BCrypt.Net.BCrypt.HashPassword("p12-synthetic")); await hash.ExecuteNonQueryAsync(Token); }
        await fixture.ApplyTenantFoundationAsync(Token); await fixture.ApplyEngagementFoundationAsync(Token); await fixture.ApplyTenantUserRolesAsync(Token);
        var root=FindRoot();
        await Sql(await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0006_session_tenant_context.sql"),Token));
        await using(var rollback=await connection.BeginTransactionAsync(Token))
        {
            await using var candidate=new NpgsqlCommand(await PostgresFixture.ReadTenantColumnsAsync(Token),connection,rollback);
            await candidate.ExecuteNonQueryAsync(Token); await rollback.RollbackAsync(Token);
        }
        await using(var check=new NpgsqlCommand("SELECT to_regclass('public.platform_tenant_columns') IS NULL",connection))
            Assert.True((bool)(await check.ExecuteScalarAsync(Token))!);
        await fixture.ApplyTenantColumnsAsync(Token);
        Assert.Equal(PostgresErrorCodes.DuplicateTable,(await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyTenantColumnsAsync(Token))).SqlState);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_tenant_columns"));
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'Firm A','audit_firm'),(102,'Business B','business'),(103,'Firm C','audit_firm');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(103,1,'member'),(103,2,'admin');
            UPDATE public.sessions SET selected_tenant_id=CASE WHEN token IN(repeat('a',64),repeat('e',64)) THEN 101 WHEN token=repeat('b',64) THEN 102 ELSE 103 END;
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(30,103,now());
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,103,30,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,103,1,'internee');
            """);
        using var pool=fixture.CreatePool();
        var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions())); var limits=Options.Create(new LogPersistenceOptions());
        var audit=new PostgresAuditWriter(new LogSanitizer(limits),limits); var service=new TenantColumnService(tx,audit);
        var store=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions {Enabled=true}));
        var admin=(await store.FindAsync(new string('a',64),Token))!; var other=(await store.FindAsync(new string('b',64),Token))!;
        var member=(await store.FindAsync(new string('c',64),Token))!; var cAdmin=(await store.FindAsync(new string('d',64),Token))!;
        var created=await service.SaveAsync(admin,null,new("Invoice Date","Date","Dates","SYNTHETIC_NOTE"),Token);
        Assert.Equal(ColumnStatus.Success,created.Status); int id=created.ColumnId!.Value;
        async Task<TenantColumn> Current()=>Assert.Single((await service.ListAsync(admin,true,Token)).Columns!,c=>c.Id==id);
        var before=await Current(); Assert.NotEqual(before.Name,before.StorageKey); Assert.StartsWith("col_",before.StorageKey);
        Assert.Equal(ColumnStatus.Success,(await service.SaveAsync(other,null,new("Invoice Date","Date"),Token)).Status);
        Assert.Equal(ColumnStatus.Conflict,(await service.SaveAsync(admin,null,new("INVOICE DATE","Text"),Token)).Status);
        Assert.Equal(ColumnStatus.Forbidden,(await service.SaveAsync(member,null,new("No permission","Text"),Token)).Status);
        Assert.Equal(ColumnStatus.Success,(await service.SaveAsync(cAdmin,null,new("Read-only field","Text"),Token)).Status);
        Assert.Equal(ColumnStatus.NotFound,(await service.UsageAsync(other,id,Token)).Status);
        Assert.Equal(ColumnStatus.NotFound,(await service.SaveAsync(other,id,new("Attack","Text"),Token)).Status);
        Assert.Equal(ColumnStatus.NotFound,(await service.DeleteAsync(other,id,Token)).Status);
        Assert.Equal(ColumnStatus.Invalid,(await service.SaveAsync(admin,null,new("Role","Text",ReconciliationRole:"document_id"),Token)).Status);
        Assert.Equal(ColumnStatus.Invalid,(await service.SaveAsync(admin,null,new("Bad type","Document ID"),Token)).Status);
        Assert.Equal(ColumnStatus.Forbidden,(await service.ListAsync(admin with {UserId=2},true,Token)).Status);
        Assert.Equal(ColumnStatus.Forbidden,(await service.ListAsync(admin with {Token=new string('e',64)},true,Token)).Status);
        Assert.Equal(ColumnStatus.NotFound,(await service.UsageAsync(admin,2,Token)).Status);

        // Binding guards protect identity even from accidental direct SQL edits.
        foreach(var sql in new[] {$"UPDATE public.master_columns SET column_name='bad key' WHERE id={id}",
            $"UPDATE public.master_columns SET id=DEFAULT WHERE id={id}",
            $"UPDATE public.platform_tenant_columns SET tenant_id=102 WHERE column_id={id}",
            $"UPDATE public.platform_tenant_columns SET column_id=2 WHERE column_id={id}",
            $"UPDATE public.platform_tenant_columns SET binding_origin='legacy' WHERE column_id={id}",
            $"DELETE FROM public.platform_tenant_columns WHERE column_id={id}"})
            Assert.Equal(PostgresErrorCodes.CheckViolation,(await Assert.ThrowsAsync<PostgresException>(()=>Sql(sql))).SqlState);
        Assert.Equal(PostgresErrorCodes.ForeignKeyViolation,(await Assert.ThrowsAsync<PostgresException>(()=>Sql($"DELETE FROM public.master_columns WHERE id={id}"))).SqlState);

        // A label rename keeps the actual current/history JSON values, source
        // identities and fingerprints byte-equivalent; lookup still uses the key.
        var json=JsonSerializer.Serialize(new Dictionary<string,object> {[before.StorageKey]="2026-03-31",["Exact amount"]="001234.5600"});
        foreach(var sql in new[] {"INSERT INTO public.client_data(client_id,row_data,source_identity,source_fingerprint) VALUES(10,$1,'identity-unchanged','fingerprint-unchanged')",
            "INSERT INTO public.client_data_history VALUES(7,$1,'history-unchanged')"})
        {
            await using var source=new NpgsqlCommand(sql,connection);
            source.Parameters.AddWithValue(NpgsqlDbType.Jsonb,json); await source.ExecuteNonQueryAsync(Token);
        }
        async Task<string> Fingerprint()
        {
            await using var command=new NpgsqlCommand("SELECT md5(string_agg(row_data::text||source_identity||source_fingerprint,'' ORDER BY id))||(SELECT md5(string_agg(row_data::text||source_identity,'' ORDER BY id)) FROM public.client_data_history) FROM public.client_data",connection);
            return (string)(await command.ExecuteScalarAsync(Token))!;
        }
        var sourceBefore=await Fingerprint();
        Assert.Equal(ColumnStatus.Success,(await service.SaveAsync(admin,id,new("Supplier Invoice Date","Date","Dates","Updated note"),Token)).Status);
        var renamed=await Current(); Assert.Equal(before.StorageKey,renamed.StorageKey); Assert.Equal(before.Id,renamed.Id); Assert.True(renamed.Version>before.Version);
        Assert.Equal(sourceBefore,await Fingerprint());
        await using(var lookup=new NpgsqlCommand("SELECT row_data->>$1 FROM public.client_data WHERE client_id=10",connection))
        { lookup.Parameters.AddWithValue(renamed.StorageKey); Assert.Equal("2026-03-31",(string)(await lookup.ExecuteScalarAsync(Token))!); }
        Assert.Equal(ColumnStatus.DependencyReview,(await service.SaveAsync(admin,id,new(renamed.Name,"Text"),Token)).Status);
        Assert.Equal(ColumnStatus.DependencyReview,(await service.DeleteAsync(admin,id,Token)).Status);
        await Sql("DELETE FROM public.client_data"); // Synthetic fixture only: verify history alone also protects type/deactivation.
        Assert.Equal(ColumnStatus.DependencyReview,(await service.DeleteAsync(admin,id,Token)).Status);
        await Sql("DELETE FROM public.client_data_history");
        Assert.Equal(ColumnStatus.Success,(await service.UsageAsync(admin,id,Token)).Status);
        await Sql("INSERT INTO public.audit_rule_cross_checks VALUES(1,'Supplier Invoice Date')");
        // A coincidentally equal display label is not a permanent field binding.
        Assert.Equal(ColumnStatus.Success,(await service.UsageAsync(admin,id,Token)).Status);
        await Sql("DELETE FROM public.audit_rule_cross_checks");

        foreach(var (insert,clear) in new[] {
            ($"INSERT INTO public.template_column_mappings VALUES(501,{id})","DELETE FROM public.template_column_mappings"),
            ($"INSERT INTO public.audit_rule_document_fields VALUES(1,7,{id},'field')","DELETE FROM public.audit_rule_document_fields"),
            ($"INSERT INTO public.audit_rule_cross_checks VALUES(1,' {before.StorageKey} ')","DELETE FROM public.audit_rule_cross_checks"),
            ($"INSERT INTO public.saved_column_mappings VALUES(1,{id})","DELETE FROM public.saved_column_mappings")})
        {
            await Sql(insert);
            Assert.Equal(ColumnStatus.DependencyReview,(await service.UsageAsync(admin,id,Token)).Status);
            Assert.Equal(ColumnStatus.DependencyReview,(await service.SaveAsync(admin,id,new("Blocked rename","Date"),Token)).Status);
            Assert.Equal(ColumnStatus.DependencyReview,(await service.SaveAsync(admin,id,new(renamed.Name,"Numeric"),Token)).Status);
            Assert.Equal(ColumnStatus.DependencyReview,(await service.DeleteAsync(admin,id,Token)).Status);
            Assert.Equal(ColumnStatus.Success,(await service.SaveAsync(admin,id,new(renamed.Name,"Date","Updated category","Safe tenant note"),Token)).Status);
            await Sql(clear);
        }
        await Sql("INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,category,notes) VALUES(1,101,'Legacy display','Tenant category','Tenant notes')");
        Assert.Equal(ColumnStatus.DependencyReview,(await service.SaveAsync(admin,1,new("Changed legacy display","Text"),Token)).Status);
        Assert.Equal(ColumnStatus.DependencyReview,(await service.DeleteAsync(admin,1,Token)).Status);
        Assert.Equal(ColumnStatus.Success,(await service.SaveAsync(admin,1,new("Legacy display","Text","Local only","Local annotation"),Token)).Status);
        await using(var legacy=new NpgsqlCommand("SELECT column_name,category,notes FROM public.master_columns WHERE id=1",connection))
        { await using var reader=await legacy.ExecuteReaderAsync(Token); await reader.ReadAsync(Token); Assert.Equal("Legacy Key",reader.GetString(0)); Assert.Equal("Legacy category",reader.GetString(1)); Assert.Equal("Legacy notes",reader.GetString(2)); }
        await Sql("ALTER TABLE public.audit_rule_cross_checks RENAME TO missing_dependency");
        try { await Assert.ThrowsAsync<PostgresException>(()=>service.DeleteAsync(admin,id,Token)); }
        finally { await Sql("ALTER TABLE public.missing_dependency RENAME TO audit_rule_cross_checks"); }

        var stable=await Current(); var count=await Count("SELECT count(*) FROM public.master_columns");
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_unavailable");
        try
        {
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveAsync(admin,null,new("ROLLBACK","Text"),Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveAsync(admin,id,new("ROLLBACK","Numeric"),Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.DeleteAsync(admin,id,Token));
        }
        finally { await Sql("ALTER TABLE public.audit_unavailable RENAME TO audit_log"); }
        Assert.Equal(stable,await Current()); Assert.Equal(count,await Count("SELECT count(*) FROM public.master_columns"));
        var race=await Task.WhenAll(service.SaveAsync(admin,null,new("Race label","Text"),Token),service.SaveAsync(admin,null,new("RACE LABEL","Text"),Token));
        Assert.Single(race,r=>r.Status==ColumnStatus.Success); Assert.Single(race,r=>r.Status==ColumnStatus.Conflict);
        Assert.Equal(ColumnStatus.Success,(await service.DeleteAsync(admin,id,Token)).Status);
        Assert.DoesNotContain((await service.ListAsync(admin,false,Token)).Columns!,c=>c.Id==id);
        Assert.Equal(ColumnStatus.Conflict,(await service.SaveAsync(admin,null,new(stable.Name,"Date"),Token)).Status);
        Assert.Equal(ColumnStatus.Success,(await service.SaveAsync(admin,id,new(stable.Name,stable.Type,stable.Category,stable.Notes,true),Token)).Status);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.master_columns m LEFT JOIN public.platform_tenant_columns c ON c.column_id=m.id WHERE left(m.column_name,4)='col_' AND c.column_id IS NULL"));
        var sessions=new TenantSessionService(tx,audit);
        Assert.True(await sessions.SelectAsync(admin,103,TenantSessionService.ContextKey(admin),Token));
        Assert.Equal(ColumnStatus.Forbidden,(await service.SaveAsync(admin,null,new("STALE","Text"),Token)).Status);
        await Sql("UPDATE public.sessions SET selected_tenant_id=101,context_version=context_version+1 WHERE token=repeat('a',64)");
        admin=(await store.FindAsync(admin.Token,Token))!;
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='member' WHERE tenant_id=101 AND user_id=1");
        Assert.Equal(ColumnStatus.Forbidden,(await service.SaveAsync(admin,null,new("Revoked","Text"),Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='admin' WHERE tenant_id=101 AND user_id=1");
        await using(var entry=new NpgsqlCommand("SELECT details FROM public.audit_log WHERE module='TenantColumns' AND action='Create' ORDER BY id LIMIT 1",connection))
        { var details=(string)(await entry.ExecuteScalarAsync(Token))!; Assert.Contains("tenant_id",details); Assert.DoesNotContain("SYNTHETIC_NOTE",details); }

        await using(var disabled=new WebApplicationFactory<Program>()) using(var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync("/api/columns",Token)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>
        {
            builder.UseEnvironment("Testing"); builder.ConfigureServices(services=>
            {
                services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
                services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5080";});
                services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true); services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true); services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
            });
        });
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        var address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        using var http=new HttpClient(new HttpClientHandler {UseCookies=false,AllowAutoRedirect=false}) {BaseAddress=new Uri(address)};
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/columns",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/API/COLUMNS/",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(admin)); http.DefaultRequestHeaders.Add("X-Tenant-Id","102");
        using(var list=await http.GetAsync("/api/columns",Token))
        {
            Assert.Equal(HttpStatusCode.OK,list.StatusCode); using var jsonResult=JsonDocument.Parse(await list.Content.ReadAsStringAsync(Token));
            Assert.DoesNotContain(jsonResult.RootElement.EnumerateArray(),c=>c.GetProperty("column_name").GetString()=="Invoice Date");
            Assert.False(jsonResult.RootElement[0].TryGetProperty("is_active",out _)); Assert.True(jsonResult.RootElement[0].TryGetProperty("storage_key",out _));
        }
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PostAsJsonAsync("/api/columns",new {column_name="HTTP field",data_type="Text"},Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin",address);
        using(var response=await http.PostAsJsonAsync("/api/columns",new {column_name="HTTP field",data_type="Text",storage_key="Legacy Key",column_id=1,tenant_id=102,binding_origin="legacy"},Token))
        {
            Assert.Equal(HttpStatusCode.OK,response.StatusCode);
            using var result=JsonDocument.Parse(await response.Content.ReadAsStringAsync(Token));
            var serverId=result.RootElement.GetProperty("column_id").GetInt32(); Assert.NotEqual(1,serverId);
            var serverColumn=Assert.Single((await service.ListAsync(admin,true,Token)).Columns!,c=>c.Id==serverId);
            Assert.StartsWith("col_",serverColumn.StorageKey); Assert.Equal("native",serverColumn.Origin);
        }
        Assert.Equal(HttpStatusCode.OK,(await http.PutAsJsonAsync($"/api/columns/{id}",new {column_name=stable.Name,data_type="Date",is_active=1},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.PostAsJsonAsync("/api/columns",new {data_type="Text"},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/api/columns/categories",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/api/columns/close-matches?name=Supplier",Token)).StatusCode);
        var start=new ProcessStartInfo(Path.Combine(root,"..","venv","Scripts","python.exe")) {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root,"tests","p12_browser.py")); start.ArgumentList.Add(address);
        using var process=Process.Start(start)!; var output=process.StandardOutput.ReadToEndAsync(Token); var errors=process.StandardError.ReadToEndAsync(Token);
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(Token); timeout.CancelAfter(TimeSpan.FromSeconds(100));
        try { await process.WaitForExitAsync(timeout.Token); } catch { process.Kill(entireProcessTree:true); throw; }
        Assert.True(process.ExitCode==0,await output+await errors);
    }
    private static string FindRoot()
    {
        var directory=new DirectoryInfo(AppContext.BaseDirectory);
        while(directory is not null&&!File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx"))) directory=directory.Parent;
        return directory?.FullName??throw new InvalidOperationException("Solution root required");
    }
}
