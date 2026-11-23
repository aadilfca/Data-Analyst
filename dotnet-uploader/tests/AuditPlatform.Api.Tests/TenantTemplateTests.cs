using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Columns;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Templates;
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

public sealed class TenantTemplateTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    [Fact]
    public async Task Templates_preserve_fixed_bindings_and_guard_tenant_access_configuration_and_source_history()
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
            CREATE TABLE public.saved_column_mappings(id INTEGER PRIMARY KEY,template_id INTEGER,template_column_id INTEGER);
            CREATE TABLE public.uploaded_files(id INTEGER PRIMARY KEY,template_id INTEGER,status TEXT,ledger_account_code TEXT);
            CREATE TABLE public.upload_validation_plans(id INTEGER PRIMARY KEY,template_id INTEGER,status TEXT);
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER,ADD COLUMN source_identity TEXT,ADD COLUMN source_fingerprint TEXT;
            CREATE TABLE public.client_data_history(id INTEGER PRIMARY KEY,template_id INTEGER,row_data JSONB,source_identity TEXT);
            INSERT INTO public.master_columns(id,column_name,data_type) OVERRIDING SYSTEM VALUE VALUES(1,'Legacy Key','Text');
            INSERT INTO public.templates(id,template_name) OVERRIDING SYSTEM VALUE VALUES(501,'Legacy template');
            INSERT INTO public.template_column_mappings(template_id,column_id) VALUES(501,1);
            SELECT setval(pg_get_serial_sequence('public.master_columns','id'),100,true);
            SELECT setval(pg_get_serial_sequence('public.templates','id'),600,true);
            INSERT INTO public.users VALUES(1,'template-user','pending',TRUE,NULL,NULL),(2,'other-admin','pending',TRUE,'admin',NULL);
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(30,'C','Gamma');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',NULL),(repeat('b',64),2,now()+interval '1 hour',NULL),
                (repeat('c',64),1,now()+interval '1 hour',NULL),(repeat('d',64),2,now()+interval '1 hour',NULL),(repeat('e',64),1,now()-interval '1 hour',NULL);
            """);
        await using(var hash=new NpgsqlCommand("UPDATE public.users SET password_hash=$1 WHERE id=1",connection))
        { hash.Parameters.AddWithValue(BCrypt.Net.BCrypt.HashPassword("p13-synthetic")); await hash.ExecuteNonQueryAsync(Token); }
        await fixture.ApplyTenantFoundationAsync(Token); await fixture.ApplyEngagementFoundationAsync(Token); await fixture.ApplyTenantUserRolesAsync(Token);
        var root=FindRoot();
        await Sql(await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0006_session_tenant_context.sql"),Token));
        await fixture.ApplyTenantColumnsAsync(Token);
        await using(var rollback=await connection.BeginTransactionAsync(Token))
        {
            await using var command=new NpgsqlCommand(await PostgresFixture.ReadTenantTemplatesAsync(Token),connection,rollback);
            await command.ExecuteNonQueryAsync(Token); await rollback.RollbackAsync(Token);
        }
        await using(var absent=new NpgsqlCommand("SELECT to_regclass('public.platform_tenant_templates') IS NULL",connection)) Assert.True((bool)(await absent.ExecuteScalarAsync(Token))!);
        await fixture.ApplyTenantTemplatesAsync(Token);
        Assert.Equal(PostgresErrorCodes.DuplicateTable,(await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyTenantTemplatesAsync(Token))).SqlState);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_tenant_templates"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.templates WHERE id=501 AND template_name='Legacy template' AND data_purpose='standard'"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.template_column_mappings WHERE template_id=501 AND column_id=1 AND tenant_id IS NULL AND storage_key IS NULL"));
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'Firm A','audit_firm'),(102,'Business B','business'),(103,'Firm C','audit_firm');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(103,1,'member'),(103,2,'admin');
            UPDATE public.sessions SET selected_tenant_id=CASE WHEN token IN(repeat('a',64),repeat('e',64)) THEN 101 WHEN token=repeat('b',64) THEN 102 ELSE 103 END;
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(30,103,now());
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,103,30,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,103,1,'internee');
            """);
        using var pool=fixture.CreatePool(); var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var limits=Options.Create(new LogPersistenceOptions()); var audit=new PostgresAuditWriter(new LogSanitizer(limits),limits);
        var options=Options.Create(new TemplateManagementOptions {Enabled=true});
        var columns=new TenantColumnService(tx,audit,options); var service=new TenantTemplateService(tx,audit);
        var store=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions {Enabled=true}));
        var admin=(await store.FindAsync(new string('a',64),Token))!; var other=(await store.FindAsync(new string('b',64),Token))!;
        var member=(await store.FindAsync(new string('c',64),Token))!; var cAdmin=(await store.FindAsync(new string('d',64),Token))!;
        async Task<int> Column(string name,string type,VerifiedSession? actor=null)
        { var result=await columns.SaveAsync(actor??admin,null,new(name,type),Token); Assert.Equal(ColumnStatus.Success,result.Status); return result.ColumnId!.Value; }
        async Task<TemplateDefinition> Current(int id,VerifiedSession? actor=null)=>Assert.Single((await service.GetAsync(actor??admin,id,Token)).Templates!);
        TemplateInput Edit(TemplateDefinition t)=>new(t.Name,t.Columns.Select(c=>c.Id).ToArray(),t.Purpose,t.Ledger,RateSource:t.RateSource,Subtotals:t.Subtotals,Revision:t.ConfigurationKey);
        int key=await Column("Reference","Text"), alternate=await Column("Alternate","Text"),date=await Column("Posting Date","Date"),date2=await Column("Other Date","Date"),amount=await Column("Exact Amount","Numeric");
        int otherColumn=await Column("Reference","Text",other);
        var input=new TemplateInput("Purchase Order",[key,alternate,date,date2,amount],Roles:new() {[key]="document_id",[date]="document_date",[amount]="footing"},RateSource:"imported",Subtotals:true);
        var created=await service.SaveAsync(admin,null,input,Token); Assert.Equal(TemplateStatus.Success,created.Status); int id=created.Id!.Value;
        var before=await Current(id); Assert.Equal("purchase_order",before.Kind); Assert.Equal("imported",before.RateSource);
        Assert.Equal(input.Columns,before.Columns.Select(c=>c.Id).ToArray()); Assert.Equal("template-user",before.CreatedBy);
        Assert.All(before.Columns,c=>Assert.StartsWith("col_",c.StorageKey)); Assert.True(before.Subtotals);
        Assert.Equal(TemplateStatus.NotFound,(await service.GetAsync(other,id,Token)).Status);
        Assert.Equal(TemplateStatus.NotFound,(await service.SaveAsync(other,id,Edit(before),Token)).Status);
        Assert.Equal(TemplateStatus.NotFound,(await service.DeleteAsync(other,id,before.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Forbidden,(await service.SaveAsync(member,null,input,Token)).Status);
        Assert.Equal(TemplateStatus.Forbidden,(await service.ListAsync(admin with {UserId=2},Token)).Status);
        Assert.Equal(TemplateStatus.Forbidden,(await service.ListAsync(admin with {Token=new string('e',64)},Token)).Status);
        Assert.Equal(TemplateStatus.Invalid,(await service.SaveAsync(admin,null,input with {Name="Foreign",Columns=[otherColumn]},Token)).Status);
        Assert.Equal(TemplateStatus.Invalid,(await service.SaveAsync(admin,null,input with {Name="Duplicate IDs",Columns=[key,key]},Token)).Status);
        Assert.Equal(TemplateStatus.Invalid,(await service.SaveAsync(admin,null,input with {Name="Bad Role",Roles=new() {[amount]="document_id"}},Token)).Status);
        Assert.Equal(TemplateStatus.Invalid,(await service.SaveAsync(admin,null,input with {Name="Two IDs",Roles=new() {[key]="document_id",[alternate]="document_id"}},Token)).Status);
        Assert.Equal(TemplateStatus.Invalid,(await service.SaveAsync(admin,null,input with {Name="Role outside",Roles=new() {[otherColumn]="footing"}},Token)).Status);
        Assert.Equal(TemplateStatus.Review,(await service.SaveAsync(admin,id,Edit(before) with {Revision=null},Token)).Status);
        Assert.Equal(TemplateStatus.Success,(await service.SaveAsync(other,null,new("Purchase Order",[otherColumn]),Token)).Status);
        var race=await Task.WhenAll(service.SaveAsync(admin,null,new("Race",[key]),Token),service.SaveAsync(admin,null,new("RACE",[key]),Token));
        Assert.Single(race,r=>r.Status==TemplateStatus.Success); Assert.Single(race,r=>r.Status==TemplateStatus.Conflict);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.templates t WHERE left(template_name,4)='tpl_' AND NOT EXISTS(SELECT 1 FROM public.platform_tenant_templates d WHERE d.template_id=t.id)"));

        // Database guards reject identity/key/owner changes even outside HTTP.
        foreach(var sql in new[] {$"UPDATE public.templates SET template_name='rewritten' WHERE id={id}",
            $"UPDATE public.platform_tenant_templates SET tenant_id=102 WHERE template_id={id}",
            $"UPDATE public.platform_tenant_templates SET template_kind='standard' WHERE template_id={id}",
            $"DELETE FROM public.platform_tenant_templates WHERE template_id={id}",
            $"UPDATE public.template_column_mappings SET storage_key='wrong' WHERE template_id={id}",
            $"INSERT INTO public.template_column_mappings(template_id,column_id,sort_order) VALUES({id},{key},99)"})
            Assert.Equal(PostgresErrorCodes.CheckViolation,(await Assert.ThrowsAsync<PostgresException>(()=>Sql(sql))).SqlState);
        Assert.Equal(PostgresErrorCodes.ForeignKeyViolation,(await Assert.ThrowsAsync<PostgresException>(()=>Sql($"DELETE FROM public.templates WHERE id={id}"))).SqlState);
        var foreignKey=Assert.Single((await columns.ListAsync(other,true,Token)).Columns!).StorageKey;
        Assert.Equal(PostgresErrorCodes.ForeignKeyViolation,(await Assert.ThrowsAsync<PostgresException>(()=>Sql($"INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,tenant_id,storage_key) VALUES({id},{otherColumn},99,101,'{foreignKey}')"))).SqlState);

        // Ordering is a complete permutation; removal and configuration saves
        // preserve existing per-mapping choices when optional input is omitted.
        Assert.Equal(TemplateStatus.Invalid,(await service.ReorderAsync(admin,id,[key],before.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Success,(await service.ReorderAsync(admin,id,[key,date,alternate,date2,amount],before.ConfigurationKey,Token)).Status);
        var reordered=await Current(id); Assert.Equal("document_date",reordered.Columns.Single(c=>c.Id==date).Role);
        Assert.Equal(TemplateStatus.Review,(await service.SaveAsync(admin,id,Edit(before),Token)).Status);
        var usage=await columns.UsageAsync(admin,key,Token); Assert.Equal(ColumnStatus.Success,usage.Status); Assert.True(usage.TemplatesCount>=1);
        Assert.Equal(ColumnStatus.DependencyReview,(await columns.SaveAsync(admin,key,new("Reference","Numeric"),Token)).Status);
        await Sql($"INSERT INTO public.upload_validation_plans VALUES(1,{id},'ready')");
        Assert.Equal(ColumnStatus.Success,(await columns.SaveAsync(admin,key,new("Invoice Reference","Text"),Token)).Status);
        var renamed=await Current(id); Assert.Equal(reordered.Columns.Single(c=>c.Id==key).StorageKey,renamed.Columns.Single(c=>c.Id==key).StorageKey);
        Assert.NotEqual(reordered.ConfigurationKey,renamed.ConfigurationKey);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE status='superseded'"));
        Assert.Equal(TemplateStatus.Review,(await service.SaveAsync(admin,id,Edit(reordered),Token)).Status);
        Assert.Equal(TemplateStatus.Success,(await service.SaveAsync(admin,id,Edit(renamed) with {Name="Renamed purchasing",RateSource="derived"},Token)).Status);
        var current=await Current(id); Assert.Equal("purchase_order",current.Kind); Assert.Equal("derived",current.RateSource);

        var json=JsonSerializer.Serialize(new Dictionary<string,object> {
            [current.Columns.Single(c=>c.Id==key).StorageKey]="A-001",[current.Columns.Single(c=>c.Id==alternate).StorageKey]="Different",
            [current.Columns.Single(c=>c.Id==date).StorageKey]="2026-03-31",[current.Columns.Single(c=>c.Id==amount).StorageKey]="00123456789012345678901234567890.5600"});
        foreach(var sql in new[] {"INSERT INTO public.client_data(client_id,template_id,row_data,source_identity,source_fingerprint) VALUES(10,$1,$2,'identity','fingerprint')",
            "INSERT INTO public.client_data_history VALUES(7,$1,$2,'history-identity')"})
        { await using var source=new NpgsqlCommand(sql,connection); source.Parameters.AddWithValue(id); source.Parameters.AddWithValue(NpgsqlDbType.Jsonb,json); await source.ExecuteNonQueryAsync(Token); }
        async Task<string> Fingerprint()
        { await using var command=new NpgsqlCommand("SELECT md5(string_agg(row_data::text||source_identity||source_fingerprint,'' ORDER BY id))||(SELECT md5(string_agg(row_data::text||source_identity,'' ORDER BY id)) FROM public.client_data_history) FROM public.client_data",connection); return (string)(await command.ExecuteScalarAsync(Token))!; }
        var fingerprint=await Fingerprint();
        Assert.Equal(TemplateStatus.Review,(await service.SaveAsync(admin,id,Edit(current) with {Roles=new() {[key]=null,[alternate]="document_id"}},Token)).Status);
        Assert.Equal(TemplateStatus.Review,(await service.SaveAsync(admin,id,Edit(current) with {Roles=new() {[date]=null,[date2]="document_date"}},Token)).Status);
        Assert.Equal(TemplateStatus.Review,(await service.RemoveAsync(admin,id,amount,current.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Review,(await service.DeleteAsync(admin,id,current.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Success,(await service.SaveAsync(admin,id,Edit(current) with {Comparisons=new() {[amount]=false}},Token)).Status);
        Assert.Equal(fingerprint,await Fingerprint()); current=await Current(id);
        Assert.False(TenantTemplateService.EffectiveComparison(current.Columns.Single(c=>c.Id==amount),current));
        Assert.Equal(ColumnStatus.Success,(await columns.SaveAsync(admin,amount,new("Relabelled Amount","Numeric"),Token)).Status);
        Assert.Equal(fingerprint,await Fingerprint()); current=await Current(id);
        // History by itself protects a field/date/identity after current data is removed.
        await Sql("DELETE FROM public.client_data");
        Assert.Equal(TemplateStatus.Review,(await service.RemoveAsync(admin,id,amount,current.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Review,(await service.DeleteAsync(admin,id,current.ConfigurationKey,Token)).Status);
        await Sql("DELETE FROM public.client_data_history");
        await Sql($"INSERT INTO public.audit_rule_documents VALUES(1,{id})");
        Assert.Equal(TemplateStatus.Review,(await service.RemoveAsync(admin,id,amount,current.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Review,(await service.DeleteAsync(admin,id,current.ConfigurationKey,Token)).Status);
        await Sql("DELETE FROM public.audit_rule_documents");
        await Sql($"INSERT INTO public.saved_column_mappings VALUES(1,{id},{amount})");
        Assert.Equal(TemplateStatus.Review,(await service.RemoveAsync(admin,id,amount,current.ConfigurationKey,Token)).Status);
        Assert.Equal(ColumnStatus.DependencyReview,(await columns.SaveAsync(admin,amount,new("Blocked","Numeric"),Token)).Status);
        await Sql("DELETE FROM public.saved_column_mappings");
        await Sql($"INSERT INTO public.uploaded_files VALUES(1,{id},'Imported','001')");
        Assert.Equal(TemplateStatus.Review,(await service.DeleteAsync(admin,id,current.ConfigurationKey,Token)).Status);
        await Sql("DELETE FROM public.uploaded_files");

        var equivalent=await service.SaveAsync(admin,null,new("Equivalent keys",[key,alternate]),Token);
        var equivalentTemplate=await Current(equivalent.Id!.Value);
        var equalJson=JsonSerializer.Serialize(equivalentTemplate.Columns.ToDictionary(c=>c.StorageKey,c=>(object)"SAME-001"));
        foreach(var sql in new[] {"INSERT INTO public.client_data(client_id,template_id,row_data,source_identity,source_fingerprint) VALUES(10,$1,$2,'equal-identity','equal-fingerprint')",
            "INSERT INTO public.client_data_history VALUES(8,$1,$2,'equal-history')"})
        { await using var source=new NpgsqlCommand(sql,connection); source.Parameters.AddWithValue(equivalentTemplate.Id); source.Parameters.AddWithValue(NpgsqlDbType.Jsonb,equalJson); await source.ExecuteNonQueryAsync(Token); }
        var equalFingerprint=await Fingerprint();
        Assert.Equal(TemplateStatus.Success,(await service.SaveAsync(admin,equivalentTemplate.Id,Edit(equivalentTemplate) with {Roles=new() {[alternate]="document_id"}},Token)).Status);
        Assert.Equal(equalFingerprint,await Fingerprint());
        await Sql("DELETE FROM public.client_data; DELETE FROM public.client_data_history");

        var emptyRoles=new Dictionary<string,int?> { ["voucher"]=null,["date"]=null,["description"]=null,["debit"]=null,["credit"]=null,["balance"]=null };
        var preview=await service.LedgerPreviewAsync(admin,Token); Assert.Equal(TemplateStatus.Success,preview.Status);
        var columnCount=await Count("SELECT count(*) FROM public.master_columns");
        var ledger=await service.PrepareLedgerAsync(admin,"Account Ledger",emptyRoles,null,false,Token); Assert.Equal(TemplateStatus.Success,ledger.Status);
        var ledgerTemplate=await Current(ledger.Id!.Value); Assert.Equal(6,ledgerTemplate.Columns.Count);
        Assert.False(TenantTemplateService.EffectiveComparison(ledgerTemplate.Columns.Single(c=>c.Id==ledgerTemplate.Ledger["balance"]),ledgerTemplate));
        Assert.All(ledgerTemplate.Columns.Where(c=>c.Id!=ledgerTemplate.Ledger["balance"]),c=>Assert.True(TenantTemplateService.EffectiveComparison(c,ledgerTemplate)));
        Assert.True((await service.PrepareLedgerAsync(admin,"Account Ledger",emptyRoles,null,false,Token)).Reused);
        Assert.Equal(columnCount+6,await Count("SELECT count(*) FROM public.master_columns"));
        var ledgerDate=ledgerTemplate.Ledger["date"];
        await Sql($"UPDATE public.master_columns SET is_active=FALSE WHERE id={ledgerDate}");
        Assert.Equal(TemplateStatus.Review,(await service.LedgerPreviewAsync(admin,Token)).Status);
        await Sql($"UPDATE public.master_columns SET is_active=TRUE WHERE id={ledgerDate}"); ledgerTemplate=await Current(ledgerTemplate.Id);
        Assert.Equal(TemplateStatus.Invalid,(await service.ReorderAsync(admin,ledgerTemplate.Id,ledgerTemplate.Columns.Select(c=>c.Id).Reverse().ToArray(),ledgerTemplate.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Invalid,(await service.RemoveAsync(admin,ledgerTemplate.Id,ledgerTemplate.Ledger["debit"],ledgerTemplate.ConfigurationKey,Token)).Status);
        await Sql($"INSERT INTO public.uploaded_files VALUES(2,{ledgerTemplate.Id},'Imported','001')");
        Assert.Equal(TemplateStatus.Review,(await service.SaveAsync(admin,ledgerTemplate.Id,Edit(ledgerTemplate) with {Purpose="standard",Ledger=[]},Token)).Status);
        await Sql("DELETE FROM public.uploaded_files");

        // Missing dependency evidence and audit failures never leave partial
        // templates, newly prepared columns, changed mappings or plan status.
        await Sql("ALTER TABLE public.saved_column_mappings RENAME TO missing_dependency");
        try { await Assert.ThrowsAsync<PostgresException>(()=>service.RemoveAsync(admin,id,amount,current.ConfigurationKey,Token)); }
        finally { await Sql("ALTER TABLE public.missing_dependency RENAME TO saved_column_mappings"); }
        current=await Current(id); var unchanged=current.ConfigurationKey; var templateCount=await Count("SELECT count(*) FROM public.templates");
        columnCount=await Count("SELECT count(*) FROM public.master_columns");
        await Sql($"UPDATE public.upload_validation_plans SET status='ready' WHERE template_id={id}");
        await Sql("ALTER TABLE public.audit_log RENAME TO audit_unavailable");
        try
        {
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveAsync(admin,null,new("ROLLBACK",[key]),Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.SaveAsync(admin,id,Edit(current) with {Name="ROLLBACK"},Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.DeleteAsync(admin,id,unchanged,Token));
            await Assert.ThrowsAsync<PostgresException>(()=>service.PrepareLedgerAsync(cAdmin,"Rollback ledger",emptyRoles,null,false,Token));
        }
        finally { await Sql("ALTER TABLE public.audit_unavailable RENAME TO audit_log"); }
        Assert.Equal(unchanged,(await Current(id)).ConfigurationKey); Assert.Equal(templateCount,await Count("SELECT count(*) FROM public.templates"));
        Assert.Equal(columnCount,await Count("SELECT count(*) FROM public.master_columns"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.upload_validation_plans WHERE status='ready'"));
        Assert.Equal(TemplateStatus.Success,(await service.DeleteAsync(admin,id,unchanged,Token)).Status);
        Assert.Equal(TemplateStatus.NotFound,(await service.GetAsync(admin,id,Token)).Status);
        Assert.Equal(5L,await Count($"SELECT count(*) FROM public.template_column_mappings WHERE template_id={id}"));
        Assert.Equal(TemplateStatus.Conflict,(await service.SaveAsync(admin,null,input with {Name=current.Name},Token)).Status);
        await Sql("INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name) VALUES(501,101,'Legacy display')");
        var legacy=await Current(501); Assert.Equal(TemplateStatus.Review,(await service.SaveAsync(admin,501,Edit(legacy),Token)).Status);
        Assert.Equal(TemplateStatus.Review,(await service.DeleteAsync(admin,501,legacy.ConfigurationKey,Token)).Status);
        await Sql($"UPDATE public.templates SET is_system=TRUE WHERE id={ledgerTemplate.Id}"); ledgerTemplate=await Current(ledgerTemplate.Id);
        Assert.Equal(TemplateStatus.Forbidden,(await service.DeleteAsync(admin,ledgerTemplate.Id,ledgerTemplate.ConfigurationKey,Token)).Status);
        Assert.Equal(TemplateStatus.Success,(await service.SaveAsync(admin,ledgerTemplate.Id,Edit(ledgerTemplate) with {Name="Guarded ledger"},Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=FALSE WHERE tenant_id=101 AND user_id=1");
        Assert.Equal(TemplateStatus.Forbidden,(await service.ListAsync(admin,Token)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET is_active=TRUE WHERE tenant_id=101 AND user_id=1");
        var sessions=new TenantSessionService(tx,audit); Assert.True(await sessions.SelectAsync(admin,103,TenantSessionService.ContextKey(admin),Token));
        Assert.Equal(TemplateStatus.Forbidden,(await service.SaveAsync(admin,null,input,Token)).Status);
        await Sql("UPDATE public.sessions SET selected_tenant_id=101,context_version=context_version+1 WHERE token=repeat('a',64)"); admin=(await store.FindAsync(admin.Token,Token))!;
        int cColumn=await Column("Other workspace field","Text",cAdmin);
        Assert.Equal(TemplateStatus.Success,(await service.SaveAsync(cAdmin,null,new("Firm C template",[cColumn]),Token)).Status);
        await using(var log=new NpgsqlCommand("SELECT details FROM public.audit_log WHERE module='TenantTemplates' ORDER BY id LIMIT 1",connection))
        { var details=(string)(await log.ExecuteScalarAsync(Token))!; Assert.Contains("tenant_id",details); Assert.DoesNotContain("Purchase Order",details); }

        await using(var disabled=new WebApplicationFactory<Program>()) using(var client=disabled.CreateClient())
            Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync("/api/templates",Token)).StatusCode);
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>
        {
            builder.UseEnvironment("Testing"); builder.ConfigureServices(services=>
            {
                services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
                services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5080";});
                services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true); services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true); services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<TemplateManagementOptions>(o=>{o.Enabled=true;o.WriteEnabled=true;});
            });
        });
        app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        var address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        using var http=new HttpClient(new HttpClientHandler {UseCookies=false,AllowAutoRedirect=false}) {BaseAddress=new Uri(address)};
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/templates",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+admin.Token);
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/API/TEMPLATES/",Token)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(admin)); http.DefaultRequestHeaders.Add("X-Tenant-Id","102");
        using(var list=await http.GetAsync("/api/templates?limit=1&offset=0",Token))
        { Assert.Equal(HttpStatusCode.OK,list.StatusCode); Assert.True(list.Headers.Contains("X-Total-Count")); Assert.Equal("no-store",list.Headers.CacheControl!.ToString()); }
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PostAsJsonAsync("/api/templates",new {template_name="HTTP",selected_columns=new[] {key}},Token)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin",address);
        using(var response=await http.PostAsJsonAsync("/api/templates",new {template_name="HTTP",selected_columns=new[] {key},created_by="forged",tenant_id=102,template_kind="vendor_bill"},Token))
        {
            Assert.Equal(HttpStatusCode.OK,response.StatusCode); using var body=JsonDocument.Parse(await response.Content.ReadAsStringAsync(Token));
            var t=await Current(body.RootElement.GetProperty("template_id").GetInt32()); Assert.Equal("template-user",t.CreatedBy); Assert.Equal("standard",t.Kind);
            foreach(var endpoint in new[] {$"/api/templates/{t.Id}",$"/api/templates/{t.Id}/columns",$"/api/templates/{t.Id}/upload-columns","/api/templates/by-name/HTTP","/api/templates/ledger-setup"})
                Assert.Equal(HttpStatusCode.OK,(await http.GetAsync(endpoint,Token)).StatusCode);
            Assert.Equal(HttpStatusCode.Conflict,(await http.PutAsJsonAsync($"/api/templates/{t.Id}",new {template_name="Older",selected_columns=new[] {key}},Token)).StatusCode);
        }
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.PostAsJsonAsync("/api/templates",new {template_name="Missing columns"},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity,(await http.GetAsync("/api/templates?limit=201",Token)).StatusCode);
        var templatesPage=await http.GetAsync("/static/templates.html",Token);
        Assert.Equal(HttpStatusCode.Found,templatesPage.StatusCode);
        Assert.Equal("/static/client_select.html",templatesPage.Headers.Location?.OriginalString);
    }
    [Fact]
    public async Task Template_activation_requires_shell_and_columns()
    {
        await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
            services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true)));
        Assert.Throws<OptionsValidationException>(()=>app.CreateClient());
    }
    private static string FindRoot()
    {
        var directory=new DirectoryInfo(AppContext.BaseDirectory);
        while(directory is not null&&!File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx"))) directory=directory.Parent;
        return directory?.FullName??throw new InvalidOperationException("Solution root required");
    }
}
