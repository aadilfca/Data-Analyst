using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Reports;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class FinancialReportJobTests
{
    private static CancellationToken Ct=>TestContext.Current.CancellationToken;

    [Fact]
    public async Task JobsAreIdempotentOwnerScopedAndCancellable()
    {
        await using var fixture=new PostgresFixture();await fixture.InitializeAsync();await fixture.ApplyAuditSchemaAsync(Ct);
        await using var connection=await fixture.OpenControlConnectionAsync(Ct);
        await Sql(connection,"""
            CREATE TABLE public.users(id integer PRIMARY KEY,username text NOT NULL,password_hash text NOT NULL,
                is_active boolean NOT NULL DEFAULT true,role text,last_login timestamptz);
            CREATE TABLE public.clients(id integer PRIMARY KEY,client_code text NOT NULL,client_name text NOT NULL,
                is_active boolean NOT NULL DEFAULT true,industry text,contact_person text,email text,phone text,
                created_at timestamptz NOT NULL DEFAULT now());
            CREATE TABLE public.sessions(token text PRIMARY KEY,user_id integer NOT NULL REFERENCES public.users(id),
                expiry timestamptz NOT NULL,selected_client_id integer REFERENCES public.clients(id),
                selected_tenant_id integer,context_version bigint NOT NULL DEFAULT 0);
            INSERT INTO public.users(id,username,password_hash,role) VALUES(1,'owner','unused','admin');
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0);
            """);
        await fixture.ApplyTenantFoundationAsync(Ct);await fixture.ApplyEngagementFoundationAsync(Ct);
        await fixture.ApplyTenantUserRolesAsync(Ct);
        await Sql(connection,"""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin');
            INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES(10,101);
            """);
        string root=FindRoot();
        await Sql(connection,await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0020_financial_report_jobs.sql"),Ct));

        using var pool=fixture.CreatePool();
        var store=new FinancialReportJobStore(new PostgresTransactions(pool,Options.Create(new PostgresOptions())),
            Options.Create(new FinancialReportJobOptions()));
        var actor=new VerifiedSession(new string('a',64),1,"owner","admin",DateTime.UtcNow.AddHours(1),
            10,"Alpha","A",true,101,0);
        var request=new FinancialReportJobRequest(Guid.NewGuid(),10,"balance_sheet",30);

        var created=await store.CreateAsync(actor,request,new string('b',64),Ct);
        var repeated=await store.CreateAsync(actor,request,new string('b',64),Ct);
        Assert.Equal("success",created.Status);
        Assert.Equal(created.Job!.Id,repeated.Job!.Id);
        Assert.Equal("conflict",(await store.CreateAsync(actor,request,new string('c',64),Ct)).Status);
        Assert.Equal("success",(await store.GetAsync(actor,created.Job.Id,Ct)).Status);
        Assert.Equal("cancelled",(await store.CancelAsync(actor,created.Job.Id,Ct)).Job!.State);
        Assert.Null(await store.DownloadAsync(actor,created.Job.Id,Ct));
        Assert.Equal("not_found",(await store.CancelAsync(actor,Guid.NewGuid(),Ct)).Status);
    }

    private static async Task Sql(NpgsqlConnection connection,string sql)
    {await using var command=new NpgsqlCommand(sql,connection);await command.ExecuteNonQueryAsync(Ct);}

    private static string FindRoot()
    {
        DirectoryInfo? directory=new(AppContext.BaseDirectory);
        while(directory is not null&&!File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx")))directory=directory.Parent;
        return directory?.FullName??throw new DirectoryNotFoundException("Could not find the .NET solution root.");
    }
}
