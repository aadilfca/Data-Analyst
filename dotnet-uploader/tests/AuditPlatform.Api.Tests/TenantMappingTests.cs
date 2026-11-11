using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Columns;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.Templates;
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

public sealed class TenantMappingTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    [Fact]
    public async Task Saved_headers_are_scoped_versioned_atomic_and_independent_of_display_labels()
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
            CREATE TABLE public.templates(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_name TEXT UNIQUE NOT NULL,created_by TEXT DEFAULT 'Admin',created_date TIMESTAMPTZ DEFAULT now(),is_active BOOLEAN DEFAULT TRUE,is_system BOOLEAN DEFAULT FALSE,has_subtotals_by_default BOOLEAN DEFAULT FALSE,rate_source TEXT,data_purpose TEXT NOT NULL DEFAULT 'standard',ledger_columns TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE public.template_column_mappings(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id),sort_order INTEGER NOT NULL DEFAULT 0,reconciliation_role TEXT,compare_for_changes BOOLEAN,comparison_review_required BOOLEAN NOT NULL DEFAULT FALSE);
            CREATE TABLE public.audit_rule_documents(id INTEGER PRIMARY KEY,template_id INTEGER);
            CREATE TABLE public.audit_rule_document_fields(id INTEGER PRIMARY KEY,document_id INTEGER,master_column_id INTEGER,field_name TEXT);
            CREATE TABLE public.audit_rule_cross_checks(id INTEGER PRIMARY KEY,compare_field TEXT);
            CREATE TABLE public.saved_column_mappings(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,template_column_id INTEGER,excel_column TEXT,compare_for_changes BOOLEAN);
            CREATE TABLE public.uploaded_files(id INTEGER PRIMARY KEY,template_id INTEGER,status TEXT,ledger_account_code TEXT);
            CREATE TABLE public.upload_validation_plans(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,status TEXT);
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER,ADD COLUMN source_identity TEXT,ADD COLUMN source_fingerprint TEXT;
            CREATE TABLE public.client_data_history(id INTEGER PRIMARY KEY,template_id INTEGER,row_data JSONB,source_identity TEXT);
            INSERT INTO public.master_columns(id,column_name,data_type) OVERRIDING SYSTEM VALUE VALUES(1,'Legacy Key','Text');
            INSERT INTO public.templates(id,template_name) OVERRIDING SYSTEM VALUE VALUES(501,'Legacy template');
            INSERT INTO public.template_column_mappings(template_id,column_id) VALUES(501,1);
            INSERT INTO public.saved_column_mappings VALUES(1,10,501,1,'Legacy ERP',FALSE);
            SELECT setval(pg_get_serial_sequence('public.master_columns','id'),100,true);
            SELECT setval(pg_get_serial_sequence('public.templates','id'),600,true);
            INSERT INTO public.users VALUES(1,'mapping-user','pending',TRUE,NULL,NULL),(2,'other-admin','pending',TRUE,'admin',NULL),(3,'unassigned-admin','pending',TRUE,'admin',NULL);
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(11,'B','Beta'),(20,'D','Business'),(30,'C','Gamma');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10),(repeat('f',64),1,now()+interval '1 hour',11),
                (repeat('b',64),2,now()+interval '1 hour',20),(repeat('c',64),1,now()+interval '1 hour',30),(repeat('d',64),2,now()+interval '1 hour',30),
                (repeat('e',64),1,now()-interval '1 hour',10),(repeat('1',64),3,now()+interval '1 hour',30),(repeat('2',64),3,now()+interval '1 hour',20);
            """);
        await using(var hash=new NpgsqlCommand("UPDATE public.users SET password_hash=$1 WHERE id=1",connection))
        { hash.Parameters.AddWithValue(BCrypt.Net.BCrypt.HashPassword("p14-synthetic")); await hash.ExecuteNonQueryAsync(Token); }
        await fixture.ApplyTenantFoundationAsync(Token); await fixture.ApplyEngagementFoundationAsync(Token); await fixture.ApplyTenantUserRolesAsync(Token);
        var root=FindRoot();
        await Sql(await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0006_session_tenant_context.sql"),Token));
        await fixture.ApplyTenantColumnsAsync(Token); await fixture.ApplyTenantTemplatesAsync(Token);
        await using(var rollback=await connection.BeginTransactionAsync(Token))
        {
            await using var command=new NpgsqlCommand(await PostgresFixture.ReadSavedMappingsAsync(Token),connection,rollback);
            await command.ExecuteNonQueryAsync(Token); await rollback.RollbackAsync(Token);
        }
        await using(var absent=new NpgsqlCommand("SELECT to_regclass('public.platform_tenant_saved_mapping_sets') IS NULL",connection)) Assert.True((bool)(await absent.ExecuteScalarAsync(Token))!);
        await fixture.ApplySavedMappingsAsync(Token);
        Assert.Equal(PostgresErrorCodes.DuplicateTable,(await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplySavedMappingsAsync(Token))).SqlState);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_tenant_saved_mapping_sets"));
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'Firm A','audit_firm'),(102,'Business B','business'),(103,'Firm C','audit_firm');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(103,1,'member'),(103,2,'admin'),(103,3,'member'),(102,3,'member');
            UPDATE public.sessions SET selected_tenant_id=CASE WHEN token IN(repeat('a',64),repeat('e',64),repeat('f',64)) THEN 101 WHEN token IN(repeat('b',64),repeat('2',64)) THEN 102 ELSE 103 END;
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(11,101,now()),(20,102,now()),(30,103,now());
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,103,30,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,103,1,'internee');
            """);
        using var pool=fixture.CreatePool(); var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var limits=Options.Create(new LogPersistenceOptions()); var audit=new PostgresAuditWriter(new LogSanitizer(limits),limits);
        var nativeTemplates=Options.Create(new TemplateManagementOptions {Enabled=true}); var options=Options.Create(new SavedMappingOptions {Enabled=true});
        var columns=new TenantColumnService(tx,audit,nativeTemplates,options); var templates=new TenantTemplateService(tx,audit,options);
        var service=new TenantMappingService(tx,audit); var store=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions {Enabled=true}));
        async Task<VerifiedSession> Session(char token)=>(await store.FindAsync(new string(token,64),Token))!;
        var admin=await Session('a'); var beta=await Session('f'); var other=await Session('b'); var staff=await Session('c'); var cAdmin=await Session('d');
        async Task<int> Column(string name,string type,VerifiedSession? actor=null)
        { var result=await columns.SaveAsync(actor??admin,null,new(name,type),Token); Assert.Equal(ColumnStatus.Success,result.Status); return result.ColumnId!.Value; }
        async Task<TemplateDefinition> Template(int id,VerifiedSession? actor=null)=>Assert.Single((await templates.GetAsync(actor??admin,id,Token)).Templates!);
        async Task<int> CreateTemplate(string name,int[] ids,VerifiedSession? actor=null)
        { var result=await templates.SaveAsync(actor??admin,null,new(name,ids),Token); Assert.Equal(TemplateStatus.Success,result.Status); return result.Id!.Value; }
        async Task<MappingSnapshot> Read(VerifiedSession actor,int client,int template)
        { var result=await service.ReadAsync(actor,client,template,Token); Assert.Equal(MappingStatus.Success,result.Status); return result.Snapshot!; }
        async Task<MappingResult> Save(VerifiedSession actor,int client,int template,MappingEntry[] entries,MappingSnapshot? snapshot=null)
        { var current=snapshot??await Read(actor,client,template); return await service.SaveAsync(actor,client,template,entries,current.Revision,current.ConfigurationKey,Token); }
        int key=await Column("Reference","Text"),date=await Column("Date","Date"),amount=await Column("Amount","Numeric");
        int id=await CreateTemplate("Mapping fixture",[key,date,amount]); var definition=await Template(id);
        int otherKey=await Column("Reference","Text",other),otherId=await CreateTemplate("Mapping fixture",[otherKey],other);
        int cKey=await Column("Assigned field","Text",cAdmin),cId=await CreateTemplate("Assigned template",[cKey],cAdmin);
        var empty=await Read(admin,10,id); Assert.Empty(empty.Entries); Assert.Equal(0,empty.Revision); Assert.False(empty.NeedsReview);
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin,11,id,Token)).Status);
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin with {ClientId=11},11,id,Token)).Status);
        Assert.Equal(MappingStatus.NotFound,(await service.ReadAsync(admin,10,otherId,Token)).Status);
        var unassigned=await Session('1'); var businessMember=await Session('2');
        Assert.NotNull(unassigned); Assert.NotNull(businessMember);
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(unassigned,30,cId,Token)).Status);
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(businessMember,20,otherId,Token)).Status);
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin with {UserId=2},10,id,Token)).Status);
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin with {Token=new string('e',64)},10,id,Token)).Status);
        Assert.Equal(MappingStatus.Invalid,(await Save(admin,10,id,[new(otherKey,"Foreign")],empty)).Status);
        Assert.Equal(MappingStatus.Invalid,(await Save(admin,10,id,[new(key,"First"),new(key,"Second")],empty)).Status);
        Assert.Equal(MappingStatus.Invalid,(await Save(admin,10,id,[new(key,"Invalid\0header")],empty)).Status);
        Assert.Equal(MappingStatus.Conflict,(await service.SaveAsync(admin,10,id,[new(key,"ERP")],0,"forged",Token)).Status);
        await Sql($"INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name) VALUES(1,101,'Legacy field'); INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,tenant_id,storage_key) VALUES({id},1,99,101,'Legacy Key')");
        Assert.Equal(MappingStatus.Review,(await service.ReadAsync(admin,10,id,Token)).Status);
        await Sql($"DELETE FROM public.template_column_mappings WHERE template_id={id} AND column_id=1");
        empty=await Read(admin,10,id);

        await Sql($"INSERT INTO public.upload_validation_plans VALUES(1,10,{id},'ready'),(2,11,{id},'ready'),(3,30,{cId},'ready')");
        var created=await Save(admin,10,id,[new(key,"  ERP Reference  "),new(date," ")],empty);
        Assert.Equal(MappingStatus.Success,created.Status); Assert.Equal(1,created.Count); Assert.Equal("  ERP Reference  ",Assert.Single(created.Snapshot!.Entries).Header);
        Assert.True(created.Snapshot.Revision>empty.Revision); Assert.False(created.Snapshot.NeedsReview);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE id=1 AND status='superseded'"));
        Assert.Equal(2L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE id IN(2,3) AND status='ready'"));
        Assert.Empty((await Read(beta,11,id)).Entries);
        Assert.Equal(MappingStatus.Success,(await Save(other,20,otherId,[new(otherKey,"Different tenant ERP")])).Status);
        Assert.Equal(MappingStatus.Success,(await Save(staff,30,cId,[new(cKey,"Assigned ERP")])).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=FALSE WHERE engagement_id=201 AND user_id=1");
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(staff,30,cId,Token)).Status);
        await Sql("UPDATE public.platform_engagement_assignments SET is_active=TRUE,team_role='manager' WHERE engagement_id=201 AND user_id=1");
        Assert.Equal(MappingStatus.Success,(await Save(staff,30,cId,[new(cKey,"Manager ERP")])).Status);
        await Sql("UPDATE public.platform_audit_engagements SET is_active=FALSE WHERE id=201");
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(staff,30,cId,Token)).Status);
        await Sql("UPDATE public.platform_audit_engagements SET is_active=TRUE WHERE id=201");
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='member' WHERE tenant_id=101 AND user_id=1");
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin with {Role="admin"},10,id,Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='admin',is_active=FALSE WHERE tenant_id=101 AND user_id=1");
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin,10,id,Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE tenant_id=101 AND user_id=1");

        foreach(var sql in new[] {$"UPDATE public.platform_tenant_saved_mapping_sets SET client_id=11 WHERE client_id=10 AND template_id={id}",
            $"DELETE FROM public.platform_tenant_saved_mapping_sets WHERE client_id=10 AND template_id={id}",
            $"UPDATE public.platform_tenant_saved_mappings SET storage_key='rewritten' WHERE client_id=10 AND template_id={id}",
            $"UPDATE public.platform_tenant_saved_mappings SET tenant_id=102 WHERE client_id=10 AND template_id={id}",
            $"INSERT INTO public.platform_tenant_saved_mapping_sets(tenant_id,client_id,template_id,template_binding_key) VALUES(101,20,{id},repeat('A',64))"})
            Assert.Equal(PostgresErrorCodes.CheckViolation,(await Assert.ThrowsAsync<PostgresException>(()=>Sql(sql))).SqlState);
        var before=await Read(admin,10,id);
        var json=JsonSerializer.Serialize(new Dictionary<string,object> {[definition.Columns[0].StorageKey]="A-001",[definition.Columns[2].StorageKey]="00123456789012345678901234567890.5600"});
        foreach(var sql in new[] {"INSERT INTO public.client_data(client_id,template_id,row_data,source_identity,source_fingerprint) VALUES(10,$1,$2,'identity','fingerprint')",
            "INSERT INTO public.client_data_history VALUES(7,$1,$2,'history-identity')"})
        { await using var source=new NpgsqlCommand(sql,connection); source.Parameters.AddWithValue(id); source.Parameters.AddWithValue(NpgsqlDbType.Jsonb,json); await source.ExecuteNonQueryAsync(Token); }
        async Task<string> Fingerprint()
        { await using var command=new NpgsqlCommand("SELECT md5(string_agg(row_data::text||source_identity||source_fingerprint,'' ORDER BY id))||(SELECT md5(string_agg(row_data::text||source_identity,'' ORDER BY id)) FROM public.client_data_history) FROM public.client_data",connection); return (string)(await command.ExecuteScalarAsync(Token))!; }
        var fingerprint=await Fingerprint();
        Assert.Equal(ColumnStatus.Success,(await columns.SaveAsync(admin,key,new("Invoice reference","Text"),Token)).Status);
        var relabelled=await Read(admin,10,id); Assert.Equal(before.BindingKey,relabelled.BindingKey); Assert.NotEqual(before.ConfigurationKey,relabelled.ConfigurationKey);
        Assert.False(relabelled.NeedsReview); Assert.Equal(before.Entries,relabelled.Entries);
        Assert.Equal(MappingStatus.Conflict,(await Save(admin,10,id,[new(key,"Old form")],before)).Status);
        definition=await Template(id);
        Assert.Equal(TemplateStatus.Success,(await templates.SaveAsync(admin,id,new(definition.Name,[key,date,amount],Comparisons:new() {[amount]=false},Revision:definition.ConfigurationKey),Token)).Status);
        var changed=await Read(admin,10,id); Assert.True(changed.NeedsReview); Assert.NotEqual(changed.BindingKey,relabelled.BindingKey);
        Assert.Equal(MappingStatus.Success,(await Save(admin,10,id,[new(key,"Reviewed ERP"),new(amount,"SYNTHETIC_HEADER_SECRET")],changed)).Status);
        Assert.False((await Read(admin,10,id)).NeedsReview); Assert.Equal(fingerprint,await Fingerprint());
        var saved=await Read(admin,10,id);
        var cleared=await service.ClearAsync(admin,10,id,saved.Revision,saved.ConfigurationKey,Token);
        Assert.Equal(MappingStatus.Success,cleared.Status); Assert.Equal(2,cleared.Count); Assert.Empty(cleared.Snapshot!.Entries); Assert.True(cleared.Snapshot.Revision>saved.Revision);
        Assert.Equal(MappingStatus.Conflict,(await Save(admin,10,id,[new(key,"Stale resurrection")],saved)).Status);
        Assert.Equal(1L,await Count($"SELECT count(*) FROM public.platform_tenant_saved_mapping_sets WHERE tenant_id=101 AND client_id=10 AND template_id={id}"));
        var raceSnapshot=cleared.Snapshot;
        var race=await Task.WhenAll(Save(admin,10,id,[new(key,"Winner A")],raceSnapshot),Save(admin,10,id,[new(key,"Winner B")],raceSnapshot));
        Assert.Single(race,r=>r.Status==MappingStatus.Success); Assert.Single(race,r=>r.Status==MappingStatus.Conflict);

        // Saved-only references stay protective even with their HTTP feature
        // switched off; clearing releases them while retaining the revision.
        int spare=await Column("Saved-only field","Text"),spareId=await CreateTemplate("Saved-only template",[spare,key]);
        Assert.Equal(MappingStatus.Success,(await Save(beta,11,spareId,[new(spare,"Spare ERP")])).Status);
        var spareTemplate=await Template(spareId);
        Assert.Equal(TemplateStatus.Success,(await templates.RemoveAsync(admin,spareId,spare,spareTemplate.ConfigurationKey,Token)).Status);
        Assert.True((await Read(beta,11,spareId)).NeedsReview);
        var flagOffColumns=new TenantColumnService(tx,audit,nativeTemplates); var flagOffTemplates=new TenantTemplateService(tx,audit);
        Assert.Equal(ColumnStatus.DependencyReview,(await flagOffColumns.DeleteAsync(admin,spare,Token)).Status);
        spareTemplate=await Template(spareId);
        Assert.Equal(TemplateStatus.Review,(await flagOffTemplates.DeleteAsync(admin,spareId,spareTemplate.ConfigurationKey,Token)).Status);
        var spareSnapshot=await Read(beta,11,spareId);
        Assert.Equal(MappingStatus.Success,(await service.ClearAsync(beta,11,spareId,spareSnapshot.Revision,spareSnapshot.ConfigurationKey,Token)).Status);
        Assert.Equal(ColumnStatus.Success,(await flagOffColumns.DeleteAsync(admin,spare,Token)).Status);
        Assert.Equal(TemplateStatus.Success,(await flagOffTemplates.DeleteAsync(admin,spareId,spareTemplate.ConfigurationKey,Token)).Status);

        saved=await Read(admin,10,id); var sets=await Count("SELECT count(*) FROM public.platform_tenant_saved_mapping_sets");
        await Sql("UPDATE public.upload_validation_plans SET status='ready' WHERE id=1");
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_unavailable");
        try
        {
            await Assert.ThrowsAsync<PostgresException>(()=>Save(admin,10,id,[new(date,"Rollback date")],saved));
            await Assert.ThrowsAsync<PostgresException>(()=>service.ClearAsync(admin,10,id,saved.Revision,saved.ConfigurationKey,Token));
            await Assert.ThrowsAsync<PostgresException>(()=>Save(beta,11,id,[new(key,"Rollback new set")]));
        }
        finally { await Sql("ALTER TABLE public.audit_unavailable RENAME TO audit_log"); }
        var after=await Read(admin,10,id); Assert.Equal(saved.Revision,after.Revision); Assert.Equal(saved.Entries,after.Entries);
        Assert.Equal(sets,await Count("SELECT count(*) FROM public.platform_tenant_saved_mapping_sets"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE id=1 AND status='ready'"));
        Assert.Equal(fingerprint,await Fingerprint());
        await Sql("ALTER TABLE public.platform_tenant_saved_mappings RENAME TO missing_mapping_dependency");
        try { await Assert.ThrowsAsync<PostgresException>(()=>service.ReadAsync(admin,10,id,Token)); }
        finally { await Sql("ALTER TABLE public.missing_mapping_dependency RENAME TO platform_tenant_saved_mappings"); }
        await Sql("UPDATE public.clients SET is_active=FALSE WHERE id=10");
        Assert.Equal(MappingStatus.NotFound,(await service.ReadAsync(admin,10,id,Token)).Status);
        await Sql("UPDATE public.clients SET is_active=TRUE WHERE id=10");
        await Sql("UPDATE public.sessions SET selected_client_id=11 WHERE token=repeat('a',64)");
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin,10,id,Token)).Status);
        await Sql("UPDATE public.sessions SET selected_client_id=10,context_version=context_version+1 WHERE token=repeat('a',64)");
        Assert.Equal(MappingStatus.Forbidden,(await service.ReadAsync(admin,10,id,Token)).Status); admin=await Session('a');
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.saved_column_mappings WHERE id=1 AND excel_column='Legacy ERP' AND compare_for_changes=FALSE"));
        await using(var log=new NpgsqlCommand("SELECT client_id,details FROM public.audit_log WHERE module='TenantMappings'",connection))
        {
            await using var reader=await log.ExecuteReaderAsync(Token); int logs=0;
            while(await reader.ReadAsync(Token))
            { logs++; Assert.Contains(reader.GetInt32(0),new[] {10,11,20,30}); string details=reader.GetString(1); Assert.Contains("tenant_id",details); Assert.DoesNotContain("SYNTHETIC_HEADER_SECRET",details); }
            Assert.True(logs>0);
        }
        int browserId=await CreateTemplate("P14 Browser template",[key,date,amount]);
        await using(var disabled=new WebApplicationFactory<Program>()) using(var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync("/api/upload/saved-mapping?client_id=10&template_id=1",Token)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>
        {
            builder.UseEnvironment("Testing"); builder.ConfigureServices(services=>
            {
                services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
                services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5080";});
                services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true); services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true); services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true); services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
            });
        });
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        var address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        using var http=new HttpClient(new HttpClientHandler {UseCookies=false,AllowAutoRedirect=false}) {BaseAddress=new Uri(address)};
        string path=$"/api/upload/saved-mapping?client_id=10&template_id={id}";
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync(path,Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync(path.ToUpperInvariant(),Token)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(admin));
        saved=await Read(admin,10,id);
        var payload=new {client_id=10,template_id=id,mappings=new[] {new {template_column_id=key,excel_column="HTTP ERP",storage_key="forged",compare_for_changes=false}},
            mapping_revision=saved.Revision,configuration_key=saved.ConfigurationKey,tenant_id=102};
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PostAsJsonAsync("/api/upload/save-mapping",payload,Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.OK,(await http.PostAsJsonAsync("/api/upload/save-mapping",payload,Token)).StatusCode);
        Assert.Equal(HttpStatusCode.Conflict,(await http.PostAsJsonAsync("/api/upload/save-mapping",payload,Token)).StatusCode);
        var actual=Assert.Single((await Read(admin,10,id)).Entries); Assert.Equal(definition.Columns[0].StorageKey,actual.StorageKey);
        Assert.True(TenantTemplateService.EffectiveComparison((await Template(id)).Columns.Single(c=>c.Id==key),await Template(id)));
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.DeleteAsync(path,Token)).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.PostAsJsonAsync("/api/upload/save-mapping",new {client_id=10,template_id=id},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.PostAsJsonAsync("/api/upload/save-mapping",new {client_id=10,template_id=id,
            mappings=new object?[] {null},mapping_revision=0,configuration_key="redacted"},Token)).StatusCode);
        var start=new ProcessStartInfo(Path.Combine(root,"..","venv","Scripts","python.exe")) {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root,"tests","p14_browser.py")); start.ArgumentList.Add(address); start.ArgumentList.Add(browserId.ToString()); start.ArgumentList.Add(cId.ToString());
        using var process=Process.Start(start)!; var output=process.StandardOutput.ReadToEndAsync(Token); var errors=process.StandardError.ReadToEndAsync(Token);
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(Token); timeout.CancelAfter(TimeSpan.FromSeconds(100));
        try { await process.WaitForExitAsync(timeout.Token); } catch { process.Kill(entireProcessTree:true); throw; }
        Assert.True(process.ExitCode==0,await output+await errors);
    }
    [Fact]
    public async Task Saved_mapping_activation_requires_native_templates()
    {
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
            services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true)));
        Assert.Throws<OptionsValidationException>(()=>app.CreateClient());
    }
    private static string FindRoot()
    {
        var directory=new DirectoryInfo(AppContext.BaseDirectory);
        while(directory is not null&&!File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx"))) directory=directory.Parent;
        return directory?.FullName??throw new InvalidOperationException("Solution root required");
    }
}
