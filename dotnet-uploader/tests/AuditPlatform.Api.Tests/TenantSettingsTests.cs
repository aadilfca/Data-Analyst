using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Settings;
using AuditPlatform.Api.Tenancy;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TenantSettingsTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    private sealed class FixedClock : TimeProvider
    {
        public override DateTimeOffset GetUtcNow() => new(2026,3,31,12,0,0,TimeSpan.Zero);
        public override TimeZoneInfo LocalTimeZone => TimeZoneInfo.Utc;
    }
    [Fact]
    public async Task Tenant_settings_preserve_legacy_values_and_enforce_permissions_atomicity_and_browser_scope()
    {
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection=await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string value)
        { await using var command=new NpgsqlCommand(value,connection); await command.ExecuteNonQueryAsync(Token); }
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_name TEXT,client_code TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),
                expiry TIMESTAMPTZ,selected_client_id INTEGER REFERENCES public.clients(id));
            CREATE TABLE public.app_settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE public.holidays(id INTEGER PRIMARY KEY,label TEXT);
            INSERT INTO public.app_settings VALUES('materiality_threshold','LEGACY_UNCHANGED');
            INSERT INTO public.holidays VALUES(77,'LEGACY_HOLIDAY');
            INSERT INTO public.users VALUES (1,'settings-user','pending',TRUE,NULL,NULL),(2,'other-admin','pending',TRUE,'admin',NULL);
            INSERT INTO public.sessions VALUES (repeat('a',64),1,now()+interval '1 hour',NULL),
                (repeat('b',64),2,now()+interval '1 hour',NULL),(repeat('c',64),1,now()+interval '1 hour',NULL),
                (repeat('e',64),1,now()-interval '1 hour',NULL);
            """);
        await using (var password=new NpgsqlCommand("UPDATE public.users SET password_hash=$1 WHERE id=1",connection))
        { password.Parameters.AddWithValue(BCrypt.Net.BCrypt.HashPassword("p11-synthetic")); await password.ExecuteNonQueryAsync(Token); }
        await fixture.ApplyTenantFoundationAsync(Token);
        await fixture.ApplyEngagementFoundationAsync(Token);
        await fixture.ApplyTenantUserRolesAsync(Token);
        var root=FindRoot();
        await Sql(await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0006_session_tenant_context.sql"),Token));
        await using (var rollback=await connection.BeginTransactionAsync(Token))
        {
            await using var command=new NpgsqlCommand(await PostgresFixture.ReadTenantSettingsAsync(Token),connection,rollback);
            await command.ExecuteNonQueryAsync(Token); await rollback.RollbackAsync(Token);
        }
        await using (var check=new NpgsqlCommand("SELECT to_regclass('public.platform_tenant_settings') IS NULL",connection))
            Assert.True((bool)(await check.ExecuteScalarAsync(Token))!);
        await fixture.ApplyTenantSettingsAsync(Token);
        Assert.Equal(PostgresErrorCodes.DuplicateTable,(await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyTenantSettingsAsync(Token))).SqlState);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES
                (101,'Firm A','audit_firm'),(102,'Business B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES
                (101,1,'admin'),(102,1,'member'),(102,2,'admin');
            UPDATE public.sessions SET selected_tenant_id=CASE WHEN token IN(repeat('a',64),repeat('e',64)) THEN 101 ELSE 102 END;
            INSERT INTO public.platform_tenant_settings VALUES(101,'unknown_secret','SYNTHETIC_PRIVATE',now());
            """);
        using var pool=fixture.CreatePool();
        var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var limits=Options.Create(new LogPersistenceOptions());
        var audit=new PostgresAuditWriter(new LogSanitizer(limits),limits);
        var store=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions {Enabled=true}));
        var service=new TenantSettingsService(tx,audit,new FixedClock());
        var admin=(await store.FindAsync(new string('a',64),Token))!;
        var other=(await store.FindAsync(new string('b',64),Token))!;
        var member=(await store.FindAsync(new string('c',64),Token))!;
        Dictionary<string,string> Values(SettingsResult result) => Assert.IsType<Dictionary<string,string>>(result.Value);
        Assert.Equal("50000",Values(await service.ReadAsync(admin,null,Token))["materiality_threshold"]);
        Assert.DoesNotContain("unknown_secret",Values(await service.ReadAsync(admin,null,Token)).Keys);
        Assert.Equal(SettingsStatus.NotFound,(await service.ReadAsync(admin,"unknown_secret",Token)).Status);
        Assert.Equal(SettingsStatus.Success,(await service.ReadAsync(member,null,Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.SaveAsync(member,"date_format","MM/DD/YYYY",Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.BackupsAsync(member,null,Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.SaveHolidayAsync(member,null,"2026-01-01","Denied",false,Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.CashflowsAsync(member,new(7,"ytd",null,null),Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.ReadAsync(admin with {UserId=2},null,Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.ReadAsync(admin with {Token=new string('e',64)},null,Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.SaveAsync(admin,"cashflow_custom_start","bypass",Token)).Status);
        Assert.Equal(SettingsStatus.Forbidden,(await service.SaveAsync(admin,"upload_allow_without_backup","true",Token)).Status);
        Assert.Equal(SettingsStatus.Success,(await service.SaveAsync(admin,"materiality_threshold","123456789012345678901234567890.123456789",Token)).Status);
        Assert.Equal("50000",Values(await service.ReadAsync(other,null,Token))["materiality_threshold"]);
        var holiday=await service.SaveHolidayAsync(admin,null,"2026-02-28","  Holiday A  ",true,Token);
        var holidayJson=JsonSerializer.SerializeToElement(holiday.Value);
        int holidayId=holidayJson.GetProperty("id").GetInt32();
        Assert.Equal("Holiday A",holidayJson.GetProperty("label").GetString());
        Assert.Equal(SettingsStatus.NotFound,(await service.DeleteHolidayAsync(other,holidayId,Token)).Status);
        Assert.Equal(SettingsStatus.NotFound,(await service.SaveHolidayAsync(other,holidayId,"2026-02-28","Other",false,Token)).Status);
        Assert.Equal(SettingsStatus.Invalid,(await service.SaveHolidayAsync(admin,null,"2026-02-30","Bad",false,Token)).Status);
        Assert.Equal(SettingsStatus.Invalid,(await service.SaveHolidayAsync(admin,null,"2026-02-28","  ",false,Token)).Status);
        Assert.Equal(SettingsStatus.Success,(await service.SaveHolidayAsync(admin,holidayId,"2026-03-23","Updated",false,Token)).Status);
        Assert.Equal(SettingsStatus.Invalid,(await service.CashflowsAsync(admin,new(0,"ytd",null,null),Token)).Status);
        Assert.Equal(SettingsStatus.Invalid,(await service.CashflowsAsync(admin,new(1,"custom","2026-04-02","2026-04-01"),Token)).Status);
        Assert.Equal(SettingsStatus.Success,(await service.CashflowsAsync(admin,new(7,"custom","2025-07-01","2026-03-31"),Token)).Status);
        Assert.Equal(SettingsStatus.Success,(await service.BackupsAsync(admin,true,Token)).Status);
        Assert.DoesNotContain("upload_allow_without_backup",Values(await service.ReadAsync(admin,null,Token)).Keys);
        Assert.Equal(SettingsStatus.NotFound,(await service.ReadAsync(admin,"upload_allow_without_backup",Token)).Status);
        var before=Values(await service.ReadAsync(admin,null,Token));

        await Sql("ALTER TABLE public.audit_log RENAME TO audit_unavailable");
        try
        {
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveAsync(admin,"date_format","MM/DD/YYYY",Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.CashflowsAsync(admin,new(4,"ytd",null,null),Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.BackupsAsync(admin,false,Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveHolidayAsync(admin,null,"2026-04-01","ROLLBACK",false,Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveHolidayAsync(admin,holidayId,"2026-04-01","ROLLBACK",false,Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.DeleteHolidayAsync(admin,holidayId,Token));
        }
        finally { await Sql("ALTER TABLE public.audit_unavailable RENAME TO audit_log"); }
        Assert.Equal(before,Values(await service.ReadAsync(admin,null,Token)));
        Assert.True(JsonSerializer.SerializeToElement((await service.BackupsAsync(admin,null,Token)).Value).GetProperty("allow_without_backup").GetBoolean());
        Assert.Equal("Updated",JsonSerializer.SerializeToElement((await service.HolidaysAsync(admin,Token)).Value)[0].GetProperty("label").GetString());
        Assert.Equal(SettingsStatus.Success,(await service.DeleteHolidayAsync(admin,holidayId,Token)).Status);
        var tenantSessions=new TenantSessionService(tx,audit);
        Assert.True(await tenantSessions.SelectAsync(admin,102,TenantSessionService.ContextKey(admin),Token));
        Assert.Equal(SettingsStatus.Forbidden,(await service.SaveAsync(admin,"date_format","STALE",Token)).Status);
        await Sql("UPDATE public.sessions SET selected_tenant_id=101,context_version=context_version+1 WHERE token=repeat('a',64)");
        admin=(await store.FindAsync(admin.Token,Token))!;
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE tenant_id=101 AND user_id=1");
        Assert.Equal(SettingsStatus.Forbidden,(await service.ReadAsync(admin,null,Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE tenant_id=101 AND user_id=1");
        await Sql("UPDATE public.platform_tenants SET is_active=FALSE WHERE id=101");
        Assert.Equal(SettingsStatus.Forbidden,(await service.ReadAsync(admin,null,Token)).Status);
        await Sql("UPDATE public.platform_tenants SET is_active=TRUE WHERE id=101");
        var parallel=await Task.WhenAll(service.SaveAsync(admin,"date_format","DD/MMM/YYYY",Token),service.SaveAsync(other,"date_format","MM/DD/YYYY",Token));
        Assert.All(parallel,result=>Assert.Equal(SettingsStatus.Success,result.Status));
        Assert.Equal("DD/MMM/YYYY",Values(await service.ReadAsync(admin,null,Token))["date_format"]);
        Assert.Equal("MM/DD/YYYY",Values(await service.ReadAsync(other,null,Token))["date_format"]);
        await using (var entry=new NpgsqlCommand("SELECT details FROM public.audit_log WHERE module='TenantSettings' AND action='Update_materiality_threshold'",connection))
        {
            var details=(string)(await entry.ExecuteScalarAsync(Token))!;
            Assert.Contains("tenant_id",details); Assert.DoesNotContain("123456789012345678901234567890",details);
        }
        await using (var legacy=new NpgsqlCommand("SELECT value,(SELECT label FROM public.holidays WHERE id=77) FROM public.app_settings WHERE key='materiality_threshold'",connection))
        {
            await using var reader=await legacy.ExecuteReaderAsync(Token); await reader.ReadAsync(Token);
            Assert.Equal("LEGACY_UNCHANGED",reader.GetString(0)); Assert.Equal("LEGACY_HOLIDAY",reader.GetString(1));
        }
        await using (var disabled=new WebApplicationFactory<Program>())
        using (var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync("/api/settings",Token)).StatusCode);

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
                services.PostConfigure<SettingsManagementOptions>(o=>o.Enabled=true);
                services.AddSingleton<TimeProvider>(new FixedClock());
            });
        });
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        var address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        using var http=new HttpClient(new HttpClientHandler {UseCookies=false,AllowAutoRedirect=false}) {BaseAddress=new Uri(address)};
        var origin=http.BaseAddress.GetLeftPart(UriPartial.Authority);
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=origin;
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/settings",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/API/SETTINGS/",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(admin));
        http.DefaultRequestHeaders.Add("X-Tenant-Id","102");
        using var read=await http.GetAsync("/api/settings",Token);
        Assert.Equal(HttpStatusCode.OK,read.StatusCode);
        Assert.DoesNotContain("SYNTHETIC_PRIVATE",await read.Content.ReadAsStringAsync(Token));
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PostAsJsonAsync("/api/settings/date_format",new {value="DD/MM/YYYY"},Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin",origin);
        Assert.Equal(HttpStatusCode.OK,(await http.PostAsJsonAsync("/api/settings/date_format",new {value="DD/MM/YYYY"},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.PostAsJsonAsync("/api/settings/upload-backups",new {allow_without_backup="true"},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.PostAsJsonAsync("/api/settings/date_format",new {},Token)).StatusCode);
        http.DefaultRequestHeaders.Remove("Cookie"); http.DefaultRequestHeaders.Add("Cookie","session_token="+member.Token);
        http.DefaultRequestHeaders.Remove("X-App-Context"); http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(member));
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync("/api/settings/holidays",Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PostAsJsonAsync("/api/settings/date_format",new {value="DD/MM/YYYY"},Token)).StatusCode);

        var start=new ProcessStartInfo(Path.Combine(root,"..","venv","Scripts","python.exe"))
        {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root,"tests","p11_browser.py")); start.ArgumentList.Add(origin);
        using var process=Process.Start(start)!;
        var output=process.StandardOutput.ReadToEndAsync(Token); var errors=process.StandardError.ReadToEndAsync(Token);
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(Token); timeout.CancelAfter(TimeSpan.FromSeconds(90));
        try { await process.WaitForExitAsync(timeout.Token); } catch { process.Kill(entireProcessTree:true); throw; }
        Assert.True(process.ExitCode==0,await output+await errors);
    }
    private static string FindRoot()
    {
        var directory=new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null && !File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx"))) directory=directory.Parent;
        return directory?.FullName ?? throw new InvalidOperationException("Solution root required");
    }
}
