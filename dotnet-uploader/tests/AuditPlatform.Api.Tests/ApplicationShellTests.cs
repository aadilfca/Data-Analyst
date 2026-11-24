using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Clients;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Users;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ApplicationShellTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    [Fact]
    public async Task Shell_selection_rejects_stale_context_and_hosts_existing_browser_workflow()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection=await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string value)
        { await using var command=new NpgsqlCommand(value,connection); await command.ExecuteNonQueryAsync(Token); }
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_code TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,industry TEXT,contact_person TEXT,email TEXT,phone TEXT,
                is_active BOOLEAN DEFAULT TRUE,created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),
                expiry TIMESTAMPTZ,selected_client_id INTEGER REFERENCES public.clients(id));
            INSERT INTO public.clients(id,client_code,client_name) OVERRIDING SYSTEM VALUE VALUES (10,'A','Alpha'),(20,'B','Beta');
            INSERT INTO public.users VALUES (1,'shell-user','pending',TRUE,NULL,NULL),(2,'other','pending',TRUE,'admin',NULL);
            INSERT INTO public.sessions VALUES (repeat('a',64),1,now()+interval '1 hour',10);
            """);
        await using (var password=new NpgsqlCommand("UPDATE public.users SET password_hash=$1 WHERE id=1",connection))
        { password.Parameters.AddWithValue(BCrypt.Net.BCrypt.HashPassword("p10-synthetic")); await password.ExecuteNonQueryAsync(Token); }
        await fixture.ApplyTenantFoundationAsync(Token);
        await fixture.ApplyEngagementFoundationAsync(Token);
        await fixture.ApplyTenantUserRolesAsync(Token);
        var root=FindRoot();
        var candidate=await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0006_session_tenant_context.sql"),Token);
        await using (var rollback=await connection.BeginTransactionAsync(Token))
        {
            await using var command=new NpgsqlCommand(candidate,connection,rollback);
            await command.ExecuteNonQueryAsync(Token); await rollback.RollbackAsync(Token);
        }
        await using (var check=new NpgsqlCommand("SELECT count(*) FROM information_schema.columns WHERE table_name='sessions' AND column_name='selected_tenant_id'",connection))
            Assert.Equal(0L,(long)(await check.ExecuteScalarAsync(Token))!);
        await Sql(candidate);
        var repeated=await Assert.ThrowsAsync<PostgresException>(()=>Sql(candidate));
        Assert.Equal(PostgresErrorCodes.DuplicateColumn,repeated.SqlState);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES
                (101,'Firm A','audit_firm'),(102,'Firm B','audit_firm'),(103,'Unrelated business','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES
                (101,1,'admin'),(102,1,'member'),(102,2,'admin');
            INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES (10,101),(20,102);
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES (201,102,20,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES (201,102,1,'internee');
            """);
        using var pool=fixture.CreatePool();
        var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var limits=Options.Create(new LogPersistenceOptions());
        var audit=new PostgresAuditWriter(new LogSanitizer(limits),limits);
        var store=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions {Enabled=true}));
        var service=new TenantSessionService(tx,audit);
        var initial=(await store.FindAsync(new string('a',64),Token))!;
        Assert.True(initial.TenantContextRequired); Assert.Null(initial.TenantId);
        var choices=await service.ReadAsync(initial,Token);
        Assert.Equal(2,choices.Tenants.Count); Assert.DoesNotContain(choices.Tenants,t=>t.Id==103);
        Assert.False(await service.SelectAsync(initial,103,choices.ContextKey!,Token));
        Assert.True(await service.SelectAsync(initial,101,choices.ContextKey!,Token));
        var inA=(await store.FindAsync(initial.Token,Token))!;
        Assert.Equal(101,inA.TenantId); Assert.Null(inA.ClientId); Assert.Equal(1,inA.ContextVersion);
        var users=new TenantUserService(tx,audit);
        var clients=new TenantClientService(tx,audit);
        Assert.Equal(101,(await users.ResolveSoleTenantAsync(inA,Token)).TenantId);
        Assert.Equal(ClientStatus.Success,(await clients.SelectionAsync(inA,10,Token)).Status);
        Assert.True(await service.SelectAsync(inA,102,TenantSessionService.ContextKey(inA),Token));
        var inB=(await store.FindAsync(initial.Token,Token))!;
        Assert.Equal(102,inB.TenantId); Assert.Null(inB.ClientId);
        Assert.False(await service.SelectAsync(inA,101,TenantSessionService.ContextKey(inA),Token));
        Assert.Equal(TenantUserStatus.Forbidden,(await users.ListAsync(inA,101,true,Token)).Status);
        Assert.Equal(ClientStatus.Forbidden,(await clients.SaveAsync(inA,null,new("STALE","Must not write"),Token)).Status);
        Assert.Equal(TenantUserStatus.Forbidden,(await users.ResolveSoleTenantAsync(inB,Token)).Status);
        Assert.Equal(20,Assert.Single((await clients.ListAsync(inB,false,null,null,0,Token)).Clients!).Id);
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_unavailable");
        try { await Assert.ThrowsAsync<PostgresException>(()=>service.SelectAsync(inB,101,TenantSessionService.ContextKey(inB),Token)); }
        finally { await Sql("ALTER TABLE public.audit_unavailable RENAME TO audit_log"); }
        Assert.Equal(inB.ContextVersion,(await store.FindAsync(initial.Token,Token))!.ContextVersion);

        await using (var disabled=new WebApplicationFactory<Program>())
        using (var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.NotFound,(await client.GetAsync("/static/login.html",Token)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>
        {
            builder.UseEnvironment("Testing");
            builder.ConfigureServices(services=>
            {
                services.PostConfigure<PostgresOptions>(o=> {o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
                services.PostConfigure<AuthenticationOptions>(o=> {o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5080";});
                services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);
                services.PostConfigure<VerificationRuntimeOptions>(o=>o.Enabled=false);
            });
        });
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        var address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        using var http=new HttpClient(new HttpClientHandler {UseCookies=false,AllowAutoRedirect=false}) {BaseAddress=new Uri(address)};
        var origin=http.BaseAddress!.GetLeftPart(UriPartial.Authority);
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=origin;
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/static/login.html",Token)).StatusCode);
        Assert.Equal("/static/login.html",(await http.GetAsync("/static/clients.html",Token)).Headers.Location!.ToString());
        Assert.Equal(HttpStatusCode.NotFound,(await http.GetAsync("/static/.env",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound,(await http.GetAsync("/static/backup.sql",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+initial.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/API/CLIENTS/",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(inA));
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/api/users",Token)).StatusCode);
        http.DefaultRequestHeaders.Remove("X-App-Context");
        http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(inB));
        await Sql("UPDATE public.sessions SET selected_client_id=20 WHERE token=repeat('a',64)");
        var verificationUnavailable=await http.GetAsync("/static/verification.html",Token);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,verificationUnavailable.StatusCode);
        Assert.Equal("Verification is unavailable.",await verificationUnavailable.Content.ReadAsStringAsync(Token));
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/api/clients",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden,(await http.GetAsync("/api/users",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden,(await http.GetAsync("/static/users.html",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/static/settings.html",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/static/client_settings.html?client_id=10",Token)).StatusCode);
        var bookmark=await http.GetAsync("/static/account_control_center.html?setup=notes&upload_id=7",Token);
        Assert.Contains("workflow=financial",bookmark.Headers.Location!.ToString());
        Assert.Contains("upload_id=7",bookmark.Headers.Location!.ToString());
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PutAsJsonAsync("/api/auth/select-tenant",new {tenantId=101,contextKey=TenantSessionService.ContextKey(inB)},Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin",origin);
        Assert.Equal(HttpStatusCode.OK,(await http.PutAsJsonAsync("/api/auth/select-tenant",new {tenantId=101,contextKey=TenantSessionService.ContextKey(inB)},Token)).StatusCode);
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE tenant_id=101 AND user_id=1");
        Assert.Null((await service.ReadAsync((await store.FindAsync(initial.Token,Token))!,Token)).SelectedTenantId);
        Assert.Equal("/shell/tenant_select.html",(await http.GetAsync("/static/client_select.html",Token)).Headers.Location!.ToString());
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE tenant_id=101 AND user_id=1");

        var concurrentSession=(await store.FindAsync(initial.Token,Token))!;
        var switches=await Task.WhenAll(
            service.SelectAsync(concurrentSession,101,TenantSessionService.ContextKey(concurrentSession),Token),
            service.SelectAsync(concurrentSession,102,TenantSessionService.ContextKey(concurrentSession),Token));
        Assert.Single(switches,result=>result);

        using var shellHttp=new HttpClient { BaseAddress=new Uri(origin) };
        var shellPage=await shellHttp.GetAsync("/static/index.html",Token);
        Assert.Equal(HttpStatusCode.OK,shellPage.StatusCode);
        Assert.Equal("text/html",shellPage.Content.Headers.ContentType?.MediaType);
    }
    private static string FindRoot()
    {
        var directory=new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null && !File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx"))) directory=directory.Parent;
        return directory?.FullName ?? throw new InvalidOperationException("Solution root is required for owned fixtures");
    }
}
