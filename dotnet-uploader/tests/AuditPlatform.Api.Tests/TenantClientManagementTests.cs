using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Clients;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TenantClientManagementTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;

    [Fact]
    public async Task Client_records_and_selection_enforce_home_assignment_and_atomic_audit()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string text)
        {
            await using var command = new NpgsqlCommand(text, connection);
            await command.ExecuteNonQueryAsync(Token);
        }
        async Task<long> Count(string text)
        {
            await using var command = new NpgsqlCommand(text, connection);
            return (long)(await command.ExecuteScalarAsync(Token))!;
        }
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_code TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,industry TEXT,contact_person TEXT,email TEXT,phone TEXT,
                is_active BOOLEAN DEFAULT TRUE,created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),
                expiry TIMESTAMPTZ,selected_client_id INTEGER REFERENCES public.clients(id));
            INSERT INTO public.users VALUES (1,'admin','hash',TRUE,NULL,NULL),(2,'staff','hash',TRUE,NULL,NULL),
                (3,'other','hash',TRUE,'admin',NULL),(4,'business','hash',TRUE,NULL,NULL),(5,'ambiguous','hash',TRUE,NULL,NULL);
            INSERT INTO public.sessions VALUES
                (repeat('a',64),1,now()+interval '1 hour',NULL),(repeat('b',64),2,now()+interval '1 hour',NULL),
                (repeat('c',64),3,now()+interval '1 hour',NULL),(repeat('d',64),4,now()+interval '1 hour',NULL),
                (repeat('e',64),5,now()+interval '1 hour',NULL),(repeat('f',64),1,now()-interval '1 hour',NULL);
            """);
        await fixture.ApplyTenantFoundationAsync(Token);
        await fixture.ApplyEngagementFoundationAsync(Token);
        await fixture.ApplyTenantUserRolesAsync(Token);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES
                (101,'Firm A','audit_firm'),(102,'Firm B','audit_firm'),(103,'Business','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES
                (101,1,'admin'),(101,2,'member'),(102,3,'admin'),(103,4,'admin'),(101,5,'member'),(102,5,'member');
            """);
        using var pool = fixture.CreatePool();
        var transactions = new PostgresTransactions(pool, Options.Create(new PostgresOptions()));
        var limits = Options.Create(new LogPersistenceOptions());
        var service = new TenantClientService(transactions, new PostgresAuditWriter(new LogSanitizer(limits),limits));
        var a = await service.SaveAsync(Session('a',1), null, new("A","Alpha","Textiles"),Token);
        var b = await service.SaveAsync(Session('a',1), null, new("B","Beta"),Token);
        var other = await service.SaveAsync(Session('c',3), null, new("C","Other firm"),Token);
        var business = await service.SaveAsync(Session('d',4), null, new("D","Business records"),Token);
        Assert.All(new[] {a,b,other,business}, r=>Assert.Equal(ClientStatus.Success,r.Status));
        var id = a.ClientId!.Value;
        Assert.Equal(4L,await Count("SELECT count(*) FROM public.platform_tenant_client_homes"));
        Assert.Equal(ClientStatus.Conflict,(await service.SaveAsync(Session('c',3),null,new("A","Duplicate"),Token)).Status);
        Assert.Equal(ClientStatus.Forbidden,(await service.SaveAsync(Session('b',2),null,new("E","No permission"),Token)).Status);
        Assert.Equal(ClientStatus.NotFound,(await service.GetAsync(Session('c',3),id,Token)).Status);
        Assert.Equal(ClientStatus.NotFound,(await service.SaveAsync(Session('c',3),id,new("A","Attack"),Token)).Status);
        Assert.Equal(ClientStatus.NotFound,(await service.SetActiveAsync(Session('c',3),id,false,Token)).Status);
        Assert.Equal(ClientStatus.Forbidden,(await service.ListAsync(Session('f',1),false,null,null,0,Token)).Status);
        Assert.Equal(ClientStatus.Forbidden,(await service.ListAsync(Session('a',3),false,null,null,0,Token)).Status);
        Assert.Equal(ClientStatus.AmbiguousTenant,(await service.ListAsync(Session('e',5),false,null,null,0,Token)).Status);
        Assert.Empty((await service.ListAsync(Session('b',2),true,null,null,0,Token)).Clients!);
        await Sql($"""
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES
                (201,101,{id},'Annual audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES
                (201,101,2,'internee');
            """);
        var staff = await service.ListAsync(Session('b',2),true,null,null,0,Token);
        Assert.Equal(id,Assert.Single(staff.Clients!).Id);
        Assert.Equal(ClientStatus.Forbidden,(await service.GetAsync(Session('b',2),id,Token)).Status);
        Assert.Equal(ClientStatus.Success,(await service.SelectionAsync(Session('b',2),id,Token)).Status);
        await Sql("UPDATE public.platform_audit_engagements SET is_active=FALSE WHERE id=201");
        Assert.Empty((await service.ListAsync(Session('b',2),false,null,null,0,Token)).Clients!);
        Assert.Equal(ClientStatus.NotFound,(await service.SelectionAsync(Session('b',2),id,Token)).Status);
        await Sql("UPDATE public.platform_audit_engagements SET is_active=TRUE WHERE id=201");
        Assert.Equal(ClientStatus.NotFound,(await service.SelectionAsync(Session('b',2),b.ClientId,Token)).Status);
        var page = await service.ListAsync(Session('a',1),false,null,1,1,Token);
        Assert.Equal(2L,page.Total); Assert.Equal("Beta",Assert.Single(page.Clients!).Profile.Name);
        var empty = await service.ListAsync(Session('a',1),false,null,1,99,Token);
        Assert.Equal(2L,empty.Total); Assert.Empty(empty.Clients!);
        Assert.Equal(id,Assert.Single((await service.ListAsync(Session('a',1),false,"TEXT",null,0,Token)).Clients!).Id);
        Assert.Equal(ClientStatus.Invalid,(await service.ListAsync(Session('a',1),false,null,201,0,Token)).Status);
        Assert.Equal(ClientStatus.Success,(await service.SaveAsync(Session('a',1),id,new("A","Alpha edited",Email:"audit@example.invalid"),Token)).Status);
        Assert.Equal("audit@example.invalid",Assert.Single((await service.GetAsync(Session('a',1),id,Token)).Clients!).Profile.Email);

        // A mandatory audit failure cannot leave client/home rows or selection writes behind.
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_log_unavailable");
        try
        {
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveAsync(Session('a',1),null,new("ROLLBACK","No persistence"),Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.SelectionAsync(Session('a',1),id,Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.SetActiveAsync(Session('a',1),id,false,Token));
        }
        finally { await Sql("ALTER TABLE public.audit_log_unavailable RENAME TO audit_log"); }
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.clients WHERE client_code='ROLLBACK'"));
        Assert.Equal(1L,await Count($"SELECT count(*) FROM public.clients WHERE id={id} AND is_active IS TRUE"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.sessions WHERE user_id=1 AND selected_client_id IS NOT NULL"));
        Assert.Equal(1L,await Count($"SELECT count(*) FROM public.sessions WHERE user_id=2 AND selected_client_id={id}"));
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=2");
        Assert.Empty((await service.SelectionAsync(Session('b',2),null,Token)).Clients!);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.sessions WHERE user_id=2 AND selected_client_id IS NOT NULL"));
        Assert.Equal(ClientStatus.Success,(await service.SelectionAsync(Session('a',1),id,Token)).Status);
        Assert.Equal(ClientStatus.Success,(await service.SetActiveAsync(Session('a',1),id,false,Token)).Status);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.sessions WHERE selected_client_id IS NOT NULL"));
        Assert.Equal(ClientStatus.NotFound,(await service.SelectionAsync(Session('a',1),id,Token)).Status);
        Assert.Equal(ClientStatus.Success,(await service.SetActiveAsync(Session('a',1),id,true,Token)).Status);
        Assert.Equal(4L,await Count("SELECT count(*) FROM public.clients")); // Soft deletion preserves records.
        // The legacy global code constraint also resolves simultaneous requests
        // from different tenants without a partial home association.
        var parallel = await Task.WhenAll(
            service.SaveAsync(Session('a',1),null,new("RACE","Race A"),Token),
            service.SaveAsync(Session('c',3),null,new("RACE","Race B"),Token));
        Assert.Single(parallel,r=>r.Status==ClientStatus.Success);
        Assert.Single(parallel,r=>r.Status==ClientStatus.Conflict);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_tenant_client_homes h JOIN public.clients c ON c.id=h.client_id WHERE c.client_code='RACE'"));
        // Remove the synthetic race record from visibility while retaining it.
        var winner=parallel[0].Status==ClientStatus.Success ? Session('a',1) : Session('c',3);
        Assert.Equal(ClientStatus.Success,(await service.SetActiveAsync(winner,parallel.Single(r=>r.Status==ClientStatus.Success).ClientId!.Value,false,Token)).Status);

        await using (var disabled = new WebApplicationFactory<Program>())
        using (var client = disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync("/api/clients",Token)).StatusCode);
        await using var app = new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>
        {
            builder.UseEnvironment("Testing");
            builder.ConfigureServices(services=>
            {
                services.PostConfigure<PostgresOptions>(o=> {o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
                services.PostConfigure<AuthenticationOptions>(o=> {o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5080";});
                services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
            });
        });
        using var http = app.CreateClient(new WebApplicationFactoryClientOptions {HandleCookies=false});
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/clients",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+new string('a',64));
        http.DefaultRequestHeaders.Add("X-Tenant-Id","102");
        using var listed = await http.GetAsync("/api/clients?limit=1",Token);
        Assert.Equal(HttpStatusCode.OK,listed.StatusCode);
        Assert.Equal("2",Assert.Single(listed.Headers.GetValues("X-Total-Count")));
        using (var json=JsonDocument.Parse(await listed.Content.ReadAsStringAsync(Token)))
        {
            Assert.Equal("A",json.RootElement[0].GetProperty("client_code").GetString());
            Assert.True(json.RootElement[0].TryGetProperty("contact_person",out _));
        }
        using var unpaged=await http.GetAsync("/api/clients",Token);
        Assert.False(unpaged.Headers.Contains("X-Total-Count"));
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.GetAsync("/api/clients?limit=201",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PutAsJsonAsync("/api/auth/select-client",new {client_id=id},Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin","http://127.0.0.1:5080");
        Assert.Equal(HttpStatusCode.OK,(await http.PutAsJsonAsync("/api/auth/select-client",new {client_id=id},Token)).StatusCode);
        using var current=await http.GetAsync("/api/auth/current-client",Token);
        using (var json=JsonDocument.Parse(await current.Content.ReadAsStringAsync(Token)))
            Assert.Equal(id,json.RootElement.GetProperty("client_id").GetInt32());
        Assert.Equal(HttpStatusCode.OK,(await http.PostAsJsonAsync("/api/clients",new {client_code="HTTP",client_name="HTTP client"},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.PutAsJsonAsync($"/api/clients/{id}",new {client_code="A",client_name="Alpha API",contact_person="Contact",is_active=true},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.DeleteAsync($"/api/clients/{id}",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.PostAsync($"/api/clients/{id}/restore",null,Token)).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound,(await http.GetAsync($"/api/clients/{other.ClientId}",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound,(await http.PostAsJsonAsync($"/api/clients/{id}/data-purge",new {},Token)).StatusCode);
        await Sql("UPDATE public.users SET is_active=FALSE WHERE id=1");
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/clients",Token)).StatusCode);
        await Sql("UPDATE public.users SET is_active=TRUE WHERE id=1");
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE user_id=1");
        Assert.Equal(HttpStatusCode.Forbidden,(await http.GetAsync("/api/clients",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden,(await http.GetAsync("/api/auth/current-client",Token)).StatusCode);
    }
    private static VerifiedSession Session(char value,int id) => new(new string(value,64),id,"untrusted","admin",DateTime.UtcNow.AddHours(1),null,null,null);
}
