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

        var recoveryRequest = request with { RequestId = Guid.NewGuid(), Report = "income_statement" };
        var recoveryJob = await store.CreateAsync(actor, recoveryRequest, new string('d', 64), Ct);
        var firstLease = await store.ClaimAsync(Ct);
        Assert.Equal(recoveryJob.Job!.Id, firstLease!.Job.Id);
        Assert.Equal(1, firstLease.Job.Attempts);
        await SetLeaseExpired(connection, firstLease.Job.Id);
        // A new store represents the service process after restart; persisted work remains discoverable.
        var restartedStore = new FinancialReportJobStore(
            new PostgresTransactions(pool, Options.Create(new PostgresOptions())),
            Options.Create(new FinancialReportJobOptions()));
        Assert.Equal("preparing_inputs", (await restartedStore.GetAsync(actor, firstLease.Job.Id, Ct)).Job!.State);
        var reclaimed = await restartedStore.ClaimAsync(Ct);
        Assert.Equal(firstLease.Job.Id, reclaimed!.Job.Id);
        Assert.Equal(2, reclaimed.Job.Attempts);
        await restartedStore.CompleteAsync(reclaimed, [1, 2, 3, 4], "report.xlsx", Ct);
        await using (var expire = new NpgsqlCommand(
            "UPDATE public.financial_report_jobs SET expires_at=now()-interval '1 second' WHERE job_id=$1", connection))
        { expire.Parameters.AddWithValue(reclaimed.Job.Id); await expire.ExecuteNonQueryAsync(Ct); }
        await store.CleanupAsync(Ct);
        var expired = await store.GetAsync(actor, reclaimed.Job.Id, Ct);
        Assert.Equal("expired", expired.Job!.State);
        Assert.Null(await store.DownloadAsync(actor, reclaimed.Job.Id, Ct));

        var revokedRequest = request with { RequestId = Guid.NewGuid(), Report = "balance_sheet" };
        var revoked = await store.CreateAsync(actor, revokedRequest, new string('e', 64), Ct);
        var revokedLease = await store.ClaimAsync(Ct);
        Assert.Equal(revoked.Job!.Id, revokedLease!.Job.Id);
        await SetLeaseExpired(connection, revokedLease.Job.Id);
        await Sql(connection, "UPDATE public.sessions SET expiry=now()-interval '1 second' WHERE token=repeat('a',64)");
        Assert.Null(await store.ClaimAsync(Ct));
        Assert.Equal(("failed", "job_session_revoked"), await JobState(connection, revoked.Job.Id));

        await Sql(connection, "UPDATE public.sessions SET expiry=now()+interval '1 hour' WHERE token=repeat('a',64)");
        var concurrentA = await store.CreateAsync(actor,
            request with { RequestId = Guid.NewGuid(), Report = "balance_sheet" }, new string('f', 64), Ct);
        var concurrentB = await store.CreateAsync(actor,
            request with { RequestId = Guid.NewGuid(), Report = "income_statement" }, new string('1', 64), Ct);
        var claims = await Task.WhenAll(store.ClaimAsync(Ct), store.ClaimAsync(Ct));
        Assert.All(claims, lease => Assert.NotNull(lease));
        Assert.Equal(new[] { concurrentA.Job!.Id, concurrentB.Job!.Id }.Order(),
            claims.Select(lease => lease!.Job.Id).Order());
        foreach (var lease in claims) await store.FailAsync(lease!, "acceptance_cleanup", false, Ct);
    }

    private static async Task SetLeaseExpired(NpgsqlConnection connection, Guid jobId)
    {
        await using var command = new NpgsqlCommand(
            "UPDATE public.financial_report_jobs SET lease_until=now()-interval '1 second' WHERE job_id=$1", connection);
        command.Parameters.AddWithValue(jobId); await command.ExecuteNonQueryAsync(Ct);
    }

    private static async Task<(string State, string? Code)> JobState(NpgsqlConnection connection, Guid jobId)
    {
        await using var command = new NpgsqlCommand(
            "SELECT state,code FROM public.financial_report_jobs WHERE job_id=$1", connection);
        command.Parameters.AddWithValue(jobId);
        await using var reader = await command.ExecuteReaderAsync(Ct);
        Assert.True(await reader.ReadAsync(Ct));
        return (reader.GetString(0), reader.IsDBNull(1) ? null : reader.GetString(1));
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
