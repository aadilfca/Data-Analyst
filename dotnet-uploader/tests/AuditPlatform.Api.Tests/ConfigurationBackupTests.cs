using AuditPlatform.Api.Columns;
using AuditPlatform.Api.ConfigurationBackup;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.Settings;
using Microsoft.Extensions.Options;
using Npgsql;
using System.Net;
using System.Net.Http.Json;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ConfigurationBackupTests
{
    [Fact]
    public void ValidationRejectsForeignTenantAndBrokenColumnBindings()
    {
        var column = new TenantColumn(1, "Amount", "col_1", "Numeric", "General", null, true, "native", 1);
        var templateColumn = new TemplateColumn(1, "Amount", "col_1", "Numeric", "General", null, true, false, 0, 1, true);
        var template = new TemplateDefinition(2, "Sales", "Admin", DateTime.UnixEpoch, false, false,
            "standard", [], null, "standard", "native", 1, [templateColumn]);
        var backup = new ConfigurationBackupDocument(1, 3, new Dictionary<string,string>(), [column], [template], [], []);

        Assert.True(ConfigurationBackupValidation.IsValid(backup, 3));
        Assert.False(ConfigurationBackupValidation.IsValid(backup, 4));
        Assert.False(ConfigurationBackupValidation.IsValid(backup with {
            Columns = [column with { StorageKey = "different" }]
        }, 3));
        Assert.False(ConfigurationBackupValidation.IsValid(backup with { Columns = [column, column] }, 3));
    }
}

public sealed class ConfigurationBackupDatabaseTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    [Fact]
    public async Task RestoreIsTenantScopedRevisionBoundAndRollsBackOnLateFailure()
    {
        var ct=TestContext.Current.CancellationToken;
        await fixture.ApplyAuditSchemaAsync(ct);
        await using var connection=await fixture.OpenControlConnectionAsync(ct);
        async Task Sql(string sql) { await using var command=new NpgsqlCommand(sql,connection); await command.ExecuteNonQueryAsync(ct); }
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT NOT NULL,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,industry TEXT,contact_person TEXT,email TEXT,phone TEXT,is_active BOOLEAN DEFAULT TRUE,created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER REFERENCES public.clients(id));
            CREATE TABLE public.master_columns(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,column_name TEXT UNIQUE NOT NULL,data_type TEXT NOT NULL,category TEXT DEFAULT 'General',is_active BOOLEAN DEFAULT TRUE,notes TEXT);
            CREATE TABLE public.templates(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_name TEXT UNIQUE NOT NULL,created_by TEXT DEFAULT 'Admin',created_date TIMESTAMPTZ DEFAULT now(),is_active BOOLEAN DEFAULT TRUE,is_system BOOLEAN DEFAULT FALSE,has_subtotals_by_default BOOLEAN DEFAULT FALSE,rate_source TEXT,data_purpose TEXT NOT NULL DEFAULT 'standard',ledger_columns TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE public.template_column_mappings(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id),sort_order INTEGER NOT NULL DEFAULT 0,reconciliation_role TEXT,compare_for_changes BOOLEAN,comparison_review_required BOOLEAN NOT NULL DEFAULT FALSE);
            CREATE TABLE public.audit_rule_documents(id INTEGER PRIMARY KEY,template_id INTEGER);
            CREATE TABLE public.audit_rule_document_fields(id INTEGER PRIMARY KEY,document_id INTEGER,master_column_id INTEGER,field_name TEXT);
            CREATE TABLE public.audit_rule_cross_checks(id INTEGER PRIMARY KEY,compare_field TEXT);
            CREATE TABLE public.saved_column_mappings(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,template_column_id INTEGER,excel_column TEXT,compare_for_changes BOOLEAN);
            CREATE TABLE public.uploaded_files(id INTEGER PRIMARY KEY,template_id INTEGER,status TEXT,ledger_account_code TEXT);
            CREATE TABLE public.upload_validation_plans(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,status TEXT);
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER;
            CREATE TABLE public.client_data_history(id INTEGER PRIMARY KEY,template_id INTEGER,row_data JSONB);
            INSERT INTO public.users VALUES(1,'admin-a','unused',TRUE,'admin',NULL),(2,'admin-b','unused',TRUE,'admin',NULL);
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(20,'B','Beta');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10),(repeat('b',64),2,now()+interval '1 hour',20);
            """);
        await fixture.ApplyTenantFoundationAsync(ct); await fixture.ApplyEngagementFoundationAsync(ct); await fixture.ApplyTenantUserRolesAsync(ct);
        await Sql("ALTER TABLE public.sessions ADD COLUMN selected_tenant_id INTEGER REFERENCES public.platform_tenants(id), ADD COLUMN context_version BIGINT NOT NULL DEFAULT 0");
        await fixture.ApplyTenantSettingsAsync(ct); await fixture.ApplyTenantColumnsAsync(ct); await fixture.ApplyTenantTemplatesAsync(ct); await fixture.ApplySavedMappingsAsync(ct);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'Firm A','audit_firm'),(102,'Firm B','audit_firm');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            UPDATE public.sessions SET selected_tenant_id=CASE user_id WHEN 1 THEN 101 ELSE 102 END;
            """);
        using var pool=fixture.CreatePool(); var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var limits=Options.Create(new LogPersistenceOptions()); var audit=new PostgresAuditWriter(new LogSanitizer(limits),limits);
        var templatesOption=Options.Create(new TemplateManagementOptions {Enabled=true}); var mappingsOption=Options.Create(new SavedMappingOptions {Enabled=true});
        var columns=new TenantColumnService(tx,audit,templatesOption,mappingsOption);
        var templates=new TenantTemplateService(tx,audit,mappingsOption); var settings=new TenantSettingsService(tx,audit);
        var mappings=new TenantMappingService(tx,audit); var backup=new ConfigurationBackupService(tx,columns,templates,settings,audit);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions {Enabled=true}));
        var admin=(await sessions.FindAsync(new string('a',64),ct))!; var other=(await sessions.FindAsync(new string('b',64),ct))!;
        Assert.NotNull(admin); Assert.NotNull(other);
        var columnId=(await columns.SaveAsync(admin,null,new ColumnInput("Original","Text"),ct)).ColumnId!.Value;
        var templateId=(await templates.SaveAsync(admin,null,new TemplateInput("Template",[columnId]),ct)).Id!.Value;
        var snapshot=(await mappings.ReadAsync(admin,10,templateId,ct)).Snapshot!;
        Assert.Equal(MappingStatus.Success,(await mappings.SaveAsync(admin,10,templateId,[new MappingEntry(columnId,"ERP")],snapshot.Revision,snapshot.ConfigurationKey,ct)).Status);
        Assert.Equal(SettingsStatus.Success,(await settings.SaveAsync(admin,"date_format","DD/MM/YYYY",ct)).Status);
        Assert.Equal(SettingsStatus.Success,(await settings.SaveHolidayAsync(admin,null,"2026-01-01","Holiday",false,ct)).Status);
        var storageKey=Assert.Single((await columns.ListAsync(admin,true,ct)).Columns!).StorageKey;
        foreach(var sql in new[] {"INSERT INTO public.client_data(client_id,template_id,row_data) VALUES(10,$1,jsonb_build_object($2,'12.340'::text))",
            "INSERT INTO public.client_data_history VALUES(1,$1,jsonb_build_object($2,'11.220'::text))"})
        { await using var source=new NpgsqlCommand(sql,connection); source.Parameters.AddWithValue(templateId); source.Parameters.AddWithValue(storageKey); await source.ExecuteNonQueryAsync(ct); }
        async Task<string> Fingerprint()
        { await using var command=new NpgsqlCommand("SELECT (SELECT row_data::text FROM public.client_data LIMIT 1)||(SELECT row_data::text FROM public.client_data_history LIMIT 1)",connection); return (string)(await command.ExecuteScalarAsync(ct))!; }
        var fingerprint=await Fingerprint();
        var document=(await backup.ExportAsync(admin,ct))!;
        await Sql($"UPDATE public.templates SET is_active=FALSE WHERE id={templateId}");
        await Assert.ThrowsAsync<BackupReviewException>(()=>backup.ExportAsync(admin,ct));
        await Sql($"UPDATE public.templates SET is_active=TRUE WHERE id={templateId}");
        await Sql("UPDATE public.clients SET is_active=FALSE WHERE id=10");
        await Assert.ThrowsAsync<BackupReviewException>(()=>backup.ExportAsync(admin,ct));
        await Sql("UPDATE public.clients SET is_active=TRUE WHERE id=10");
        Assert.Equal("invalid",(await backup.PreviewAsync(other,document,ct)).Status);
        var altered=document with {Mappings=[document.Mappings[0] with {BindingKey=new string('A',64)}]};
        Assert.Equal("review",(await backup.PreviewAsync(admin,altered,ct)).Status);
        var invalidSettings=document with {Settings=new Dictionary<string,string> { ["unknown"]="value" }};
        Assert.Equal("invalid",(await backup.PreviewAsync(admin,invalidSettings,ct)).Status);
        Assert.Null(await backup.ExportAsync(admin with {UserId=2},ct));
        Assert.Equal(ColumnStatus.Success,(await columns.SaveAsync(admin,columnId,new ColumnInput("Changed","Text"),ct)).Status);
        Assert.Equal(SettingsStatus.Success,(await settings.SaveAsync(admin,"date_format","YYYY-MM-DD",ct)).Status);
        var holiday=Assert.Single(document.Holidays);
        Assert.Equal(SettingsStatus.Success,(await settings.SaveHolidayAsync(admin,holiday.Id,"2026-02-01","Changed holiday",false,ct)).Status);
        var preview=await backup.PreviewAsync(admin,document,ct); Assert.Equal("success",preview.Status);
        Assert.Equal("conflict",(await backup.RestoreAsync(admin,document,"stale",true,ct)).Status);
        var badTemplate=document.Templates[0] with {Columns=[document.Templates[0].Columns[0] with {Role="invalid"}]};
        var invalid=document with {Templates=[badTemplate]};
        Assert.Equal("invalid",(await backup.PreviewAsync(admin,invalid,ct)).Status);
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_unavailable");
        try { await Assert.ThrowsAsync<PostgresException>(()=>backup.RestoreAsync(admin,document,preview.Revision,true,ct)); }
        finally { await Sql("ALTER TABLE public.audit_unavailable RENAME TO audit_log"); }
        await Sql("CREATE FUNCTION public.fail_backup_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.module='ConfigurationBackup' THEN RAISE EXCEPTION 'synthetic audit failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER fail_backup_audit BEFORE INSERT ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.fail_backup_audit()");
        try { await Assert.ThrowsAsync<PostgresException>(()=>backup.RestoreAsync(admin,document,preview.Revision,true,ct)); }
        finally { await Sql("DROP TRIGGER fail_backup_audit ON public.audit_log; DROP FUNCTION public.fail_backup_audit()"); }
        Assert.Equal("Changed",Assert.Single((await columns.ListAsync(admin,true,ct)).Columns!).Name);
        Assert.Contains("YYYY-MM-DD",System.Text.Json.JsonSerializer.Serialize((await settings.ReadAsync(admin,"date_format",ct)).Value));
        Assert.Equal(fingerprint,await Fingerprint());
        Assert.Equal("success",(await backup.RestoreAsync(admin,document,preview.Revision,true,ct)).Status);
        Assert.Equal("Original",Assert.Single((await columns.ListAsync(admin,true,ct)).Columns!).Name);
        Assert.Equal("ERP",Assert.Single((await mappings.ReadAsync(admin,10,templateId,ct)).Snapshot!.Entries).Header);
        Assert.Contains("Holiday",System.Text.Json.JsonSerializer.Serialize((await settings.HolidaysAsync(admin,ct)).Value));
        Assert.Equal(fingerprint,await Fingerprint());
        Assert.Equal("conflict",(await backup.RestoreAsync(admin,document,preview.Revision,true,ct)).Status);

        await using(var disabled=new WebApplicationFactory<Program>()) using(var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync("/api/backup/export",ct)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>
        {
            builder.UseEnvironment("Testing"); builder.ConfigureServices(services=>
            {
                services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
                services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5080";});
                services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true); services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true); services.PostConfigure<SettingsManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true); services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true); services.PostConfigure<ConfigurationBackupOptions>(o=>o.Enabled=true);
            });
        });
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        var address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        using var http=new HttpClient(new HttpClientHandler {UseCookies=false,AllowAutoRedirect=false}) {BaseAddress=new Uri(address)};
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/backup/export",ct)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/api/backup/export",ct)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(admin));
        var wire=await http.GetFromJsonAsync<ConfigurationBackupDocument>("/api/backup/export",ct); Assert.NotNull(wire);
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PostAsJsonAsync("/api/backup/preview",wire,ct)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin",address);
        var response=await http.PostAsJsonAsync("/api/backup/preview",wire,ct); Assert.Equal(HttpStatusCode.OK,response.StatusCode);
        using var payload=System.Text.Json.JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));
        var wireRevision=payload.RootElement.GetProperty("revision").GetString();
        Assert.Equal(HttpStatusCode.BadRequest,(await http.PostAsJsonAsync("/api/backup/import",new {backup=wire,revision=wireRevision,confirmed=false},ct)).StatusCode);
        Assert.Equal(HttpStatusCode.OK,(await http.PostAsJsonAsync("/api/backup/import",new {backup=wire,revision=wireRevision,confirmed=true},ct)).StatusCode);
        Assert.Equal(HttpStatusCode.Conflict,(await http.PostAsJsonAsync("/api/backup/import",new {backup=wire,revision=wireRevision,confirmed=true},ct)).StatusCode);
        var concurrent=(await backup.ExportAsync(admin,ct))!;
        var concurrencyPreview=await backup.PreviewAsync(admin,concurrent,ct);
        var results=await Task.WhenAll(backup.RestoreAsync(admin,concurrent,concurrencyPreview.Revision,true,ct),
            backup.RestoreAsync(admin,concurrent,concurrencyPreview.Revision,true,ct));
        Assert.Equal(1,results.Count(r=>r.Status=="success")); Assert.Equal(1,results.Count(r=>r.Status=="conflict"));
        Assert.Equal(fingerprint,await Fingerprint());
        await using var redaction=new NpgsqlCommand("SELECT count(*) FROM public.audit_log WHERE details::text LIKE '%ERP%'",connection);
        Assert.Equal(0L,(long)(await redaction.ExecuteScalarAsync(ct))!);
        Assert.Equal(ColumnStatus.Success,(await columns.SaveAsync(admin,null,new ColumnInput("Other","Text"),ct)).Status);
        var collisionSource=(await backup.ExportAsync(admin,ct))!;
        var collision=collisionSource with {Columns=collisionSource.Columns.Select(c=>c.Id==columnId ? c with {Name="Other"} : c).ToArray()};
        var collisionPreview=await backup.PreviewAsync(admin,collision,ct); Assert.Equal("review",collisionPreview.Status);
        Assert.Contains((await columns.ListAsync(admin,true,ct)).Columns!,c=>c.Id==columnId && c.Name=="Original");
        Assert.Equal(fingerprint,await Fingerprint());
    }
}
