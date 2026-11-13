using System.Diagnostics;
using System.Net;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.DataIntegrity;
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

public sealed class DataIntegrityTests
{
    private static CancellationToken Ct => TestContext.Current.CancellationToken;

    [Fact]
    public void IndexDefinitionsRetainAllFourteenRequirementsAndRejectImpostors()
    {
        Assert.Equal(14, IntegrityBaseline.Requirements.Length);
        foreach (var required in IntegrityBaseline.Requirements)
        {
            var index = new IntegrityIndex(required.Table, "renamed", "btree", true, true, true,
                required.Kind != "lookup", required.Kind == "primary", true, true, required.Predicate, required.Columns);
            Assert.True(DatabaseSafeguardService.Matches(index, required));
            foreach (var invalid in new[] { index with { IsValid = false }, index with { IsReady = false }, index with { IsLive = false },
                index with { StandardKeys = false }, index with { AccessMethod = "hash" }, index with { KeyColumns = ["unrelated"] }, index with { Predicate = "client_id=10" } })
                Assert.False(DatabaseSafeguardService.Matches(invalid, required));
            Assert.Equal(required.Kind == "lookup", DatabaseSafeguardService.Matches(index with { KeyColumns = [.. required.Columns, "extra"] }, required));
            if (required.Kind != "lookup")
            {
                Assert.False(DatabaseSafeguardService.Matches(index with { IsUnique = false }, required));
                Assert.False(DatabaseSafeguardService.Matches(index with { IsImmediate = false }, required));
            }
            if (required.Kind == "primary") Assert.False(DatabaseSafeguardService.Matches(index with { IsPrimary = false }, required));
        }
        var unique = IntegrityBaseline.Requirements[1];
        var valid = new IntegrityIndex(unique.Table, "same-name", "btree", true, true, true, true, false, true, true,
            "(is_deleted = false) AND (change_detection_status <> 'deleted'::text) AND (source_identity IS NOT NULL) AND (source_identity <> ''::text)", unique.Columns);
        Assert.True(DatabaseSafeguardService.Matches(valid, unique));
        foreach (var predicate in new[] { IntegrityBaseline.ActivePredicate.Replace(" AND ", " OR "), IntegrityBaseline.ActivePredicate.Replace("'deleted'", "'DELETED'"),
            IntegrityBaseline.ActivePredicate + " AND client_id=10", "is_deleted = false" })
            Assert.False(DatabaseSafeguardService.Matches(valid with { Predicate = predicate }, unique));
    }

    [Fact]
    public async Task DisabledEndpointAndInvalidDependenciesFailClosed()
    {
        await using var app = new WebApplicationFactory<Program>();
        using var client = app.CreateClient();
        Assert.False(app.Services.GetRequiredService<IOptions<DataIntegrityOptions>>().Value.Enabled);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, (await client.GetAsync("/api/data-integrity/overview?client_id=10", Ct)).StatusCode);
        await using var invalid = new WebApplicationFactory<Program>().WithWebHostBuilder(b => b.ConfigureServices(s => s.PostConfigure<DataIntegrityOptions>(o => o.Enabled = true)));
        Assert.Throws<OptionsValidationException>(() => invalid.CreateClient());
    }

    [Fact]
    public async Task OwnedInventorySafeguardsIsolationRecoveryAndBrowser()
    {
        await using var fixture = new PostgresFixture(); await fixture.InitializeAsync(); await fixture.ApplyAuditSchemaAsync(Ct);
        await using var connection = await fixture.OpenControlConnectionAsync(Ct);
        async Task Sql(string sql) { await using var command = new NpgsqlCommand(sql, connection); await command.ExecuteNonQueryAsync(Ct); }
        // Accepted P31 authentication/client fixture, with only P32's source tables added.
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,is_active BOOLEAN DEFAULT TRUE,industry TEXT,contact_person TEXT,email TEXT,phone TEXT,created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER,selected_tenant_id INTEGER,context_version BIGINT NOT NULL DEFAULT 0);
            INSERT INTO public.users(id,username,password_hash,role) VALUES(1,'owner','unused','admin'),(2,'other','unused','admin'),(3,'auditor','unused','user'),(4,'unassigned','unused','user');
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha <safe>'),(20,'B','Foreign secret');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0),(repeat('d',64),4,now()+interval '1 hour',10,101,0);
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER,ADD COLUMN source_identity TEXT,ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE,
                ADD COLUMN change_detection_status TEXT DEFAULT 'unchanged',ADD COLUMN upload_id INTEGER,ADD COLUMN extract_date DATE,
                ADD COLUMN derived_transaction_date DATE,ADD COLUMN derived_treasury_type TEXT;
            CREATE TABLE public.uploaded_files(id INTEGER PRIMARY KEY,client_id INTEGER,file_path TEXT);
            CREATE TABLE public.trial_balance_uploads(id INTEGER PRIMARY KEY,client_id INTEGER,as_of_date DATE);
            CREATE TABLE public.trial_balance_accounts(id INTEGER PRIMARY KEY,upload_id INTEGER,account_code TEXT,parent_account_code TEXT);
            CREATE TABLE public.report_balance_inputs(id INTEGER PRIMARY KEY,client_id INTEGER,group_label TEXT,account_code TEXT,period_month TEXT);
            INSERT INTO public.uploaded_files VALUES(1,10,'Z:/untrusted/never-open.xlsx'),(2,20,'foreign-secret-path');
            INSERT INTO public.client_data(client_id,template_id,source_identity,upload_id,row_data) VALUES(10,1,'line-1',1,'{"doc":"repeat","memo":"اردو"}'),(10,1,'line-2',1,'{"doc":"repeat"}'),(20,1,'foreign',2,'{"secret":"foreign"}');
            INSERT INTO public.trial_balance_uploads VALUES(1,10,'2026-04-30'),(2,10,'2026-05-31'),(3,20,'2026-04-30');
            INSERT INTO public.trial_balance_accounts VALUES(1,1,'001',NULL),(2,2,'001',NULL),(3,3,'secret',NULL);
            INSERT INTO public.report_balance_inputs VALUES(1,10,'Assets','001','2026-04'),(2,20,'Assets','001','2026-04');
            """);
        await fixture.ApplyTenantFoundationAsync(Ct); await fixture.ApplyEngagementFoundationAsync(Ct); await fixture.ApplyTenantUserRolesAsync(Ct);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member'),(101,4,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            """);
        // Primary keys were declared above; build the other ten requirements only in this owned database.
        for (var n = 0; n < IntegrityBaseline.Requirements.Length; n++)
        {
            var r = IntegrityBaseline.Requirements[n]; if (r.Kind == "primary") continue;
            await Sql($"CREATE {(r.Kind == "unique" ? "UNIQUE " : "")}INDEX p32_{n} ON public.{r.Table} ({string.Join(',', r.Columns)}){(r.Predicate is null ? "" : " WHERE " + r.Predicate)}");
        }
        await using var app = Host(fixture); app.UseKestrel(server => server.Listen(IPAddress.Loopback, 0));
        var address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin = address;
        var store = app.Services.GetRequiredService<PostgresSessionStore>();
        var owner = (await store.FindAsync(new string('a', 64), Ct))!; var staff = (await store.FindAsync(new string('c', 64), Ct))!;
        var outsider = (await store.FindAsync(new string('d', 64), Ct))!; var foreign = (await store.FindAsync(new string('b', 64), Ct))!;
        var service = app.Services.GetRequiredService<DataIntegrityService>(); var bounds = app.Services.GetRequiredService<IOptions<DataIntegrityOptions>>().Value;
        var directory = Path.Combine(Root(), ".tools", "p32-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(directory);
        async Task<IntegrityOverview> Scan(string name)
        {
            var before = await Fingerprint(connection);
            var result = await service.ReadAsync(owner, 10, Ct); Assert.Equal("success", result.Status);
            Assert.Equal(before, await Fingerprint(connection));
            var value = Assert.IsType<IntegrityOverview>(result.Value);
            await File.WriteAllTextAsync(Path.Combine(directory, name + ".json"), JsonSerializer.Serialize(value, IntegrityJson.Options), Ct);
            return value;
        }
        var clean = await Scan("clean"); Assert.Equal("Passed", clean.Safeguards.Status); Assert.Equal(2, clean.Modules[0].RowCount);
        Assert.Equal(1, clean.Modules[0].UnmeasuredSourceFiles); Assert.Equal("Incomplete", clean.Modules[0].SizeStatus);
        Assert.Equal(0, clean.Modules[1].DuplicateGroups); Assert.NotNull(clean.DatabaseContext);
        Assert.Null((await service.ReadAsync(staff, 10, Ct)).Value!.DatabaseContext);
        Assert.Equal("forbidden", (await service.ReadAsync(outsider, 10, Ct)).Status);
        Assert.Equal("forbidden", (await service.ReadAsync(foreign, 10, Ct)).Status);
        Assert.Equal("forbidden", (await service.ReadAsync(owner, 20, Ct)).Status);
        Assert.Equal("invalid", (await service.ReadAsync(owner, 0, Ct)).Status);
        bounds.MaximumResponseBytes = 1;
        Assert.Equal("too_large", (await service.ReadAsync(owner, 10, Ct)).Status); bounds.MaximumResponseBytes = 131072;
        await Sql("UPDATE public.clients SET client_name=repeat('x',131073) WHERE id=10");
        Assert.Equal("too_large", (await service.ReadAsync(owner, 10, Ct)).Status);
        await Sql("UPDATE public.clients SET client_name='Alpha <safe>' WHERE id=10");
        await Sql("INSERT INTO public.clients(id,client_code,client_name) VALUES(30,'EMPTY','Empty'); INSERT INTO public.platform_tenant_client_homes VALUES(30,102,now()); UPDATE public.sessions SET selected_client_id=30 WHERE user_id=2");
        var empty = await service.ReadAsync(foreign, 30, Ct); Assert.Equal("success", empty.Status);
        Assert.All(empty.Value!.Modules, m => Assert.Equal(0, m.RowCount)); Assert.Equal("Passed", empty.Value.Safeguards.Status);
        await Sql("UPDATE public.sessions SET selected_client_id=20 WHERE user_id=2");
        using var http = new HttpClient { BaseAddress = new Uri(address) };
        Assert.Equal(HttpStatusCode.Unauthorized, (await http.GetAsync("/api/data-integrity/overview?client_id=10", Ct)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie", "session_token=" + new string('a', 64));
        Assert.Equal(HttpStatusCode.Conflict, (await http.GetAsync("/api/data-integrity/overview?client_id=10", Ct)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context", TenantSessionService.ContextKey(owner));
        var response = await http.GetAsync("/api/data-integrity/overview?client_id=10", Ct);
        Assert.Equal(HttpStatusCode.OK, response.StatusCode); Assert.True(response.Headers.CacheControl!.NoStore);
        Assert.DoesNotContain("foreign-secret", await response.Content.ReadAsStringAsync(Ct));
        Assert.Equal(HttpStatusCode.MethodNotAllowed, (await http.PostAsync("/api/data-integrity/overview?client_id=10", null, Ct)).StatusCode);

        // Real catalog comparisons: names and covering lookups are not identities.
        await Sql("ALTER INDEX public.p32_7 RENAME TO renamed_upload_lookup; CREATE INDEX covering_lookup ON public.trial_balance_uploads(client_id,as_of_date) INCLUDE(id); DROP INDEX public.renamed_upload_lookup;");
        Assert.Equal("Passed", (await Scan("covering")).Safeguards.Status);
        await Sql("DROP INDEX public.p32_1; CREATE UNIQUE INDEX impostor_unique ON public.client_data(client_id,template_id,source_identity,id) WHERE " + IntegrityBaseline.ActivePredicate);
        Assert.Equal("Needs attention", (await Scan("impostor")).Safeguards.Status);
        await Sql("DROP INDEX public.impostor_unique; CREATE UNIQUE INDEX p32_1 ON public.client_data(client_id,template_id,source_identity) WHERE " + IntegrityBaseline.ActivePredicate);
        bounds.MaximumCatalogEntries = 14; await Sql("CREATE INDEX excess_catalog ON public.client_data(upload_id)");
        var limited = await Scan("limited"); Assert.True(limited.Safeguards.Incomplete); Assert.Equal("Not checked", limited.Safeguards.Modules[0].Checks[0].Status);
        bounds.MaximumCatalogEntries = 1024;

        // Invalid controlled data: duplicate identity is distinct from repeated voucher details.
        await Sql("""
            DROP INDEX public.p32_1; DROP INDEX public.p32_9;
            INSERT INTO public.client_data(client_id,template_id,source_identity,upload_id,row_data) VALUES
                (10,1,'line-1',2,'{}'),(10,NULL,NULL,999,'{}'),(10,1,'ignored',NULL,'{}');
            UPDATE public.client_data SET is_deleted=TRUE WHERE source_identity='ignored';
            INSERT INTO public.trial_balance_accounts VALUES(4,1,'001',NULL),(5,1,' ',NULL);
            INSERT INTO public.report_balance_inputs VALUES(3,10,' assets ','001','2026-04'),(4,10,'Assets',' ','2026-04');
            """);
        var invalidData = await Scan("invalid");
        Assert.Equal("Needs attention", invalidData.Safeguards.Status); Assert.Equal(1, invalidData.Modules[0].DuplicateGroups);
        Assert.Equal(1, invalidData.Modules[0].UnidentifiedRows); Assert.Equal(2, invalidData.Modules[0].InvalidUploadLinks);
        Assert.Equal(1, invalidData.Modules[0].AssociatedRecordCount); Assert.Equal(1, invalidData.Modules[1].DuplicateGroups);
        Assert.Equal(1, invalidData.Modules[2].DuplicateGroups);

        // Expected schema gaps roll back only the affected optional query.
        await Sql("ALTER TABLE public.trial_balance_accounts RENAME TO hidden_accounts");
        var missing = await Scan("missing"); Assert.Null(missing.Modules[1].RowCount); Assert.Equal(1, missing.Modules[2].DuplicateGroups);
        Assert.Equal("Not checked", missing.Safeguards.Modules[1].Checks[1].Status);
        await Sql("ALTER TABLE public.hidden_accounts RENAME TO trial_balance_accounts");
        // Catalog denial does not erase independently available record evidence.
        await Sql("CREATE ROLE p32_reader LOGIN PASSWORD 'owned-test-only'; GRANT USAGE ON SCHEMA public TO p32_reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO p32_reader; REVOKE SELECT ON pg_catalog.pg_index FROM PUBLIC;");
        try
        {
            var pg = new NpgsqlConnectionStringBuilder(fixture.ConnectionString) { Username = "p32_reader", Password = "owned-test-only", Pooling = false };
            await using var pool = new PostgresDataSourceFactory(Options.Create(new PostgresOptions { Enabled = true, ConnectionString = pg.ConnectionString }));
            var restricted = new DataIntegrityService(new PostgresReadSnapshot(pool, Options.Create(new PostgresOptions())), new DatabaseSafeguardService(Options.Create(bounds)), Options.Create(bounds));
            var result = await restricted.ReadAsync(owner, 10, Ct); Assert.Equal("success", result.Status);
            Assert.Equal("Not checked", result.Value!.Safeguards.Modules[0].Checks[0].Status); Assert.Equal(1, result.Value.Modules[0].DuplicateGroups);
            await File.WriteAllTextAsync(Path.Combine(directory, "restricted.json"), JsonSerializer.Serialize(result.Value, IntegrityJson.Options), Ct);
        }
        finally { await Sql("GRANT SELECT ON pg_catalog.pg_index TO PUBLIC"); }

        // A second connection blocks an owned table; cancellation/deadline/busy and revocation must discard results.
        await using var blocker = await fixture.OpenControlConnectionAsync(Ct);
        async Task BeginLock() { await using var c = new NpgsqlCommand("BEGIN; LOCK TABLE public.client_data IN ACCESS EXCLUSIVE MODE", blocker); await c.ExecuteNonQueryAsync(Ct); }
        async Task EndLock() { await using var c = new NpgsqlCommand("ROLLBACK", blocker); await c.ExecuteNonQueryAsync(Ct); }
        await BeginLock();
        using (var cancelled = CancellationTokenSource.CreateLinkedTokenSource(Ct))
        {
            var waiting = service.ReadAsync(owner, 10, cancelled.Token); await Locked(connection);
            Assert.Equal("busy", (await service.ReadAsync(owner, 10, Ct)).Status); cancelled.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => waiting);
        }
        bounds.TimeoutSeconds = 1;
        Assert.Equal("timeout", (await service.ReadAsync(owner, 10, Ct)).Status); bounds.TimeoutSeconds = 60;
        await EndLock();
        await BeginLock(); var revoked = service.ReadAsync(staff, 10, Ct); await Locked(connection);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE user_id=3"); await EndLock();
        Assert.Equal("forbidden", (await revoked).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE WHERE user_id=3");
        Assert.Equal("success", (await service.ReadAsync(owner, 10, Ct)).Status);
        await BeginLock(); var consistent = service.ReadAsync(owner, 10, Ct); await Locked(connection);
        await using (var change = new NpgsqlCommand("""
            INSERT INTO public.client_data(client_id,template_id,source_identity,row_data) VALUES(10,3,'snapshot-new','{}');
            INSERT INTO public.report_balance_inputs VALUES(100,10,'New','NEW','2026-06'); COMMIT;
            """, blocker)) await change.ExecuteNonQueryAsync(Ct);
        var snapshot = await consistent; Assert.Equal("success", snapshot.Status);
        Assert.Equal(4, snapshot.Value!.Modules[0].RowCount); Assert.Equal(3, snapshot.Value.Modules[2].RowCount);
        var refreshed = await service.ReadAsync(owner, 10, Ct);
        Assert.Equal(5, refreshed.Value!.Modules[0].RowCount); Assert.Equal(4, refreshed.Value.Modules[2].RowCount);
        await Sql("DELETE FROM public.client_data WHERE source_identity='snapshot-new'; DELETE FROM public.report_balance_inputs WHERE id=100");

        // Large aggregate scan remains a fixed three-module response.
        await Sql("INSERT INTO public.client_data(client_id,template_id,source_identity,row_data) SELECT 10,2,'large-'||n,jsonb_build_object('n',n) FROM generate_series(1,10001) n");
        var watch = Stopwatch.StartNew(); var large = await Scan("large"); watch.Stop(); Assert.Equal(10005, large.Modules[0].RowCount);
        var pgFixture = new NpgsqlConnectionStringBuilder(fixture.ConnectionString);
        await File.WriteAllTextAsync(Path.Combine(directory, "fixture.json"), JsonSerializer.Serialize(new { address, port = fixture.Port, database = pgFixture.Database,
            user = pgFixture.Username, password = pgFixture.Password, context = TenantSessionService.ContextKey(owner), large_ms = watch.ElapsedMilliseconds }), Ct);
        await Python("p32_integrity_oracle.py", directory); await Python("p32_integrity_browser.py", directory);
        bounds.Enabled = false; await Python("p32_integrity_browser.py", directory, "disabled");
        Assert.Equal(HttpStatusCode.ServiceUnavailable, (await http.GetAsync("/api/data-integrity/overview?client_id=10", Ct)).StatusCode);
    }

    private static async Task<string> Fingerprint(NpgsqlConnection connection)
    {
        var values = new List<string>();
        foreach (var table in new[] { "client_data", "uploaded_files", "trial_balance_uploads", "trial_balance_accounts", "report_balance_inputs" })
        {
            // The intentional missing-table fixture uses the same source under its temporary name.
            var actual = table;
            await using (var exists = new NpgsqlCommand("SELECT to_regclass($1)::text", connection))
            { exists.Parameters.AddWithValue("public." + table); if (await exists.ExecuteScalarAsync(Ct) is DBNull) actual = "hidden_accounts"; }
            await using var command = new NpgsqlCommand($"SELECT md5(COALESCE(string_agg(t::text,E'\\n' ORDER BY id),'')) FROM public.{actual} t", connection);
            values.Add((string)(await command.ExecuteScalarAsync(Ct))!);
        }
        return string.Join('/', values);
    }
    private static async Task Locked(NpgsqlConnection connection)
    {
        var watch = Stopwatch.StartNew();
        while (watch.Elapsed < TimeSpan.FromSeconds(5))
        {
            await using var c = new NpgsqlCommand("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'", connection);
            if ((long)(await c.ExecuteScalarAsync(Ct))! > 0) return;
            await Task.Delay(20, Ct);
        }
        Assert.Fail("Owned integrity read did not reach its table lock.");
    }
    private static WebApplicationFactory<Program> Host(PostgresFixture f) => new WebApplicationFactory<Program>().WithWebHostBuilder(b => b.ConfigureServices(s =>
    {
        s.PostConfigure<PostgresOptions>(o => { o.Enabled = true; o.ConnectionString = f.ConnectionString; });
        s.PostConfigure<AuthenticationOptions>(o => { o.Enabled = true; o.PublicOrigin = "http://127.0.0.1:5001"; });
        s.PostConfigure<ApplicationShellOptions>(o => o.Enabled = true); s.PostConfigure<UserManagementOptions>(o => o.Enabled = true);
        s.PostConfigure<ClientManagementOptions>(o => o.Enabled = true); s.PostConfigure<DataIntegrityOptions>(o => o.Enabled = true);
    }));
    private static string Root()
    { DirectoryInfo? f = new(AppContext.BaseDirectory); while (f is not null && !File.Exists(Path.Combine(f.FullName, "AuditPlatform.slnx"))) f = f.Parent; return f!.FullName; }
    private static async Task Python(string script, string directory, string? mode = null)
    {
        var start = new ProcessStartInfo(Path.Combine(Root(), "..", "venv", "Scripts", "python.exe"))
        { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        start.ArgumentList.Add(Path.Combine(Root(), "tests", script)); start.ArgumentList.Add(directory); if (mode is not null) start.ArgumentList.Add(mode);
        using var p = Process.Start(start)!; var output = p.StandardOutput.ReadToEndAsync(); var errors = p.StandardError.ReadToEndAsync();
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(Ct); timeout.CancelAfter(TimeSpan.FromMinutes(4));
        try { await p.WaitForExitAsync(timeout.Token); } catch { p.Kill(entireProcessTree: true); throw; }
        Assert.True(p.ExitCode == 0, await output + await errors);
    }
}
