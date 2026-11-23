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
using System.Text.Json;
using AuditPlatform.Api.Tenancy;

namespace AuditPlatform.Api.Tests;

public sealed class UploadConfigurationAcceptanceTests
{
    [Fact]
    public async Task BindingsAndConfigurationAreGuardedAndBrowserAccessible()
    {
        await using var fixture=new PostgresFixture(); await fixture.InitializeAsync();
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
        using var pool=fixture.CreatePool(timeoutSeconds:10); var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions{CommandTimeoutSeconds=10}));
        var limits=Options.Create(new LogPersistenceOptions()); var audit=new PostgresAuditWriter(new LogSanitizer(limits),limits);
        var templatesOption=Options.Create(new TemplateManagementOptions {Enabled=true}); var mappingsOption=Options.Create(new SavedMappingOptions {Enabled=true});
        var columns=new TenantColumnService(tx,audit,templatesOption,mappingsOption);
        var templates=new TenantTemplateService(tx,audit,mappingsOption); var settings=new TenantSettingsService(tx,audit);
        var mappings=new TenantMappingService(tx,audit); var backup=new ConfigurationBackupService(tx,columns,templates,settings,audit);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions {Enabled=true}));
        var admin=(await sessions.FindAsync(new string('a',64),ct))!; var other=(await sessions.FindAsync(new string('b',64),ct))!;
        Assert.NotNull(admin); Assert.NotNull(other);

        await fixture.ApplyUploadDerivedFieldsAsync(ct);
        await Sql("""
            INSERT INTO public.users VALUES(3,'member','unused',TRUE,'user',NULL);
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,3,'member');
            INSERT INTO public.sessions(token,user_id,expiry,selected_client_id,selected_tenant_id,context_version) VALUES(repeat('c',64),3,now()+interval '1 hour',10,101,0);
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Synthetic engagement');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            INSERT INTO public.client_data(client_id,row_data) VALUES(20,'{"sentinel":"preserve"}');
            """);
        var ids=new Dictionary<string,int>();
        foreach(var (role,name,type) in new[]{("document","Document","Text"),("date","Date","Date"),("amount","Amount","Numeric"),("quantity","Quantity","Numeric"),
            ("rate","Rate","Numeric"),("account_code","Account Code","Text"),("account_name","Account Name","Text"),("debit","Debit","Numeric"),("credit","Credit","Numeric"),
            ("description","Description","Text"),("line_id","Line ID","Text"),("alternate","Alternate Amount","Numeric")})
            ids[role]=(await columns.SaveAsync(admin,null,new ColumnInput(name,type),ct)).ColumnId!.Value;
        var assignments=ids.Where(p=>p.Key is not ("document" or "date" or "alternate")).ToDictionary(p=>p.Key,p=>p.Value);
        var roles=new Dictionary<int,string?>{{ids["document"],"document_id"},{ids["date"],"document_date"},{ids["amount"],"footing"}};
        var save=await templates.SaveAsync(admin,null,new TemplateInput("Purchase Order",ids.Values.ToArray(),Roles:roles,
            Comparisons:ids.Values.ToDictionary(id=>id,_=>true),UploadBindings:assignments),ct);
        Assert.Equal(TemplateStatus.Success,save.Status);int templateId=save.Id!.Value;
        async Task<TemplateDefinition> Current() => (await templates.GetAsync(admin,templateId,ct)).Templates!.Single();
        TemplateInput Edit(TemplateDefinition t,Dictionary<string,int> bindings)=>new(t.Name,t.Columns.Select(c=>c.Id).ToArray(),t.Purpose,t.Ledger,
            t.Columns.ToDictionary(c=>c.Id,c=>c.Role),t.Columns.ToDictionary(c=>c.Id,c=>c.Compare??true),t.RateSource,t.Subtotals,t.ConfigurationKey,
            t.Columns.ToDictionary(c=>c.Id,c=>c.Version),bindings);
        void SameBindings(Dictionary<string,int>? value)=>Assert.Equal(assignments.OrderBy(p=>p.Key),value!.OrderBy(p=>p.Key));
        SameBindings((await Current()).UploadBindings);
        Assert.Equal(SettingsStatus.Success,(await settings.SaveAsync(admin,"date_format","DD/MM/YYYY",ct)).Status);
        var document=(await backup.ExportAsync(admin,ct))!;Assert.Equal(2,document.Version);SameBindings(document.Templates.Single().UploadBindings);
        Assert.True(ConfigurationBackupValidation.IsValid(document,101));
        var invalid=document with {Templates=[document.Templates.Single() with {UploadBindings=new Dictionary<string,int>{{"amount",ids["document"]}}}]};
        Assert.Equal("invalid",(await backup.PreviewAsync(admin,invalid,ct)).Status);
        var oldMissing=document with {Version=1,Templates=[document.Templates.Single() with {UploadBindings=null}]};
        Assert.Equal("review",(await backup.PreviewAsync(admin,oldMissing,ct)).Status);
        Assert.Equal("invalid",(await backup.PreviewAsync(admin,oldMissing with {Version=2},ct)).Status);
        Assert.Equal("success",(await backup.PreviewAsync(admin,document with {Version=1},ct)).Status);
        Assert.Equal("invalid",(await backup.PreviewAsync(other,document,ct)).Status);
        var member=(await sessions.FindAsync(new string('c',64),ct))!;
        Assert.Null(await backup.ExportAsync(member,ct));Assert.Equal("forbidden",(await backup.RestoreAsync(member,document,"anything",true,ct)).Status);
        var current=await Current();var swapped=new Dictionary<string,int>(assignments){["amount"]=ids["alternate"]};
        Assert.Equal(TemplateStatus.Success,(await templates.SaveAsync(admin,templateId,Edit(current,swapped),ct)).Status);
        Assert.Equal(TemplateStatus.Review,(await templates.SaveAsync(admin,templateId,Edit(current,assignments),ct)).Status);
        Assert.Equal(SettingsStatus.Success,(await settings.SaveAsync(admin,"date_format","YYYY-MM-DD",ct)).Status);
        var preview=await backup.PreviewAsync(admin,document,ct);Assert.Equal("success",preview.Status);
        await Sql("CREATE FUNCTION public.p29_audit_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.module='ConfigurationBackup' THEN RAISE EXCEPTION 'synthetic';END IF;RETURN NEW;END $$;CREATE TRIGGER p29_audit_fail BEFORE INSERT ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.p29_audit_fail()");
        await Assert.ThrowsAsync<PostgresException>(()=>backup.RestoreAsync(admin,document,preview.Revision,true,ct));
        Assert.Equal(swapped.OrderBy(p=>p.Key),(await Current()).UploadBindings!.OrderBy(p=>p.Key));
        Assert.Contains("YYYY-MM-DD",JsonSerializer.Serialize((await settings.ReadAsync(admin,"date_format",ct)).Value));
        await Sql("DROP TRIGGER p29_audit_fail ON public.audit_log;DROP FUNCTION public.p29_audit_fail()");
        Assert.Equal("success",(await backup.RestoreAsync(admin,document,preview.Revision,true,ct)).Status);SameBindings((await Current()).UploadBindings);
        Assert.Equal("conflict",(await backup.RestoreAsync(admin,document,preview.Revision,true,ct)).Status);

        await using(var app=Host(true))
        {
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));string address=Address(app);
            app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var http=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};
            http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);http.DefaultRequestHeaders.Add("Origin",address);http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(admin));
            using var wire=JsonDocument.Parse(await http.GetStringAsync($"/api/templates/{templateId}",ct));
            Assert.Equal(ids["amount"],wire.RootElement.GetProperty("upload_bindings").GetProperty("amount").GetInt32());
            current=await Current();
            using var update=new HttpRequestMessage(HttpMethod.Put,$"/api/templates/{templateId}");update.Headers.Add("X-Template-Revision",current.ConfigurationKey);
            update.Content=JsonContent.Create(new {template_name=current.Name,selected_columns=current.Columns.Select(c=>c.Id).ToArray(),upload_bindings=swapped});
            Assert.Equal(HttpStatusCode.OK,(await http.SendAsync(update,ct)).StatusCode);Assert.Equal(swapped.OrderBy(p=>p.Key),(await Current()).UploadBindings!.OrderBy(p=>p.Key));
            current=await Current();Assert.Equal(TemplateStatus.Success,(await templates.SaveAsync(admin,templateId,Edit(current,assignments),ct)).Status);
            using var templatesPage=await http.GetAsync("/static/templates.html",ct);
            Assert.Equal(HttpStatusCode.OK,templatesPage.StatusCode);
            Assert.Contains("/shell/context.js",await templatesPage.Content.ReadAsStringAsync(ct),StringComparison.Ordinal);
        }
        await using(var app=Host(false))
        {
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));string address=Address(app);app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var http=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);
            string html=await http.GetStringAsync("/static/setup.html",ct);Assert.Contains("data-backups-enabled=\"false\"",html);
            using var templatesPage=await http.GetAsync("/static/templates.html",ct);
            Assert.Equal(HttpStatusCode.OK,templatesPage.StatusCode);
        }
        // Current rows, history-only data and existing uploads independently
        // protect assignments in both direct edits and restore previews.
        current=await Current();var desired=(await backup.ExportAsync(admin,ct))!;
        var wanted=desired with {Templates=desired.Templates.Select(t=>t.Id==templateId?t with {UploadBindings=swapped}:t).ToArray()};
        foreach(string target in new[]{"current","history","upload"})
        {
            await Sql(target=="current"?$"INSERT INTO public.client_data(client_id,template_id,row_data) VALUES(10,{templateId},'{{\"exact\":\"0012.340\"}}')":
                target=="history"?$"INSERT INTO public.client_data_history VALUES(1,{templateId},'{{\"exact\":\"0012.340\"}}')":$"INSERT INTO public.uploaded_files(id,template_id,status) VALUES(1,{templateId},'Pending')");
            string before=await Fingerprint();Assert.Equal("review",(await backup.PreviewAsync(admin,wanted,ct)).Status);
            Assert.Equal(TemplateStatus.Review,(await templates.SaveAsync(admin,templateId,Edit(await Current(),swapped),ct)).Status);Assert.Equal(before,await Fingerprint());
            await Sql($"DELETE FROM public.client_data WHERE template_id={templateId};DELETE FROM public.client_data_history;DELETE FROM public.uploaded_files");
        }
        Assert.Equal(TemplateStatus.Success,(await templates.SaveAsync(admin,templateId,Edit(await Current(),[]) ,ct)).Status);
        var unbound=(await backup.ExportAsync(admin,ct))!;
        Assert.Equal("success",(await backup.PreviewAsync(admin,unbound with {Version=1,Templates=unbound.Templates.Select(t=>t with {UploadBindings=null}).ToArray()},ct)).Status);
        await Sql("ALTER TABLE public.platform_template_upload_bindings RENAME TO p29_hidden_bindings");
        try {Assert.Equal(TemplateStatus.Review,(await templates.SaveAsync(admin,templateId,Edit(await Current(),assignments),ct)).Status);}
        finally {await Sql("ALTER TABLE public.p29_hidden_bindings RENAME TO platform_template_upload_bindings");}
        Assert.Contains("preserve",await Fingerprint());

        async Task<string> Fingerprint()
        {await using var q=new NpgsqlCommand("SELECT jsonb_build_object('current',(SELECT jsonb_agg(to_jsonb(c) ORDER BY id) FROM public.client_data c),'history',(SELECT jsonb_agg(to_jsonb(h) ORDER BY id) FROM public.client_data_history h))::text",connection);return (string)(await q.ExecuteScalarAsync(ct))!;}
        WebApplicationFactory<Program> Host(bool enabled)=>new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
        {
            services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;o.CommandTimeoutSeconds=10;});
            services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5080";});services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);
            services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SettingsManagementOptions>(o=>o.Enabled=true);
            services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);services.PostConfigure<TemplateManagementOptions>(o=>{o.Enabled=true;o.WriteEnabled=true;});services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            services.PostConfigure<ConfigurationBackupOptions>(o=>o.Enabled=enabled);
        }));
        static string Address(WebApplicationFactory<Program> app)=>app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
    }
}
