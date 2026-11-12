using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Uploads;
using AuditPlatform.Api.Workbooks;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class DerivedGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public async ValueTask InitializeAsync()
    {
        DirectoryInfo? root=new(AppContext.BaseDirectory);
        while(root is not null&&!File.Exists(Path.Combine(root.FullName,"AuditPlatform.slnx"))) root=root.Parent;
        if(root is null) throw new InvalidOperationException("Solution not found");
        DirectoryPath=Path.Combine(root.FullName,".tools","p26-tests-"+Guid.NewGuid().ToString("N"));
        var start=new ProcessStartInfo(Path.Combine(root.FullName,"..","venv","Scripts","python.exe"))
            {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root.FullName,"tests","p26_derived_fields_oracle.py"));start.ArgumentList.Add(DirectoryPath);
        using var process=Process.Start(start)!;var output=process.StandardOutput.ReadToEndAsync();var errors=process.StandardError.ReadToEndAsync();
        using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(60));
        try {await process.WaitForExitAsync(deadline.Token);} catch {process.Kill(entireProcessTree:true);throw;}
        Assert.True(process.ExitCode==0,await output+await errors);
        Oracle=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(DirectoryPath,"oracle.json")));
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class UploadDerivedFieldTests(DerivedGoldenFixture golden) : IClassFixture<DerivedGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static UploadValidationPlanOptions Settings()=>new(){Enabled=true,SigningKey=Convert.ToBase64String(Enumerable.Range(0,32).Select(i=>(byte)i).ToArray())};
    [Fact]
    public void RateAndProjectionMatchIndependentPython()
    {
        foreach(var vector in golden.Oracle.RootElement.GetProperty("rates").EnumerateArray())
        {
            var amount=new IdentityAtom("text",vector.GetProperty("amount").GetString());var quantity=new IdentityAtom("text",vector.GetProperty("quantity").GetString());int precision=vector.GetProperty("precision").GetInt32();
            if(vector.GetProperty("review").GetBoolean()) Assert.Throws<IdentityReview>(()=>UploadDerivedFieldService.Rate(amount,quantity,precision));
            else
            {
                var result=UploadDerivedFieldService.Rate(amount,quantity,precision);
                if(vector.GetProperty("expected").ValueKind==JsonValueKind.Null) Assert.Equal("blank",result.Kind);
                else Assert.Equal(0,UploadExactAmount.Parse(vector.GetProperty("expected").GetRawText()).CompareTo(UploadExactAmount.Parse(result.Text!)));
            }
        }
        var keys=new Dictionary<string,string>{{"document","a"},{"date","b"},{"account_code","c"},{"account_name","d"},{"debit","e"},{"credit","f"}};
        var profile=new UploadDerivedProfile(keys,[],null,"fixture",Treasury:[new("001","cash")]);
        var values=new List<UploadDerivedValues>();
        foreach(var vector in golden.Oracle.RootElement.GetProperty("projections").EnumerateArray())
        {
            var raw=UploadIdentityValue.FromJson(vector.GetProperty("native").GetRawText());var p=UploadDerivedFieldService.Project(profile,raw,vector.GetProperty("date").GetString());values.Add(p);
            var expected=vector.GetProperty("expected");Assert.Equal(expected.GetProperty("account_code").GetString(),p.Account);
            Assert.Equal(expected.GetProperty("classification_status").GetString(),p.Status);Assert.Equal(expected.GetProperty("transaction_fortnight").GetString(),p.Fortnight);
            Assert.Equal(expected.GetProperty("debit").GetDouble(),p.Debit);Assert.Equal(expected.GetProperty("credit").GetDouble(),p.Credit);
        }
        Assert.Equal(golden.Oracle.RootElement.GetProperty("fingerprint").GetString(),UploadDerivedFieldService.Fingerprint(values));
        Assert.Equal(UploadDerivedFieldService.Fingerprint(values),UploadDerivedFieldService.Fingerprint(values.AsEnumerable().Reverse()));
        Assert.NotEqual(UploadDerivedFieldService.Fingerprint(values),UploadDerivedFieldService.Fingerprint(values.Take(1)));
        var expectedDecisions=golden.Oracle.RootElement.GetProperty("decisions").EnumerateArray().ToArray();
        var resolved=UploadDerivedFieldService.Resolve(expectedDecisions.Select(d=>new UploadVoucherDecision(d.GetProperty("voucher_identity").GetString()!,d.GetProperty("voucher_identity").GetString()!,
            d.GetProperty("occurrence_key").GetString()!,d.GetProperty("voucher_fingerprint").GetString()!,"2026-04-01",2,d.GetProperty("upload_id").GetInt32(),d.GetProperty("source_sequence").GetString()!)));
        foreach(var expected in expectedDecisions)
        {
            var actual=resolved.Single(d=>d.Key==expected.GetProperty("occurrence_key").GetString());
            Assert.Equal(expected.GetProperty("decision_status").GetString(),actual.Status);Assert.Equal(expected.GetProperty("decision_reason").GetString(),actual.Reason);
            Assert.Equal(expected.GetProperty("canonical_occurrence_key").GetString(),actual.Canonical);
        }
        var imported=profile with {RateMode="imported",Keys=new Dictionary<string,string>{{"rate","rate"}}};
        var rate=new IdentityAtom("text","001.234567890123456789");Assert.Equal(rate,UploadDerivedFieldService.Prepare(imported,new Dictionary<string,IdentityAtom>{{"rate",rate}})["rate"]);
        Assert.Throws<IdentityReview>(()=>UploadDerivedFieldService.Project(profile,new Dictionary<string,IdentityAtom>{{"a",new("text","V")},{"c",new("number","123")}},"2026-04-01"));
        var tiny=profile with {Keys=new Dictionary<string,string>{{"document","a"},{"amount","e"}}};
        Assert.Throws<IdentityReview>(()=>UploadDerivedFieldService.Project(tiny,new Dictionary<string,IdentityAtom>{{"a",new("text","V")},{"e",new("number","1e-50")}},"2026-04-01"));
    }
    [Fact]
    public async Task OwnedAtomicDerivationFreshnessAndRecovery()
    {
        await using var fixture=new PostgresFixture();await fixture.InitializeAsync();await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection=await fixture.OpenControlConnectionAsync(Token);
        async Task Sql(string sql) {await using var command=new NpgsqlCommand(sql,connection);await command.ExecuteNonQueryAsync(Token);}
        async Task<long> Count(string sql) {await using var command=new NpgsqlCommand(sql,connection);return (long)(await command.ExecuteScalarAsync(Token))!;}
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER,selected_tenant_id INTEGER,context_version BIGINT NOT NULL DEFAULT 0);
            CREATE TABLE public.master_columns(id INTEGER PRIMARY KEY,column_name TEXT,data_type TEXT,is_active BOOLEAN DEFAULT TRUE,category TEXT,notes TEXT);
            CREATE TABLE public.templates(id INTEGER PRIMARY KEY,template_name TEXT,created_by TEXT DEFAULT 'Admin',created_date TIMESTAMPTZ DEFAULT now(),is_active BOOLEAN DEFAULT TRUE,is_system BOOLEAN DEFAULT FALSE,has_subtotals_by_default BOOLEAN DEFAULT FALSE,data_purpose TEXT DEFAULT 'standard',ledger_columns TEXT DEFAULT '{}',rate_source TEXT);
            CREATE TABLE public.template_column_mappings(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id),sort_order INTEGER,reconciliation_role TEXT,compare_for_changes BOOLEAN,comparison_review_required BOOLEAN DEFAULT FALSE);
            CREATE TABLE public.uploaded_files(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,file_name TEXT,file_path TEXT,file_type TEXT,record_count INTEGER,status TEXT,uploaded_by TEXT,client_id INTEGER,template_id INTEGER);
            CREATE TABLE public.upload_validation_plans(plan_id TEXT PRIMARY KEY,file_id INTEGER REFERENCES public.uploaded_files(id),client_id INTEGER,template_id INTEGER,
                file_sha256 TEXT,request_signature TEXT,plan_json JSONB,status TEXT CHECK(status IN('ready','applied','expired','superseded')),created_at TIMESTAMPTZ DEFAULT now(),expires_at TIMESTAMPTZ,applied_at TIMESTAMPTZ);
            INSERT INTO public.users VALUES(1,'owner','unused',TRUE,'admin',NULL),(2,'other','unused',TRUE,'user',NULL),(3,'auditor','unused',TRUE,'user',NULL);
            INSERT INTO public.clients VALUES(10,'A','Alpha',TRUE),(20,'B','Beta',TRUE);
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0);
            INSERT INTO public.master_columns VALUES(1,'col_1','Text',TRUE,'General',NULL),(2,'col_2','Date',TRUE,'General',NULL),(3,'col_3','Numeric',TRUE,'General',NULL),(4,'col_4','Text',TRUE,'General',NULL),(5,'col_5','Text',TRUE,'General',NULL),(6,'col_6','Text',TRUE,'General',NULL);
            INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture');
            """);
        await fixture.ApplyTenantFoundationAsync(Token);await fixture.ApplyEngagementFoundationAsync(Token);await fixture.ApplyTenantUserRolesAsync(Token);
        await fixture.ApplyTenantColumnsAsync(Token);await fixture.ApplyTenantTemplatesAsync(Token);await fixture.ApplySavedMappingsAsync(Token);await fixture.ApplyUploadStagingAsync(Token);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Document','native'),(2,101,'Date','native'),(3,101,'Amount','native'),(4,101,'Memo','native'),(5,101,'Balance','native'),(6,101,'Extra','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Fixture','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key) VALUES(1,1,0,'document_id',101,'col_1'),(1,2,1,'document_date',101,'col_2'),(1,3,2,'footing',101,'col_3'),(1,4,3,NULL,101,'col_4'),(1,5,4,NULL,101,'col_5'),(1,6,5,NULL,101,'col_6');
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            INSERT INTO public.client_data(client_id,row_data) VALUES(20,'{"sentinel":"unchanged"}');
            """);
        using var pool=fixture.CreatePool(timeoutSeconds:10);var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
        var logs=Options.Create(new LogPersistenceOptions());var audit=new PostgresAuditWriter(new LogSanitizer(logs),logs);
        var sessions=new PostgresSessionStore(tx,audit,Options.Create(new ApplicationShellOptions{Enabled=true}));
        var owner=(await sessions.FindAsync(new string('a',64),Token))!;var staff=(await sessions.FindAsync(new string('c',64),Token))!;
        var other=(await sessions.FindAsync(new string('b',64),Token))!;
        string directory=Path.Combine(Path.GetTempPath(),"AuditPlatform-P24-"+Guid.NewGuid().ToString("N"));var clock=new Clock();
        var staging=new UploadStagingService(tx,audit,Options.Create(new UploadStagingOptions{Enabled=true,RootDirectory=directory}),clock);
        var mappingService=new TenantMappingService(tx,audit);var preparation=new UploadPreparationService(staging,mappingService,Options.Create(new UploadPreparationOptions{Enabled=true}));
        var inputOptions=Options.Create(new UploadInputValidationOptions{Enabled=true});
        await Sql("""
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS template_id INTEGER;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS document_group_id TEXT;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS source_identity TEXT;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS source_row_number INTEGER;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS source_order_upload_id INTEGER;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS extract_date DATE;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS change_detection_status TEXT DEFAULT 'unchanged';
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS upload_id INTEGER;
            ALTER TABLE public.uploaded_files ADD COLUMN IF NOT EXISTS ledger_account_code TEXT;
            UPDATE public.template_column_mappings SET compare_for_changes=FALSE WHERE column_id=5;
            INSERT INTO public.client_data(id,client_id,template_id,row_data,document_group_id,source_identity,source_row_number,source_order_upload_id,extract_date)
            OVERRIDING SYSTEM VALUE VALUES
            (101,10,1,'{"col_1":"V1","col_2":"03/04/2026","col_3":100,"col_4":"Office  supplies","col_5":"100 DR","col_6":null}','1-V1','old-101',2,11,'2026-04-03'),
            (102,10,1,'{"col_1":"V1","col_2":"03/04/2026","col_3":100,"col_4":"Office  supplies","col_5":"100 DR","col_6":null}','1-V1','old-102',3,11,'2026-04-03'),
            (103,10,1,'{"col_1":"V2","col_2":"08/04/2026","col_3":50,"col_4":"Other","col_5":"50 DR","col_6":null}','1-V2','old-103',4,11,'2026-04-08');
            """);
        var footingOptions=Options.Create(new UploadFootingOptions{Enabled=true});
        var coverage=new UploadCoverageService(preparation,tx,Options.Create(new UploadCoverageOptions{Enabled=true}),footingOptions,inputOptions);
        var snapshots=new PostgresReadSnapshot(pool,Options.Create(new PostgresOptions()));
        var identity=new UploadIdentityService(coverage,snapshots,Options.Create(new UploadIdentityOptions{Enabled=true}));
        using var stream=File.OpenRead(Path.Combine(golden.DirectoryPath,"plans.xlsx"));
        var receipt=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",stream,Token);Assert.Equal("success",receipt.Status);int id=receipt.FileId!.Value;
        await fixture.ApplyUploadValidationPlansAsync(Token);
        await Sql("""
            DELETE FROM public.client_data WHERE client_id=10;
            ALTER TABLE public.client_data ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();ALTER TABLE public.client_data ADD COLUMN created_by TEXT;
            ALTER TABLE public.client_data ADD COLUMN source_fingerprint TEXT;ALTER TABLE public.client_data ADD COLUMN extract_amount REAL;
            ALTER TABLE public.client_data ADD COLUMN rate_source TEXT;
            ALTER TABLE public.client_data ADD COLUMN derived_voucher_id TEXT;ALTER TABLE public.client_data ADD COLUMN derived_account_code TEXT;
            ALTER TABLE public.client_data ADD COLUMN derived_account_name TEXT;ALTER TABLE public.client_data ADD COLUMN derived_transaction_date DATE;
            ALTER TABLE public.client_data ADD COLUMN derived_transaction_month TEXT;ALTER TABLE public.client_data ADD COLUMN derived_transaction_fortnight TEXT;
            ALTER TABLE public.client_data ADD COLUMN derived_debit DOUBLE PRECISION DEFAULT 0;ALTER TABLE public.client_data ADD COLUMN derived_credit DOUBLE PRECISION DEFAULT 0;
            ALTER TABLE public.client_data ADD COLUMN derived_treasury_type TEXT;ALTER TABLE public.client_data ADD COLUMN derived_treasury_account_code TEXT;
            ALTER TABLE public.client_data ADD COLUMN derived_classification_status TEXT;ALTER TABLE public.client_data ADD COLUMN derived_version INTEGER DEFAULT 1;
            CREATE UNIQUE INDEX idx_client_data_active_identity_unique ON public.client_data(client_id,template_id,source_identity)
                WHERE is_deleted=FALSE AND change_detection_status<>'deleted' AND source_identity IS NOT NULL AND source_identity<>'';
            SELECT setval(pg_get_serial_sequence('public.client_data','id'),1000);
            ALTER TABLE public.uploaded_files ADD COLUMN upload_date TIMESTAMPTZ DEFAULT now();
            CREATE TABLE public.client_data_history(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id INTEGER,template_id INTEGER,client_data_id INTEGER,
                action TEXT,changed_at TIMESTAMPTZ DEFAULT now(),changed_by TEXT,source_upload_id INTEGER,replacing_upload_id INTEGER,source_identity TEXT,row_data JSONB,changed_fields JSONB);
            CREATE TABLE public.verification_details(id INTEGER PRIMARY KEY,client_data_id INTEGER,validation_status TEXT);
            CREATE TABLE public.voucher_line_documents(client_data_id INTEGER);CREATE TABLE public.voucher_line_physical_bill_items(source_document_row_id INTEGER);
            CREATE TABLE public.cashflow_prepared_lines(source_client_data_id INTEGER);
            CREATE TABLE public.platform_tenant_settings(tenant_id INTEGER,key TEXT,value TEXT,PRIMARY KEY(tenant_id,key));
            INSERT INTO public.platform_tenant_settings VALUES(101,'rate_precision','2');
            CREATE TABLE public.account_control_cashflow_treasury_accounts(client_id INTEGER,account_code TEXT,treasury_type TEXT,is_active BOOLEAN);
            INSERT INTO public.account_control_cashflow_treasury_accounts VALUES(10,'001','cash',TRUE);
            CREATE TABLE public.account_control_cashflow_settings(client_id INTEGER PRIMARY KEY,source_template_id INTEGER);
            INSERT INTO public.account_control_cashflow_settings VALUES(10,1);
            CREATE TABLE public.account_control_cashflow_source_mappings(client_id INTEGER,template_id INTEGER,role TEXT,template_column_id INTEGER);
            INSERT INTO public.account_control_cashflow_source_mappings VALUES(10,1,'voucher_id',1),(10,1,'date',2),(10,1,'account_code',4),(10,1,'debit',3),(10,1,'credit',5);
            CREATE TABLE public.cashflow_voucher_occurrences(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id INTEGER,template_id INTEGER,upload_id INTEGER REFERENCES public.uploaded_files(id),
                voucher_id TEXT,voucher_identity TEXT,occurrence_key TEXT,voucher_fingerprint TEXT,fingerprint_version INTEGER,transaction_date DATE,source_line_count BIGINT,
                source_extract_as_of TIMESTAMPTZ,decision_status TEXT,decision_reason TEXT,canonical_occurrence_id INTEGER REFERENCES public.cashflow_voucher_occurrences(id),
                resolved_at TIMESTAMPTZ,created_at TIMESTAMPTZ DEFAULT now(),UNIQUE(client_id,template_id,upload_id,occurrence_key));
            CREATE TABLE public.cashflow_report_revisions(client_id INTEGER PRIMARY KEY,revision BIGINT,changed_at TIMESTAMPTZ,changed_by TEXT,change_reason TEXT);
            CREATE TABLE public.cash_bank_summary_read_models(client_id INTEGER,template_id INTEGER);
            UPDATE public.master_columns SET data_type='Numeric' WHERE id=5;
            UPDATE public.platform_tenant_columns SET display_name='Account' WHERE column_id=4;
            UPDATE public.platform_tenant_columns SET display_name='Credit' WHERE column_id=5;
            UPDATE public.platform_tenant_columns SET display_name='Name' WHERE column_id=6;
            UPDATE public.template_column_mappings SET compare_for_changes=TRUE WHERE column_id=5;
            INSERT INTO public.master_columns VALUES(7,'col_7','Text',TRUE,'General',NULL);
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(7,101,'Memo','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,tenant_id,storage_key,compare_for_changes) VALUES(1,7,6,101,'col_7',FALSE);
            """);
        await fixture.ApplyUploadImportsAsync(Token);await fixture.ApplyUploadDerivedFieldsAsync(Token);
        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadDerivedFieldsAsync(Token));
        await Sql("""INSERT INTO public.platform_template_upload_bindings VALUES(1,101,'{"amount":3,"account_code":4,"account_name":6,"debit":3,"credit":5}')""");
        var corrections=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true}));
        var settings=Options.Create(Settings());var signer=new UploadValidationPlanSigner(settings);var store=new UploadValidationPlanStore(tx,settings,clock);
        var plans=new UploadValidationPlanService(corrections,store,signer,settings,clock);var importOptions=Options.Create(new UploadImportOptions{Enabled=true,BatchSize=2});
        var imports=new UploadImportService(tx,store,signer,staging,new UploadImportWriter(importOptions),audit,importOptions,clock);
        async Task<ValidationPlanRequest> Review(string sheet="Base",string mode="replace_range")
        {
            var source=new PreparationRequest(10,1,sheet,RequestId:Guid.NewGuid(),Mappings:[new(1,1),new(2,2),new(3,3),new(4,4),new(5,5),new(6,6),new(7,7)]);
            var prepared=await preparation.PrepareAsync(owner,id,source,Token);Assert.Equal("success",prepared.Status);
            source=source with {Sha256=prepared.Output!.Context.Sha256,ConfigurationKey=prepared.Output.Context.ConfigurationKey,MappingRevision=prepared.Output.Context.MappingRevision};
            int control=sheet=="Removed"||sheet=="Partial"?5:sheet=="Incomplete"?4:7;
            return new(Guid.NewGuid(),new(new(new(new(source,mode,control),true),"2026-04-01","2026-04-30",true)));
        }
        async Task<UploadImportRequest> Plan(string sheet="Base",string mode="replace_range")
        {
            var review=await Review(sheet,mode);var result=await plans.CreateAsync(owner,id,review,Token);Assert.True(result.Status=="success",result.Code);Assert.Equal("ready",result.Output!.Status);
            return new(Guid.NewGuid(),result.Output.PlanId!,result.Output.Signature!,review.Review);
        }
        async Task<UploadImportResult> Import(UploadImportRequest p)=>await imports.ImportAsync(owner,id,p,Token);
        async Task Stage()
        {
            using var file=File.OpenRead(Path.Combine(golden.DirectoryPath,"plans.xlsx"));var received=await staging.ReceiveAsync(owner,10,1,Guid.NewGuid(),"synthetic.xlsx",file,Token);
            Assert.Equal("success",received.Status);id=received.FileId!.Value;
        }
        var first=await Plan();var applied=await Import(first);Assert.True(applied.Status=="success",applied.Code);
        Assert.Equal(5L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND derived_configuration_hash IS NOT NULL"));
        Assert.Equal(3L,await Count("SELECT count(*) FROM public.client_data WHERE derived_treasury_type='cash' AND derived_account_code='001'"));
        Assert.Equal(2L,await Count("SELECT count(*) FROM public.cashflow_voucher_occurrences WHERE decision_status='current'"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
        Assert.Equal(2L,await Count("SELECT revision FROM public.cashflow_report_revisions WHERE client_id=10"));
        Assert.Equal(2L,await Count("SELECT count(*) FROM public.client_data WHERE row_data->>'col_1'='V1' AND row_data->>'col_3'='100'"));
        Assert.Equal(applied.Output,(await Import(first)).Output);
        await Stage();var same=await plans.CreateAsync(owner,id,await Review(),Token);Assert.Equal("no_changes",same.Output!.Status);
        await Sql("UPDATE public.platform_tenant_columns SET display_name='Renamed account code' WHERE column_id=4");
        var metadata=await Plan();Assert.Equal(0,(await plans.CheckAsync(owner,id,new(metadata.PlanId,metadata.Signature,metadata.Review),Token)).Output!.Counts.Changed);
        Assert.Equal("success",(await Import(metadata)).Status);Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
        Assert.Equal(3L,await Count("SELECT revision FROM public.cashflow_report_revisions WHERE client_id=10"));
        await Stage();var stale=await Plan("Changed");await Sql("UPDATE public.account_control_cashflow_treasury_accounts SET treasury_type='bank' WHERE client_id=10");
        Assert.Equal("derived_configuration_changed",(await Import(stale)).Code);Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
        var stalePartial=await Plan("Partial","correction");Assert.Equal("derived_retained_rows_review_required",(await Import(stalePartial)).Code);
        var correction=await Plan("Changed");
        await Sql("CREATE FUNCTION public.p26_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic derived failure'; END $$;CREATE TRIGGER p26_fail AFTER INSERT ON public.cashflow_voucher_occurrences FOR EACH ROW EXECUTE FUNCTION public.p26_fail()");
        await Assert.ThrowsAsync<PostgresException>(()=>Import(correction));Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));
        Assert.Equal(3L,await Count("SELECT revision FROM public.cashflow_report_revisions WHERE client_id=10"));
        await Sql("DROP TRIGGER p26_fail ON public.cashflow_voucher_occurrences");
        var corrected=await Import(correction);Assert.True(corrected.Status=="success",corrected.Code);
        Assert.Equal(2L,await Count("SELECT count(*) FROM public.client_data_history WHERE action='changed'"));
        Assert.Equal(3L,await Count("SELECT count(*) FROM public.client_data WHERE derived_treasury_type='bank'"));
        Assert.Equal(4L,await Count("SELECT revision FROM public.cashflow_report_revisions WHERE client_id=10"));
        await Stage();var partial=await Plan("Partial","correction");Assert.Equal("success",(await Import(partial)).Status);
        Assert.Equal(5L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10"));
        await Stage();var incomplete=await Plan("Incomplete","correction");Assert.Equal("derived_partial_voucher_review_required",(await Import(incomplete)).Code);
        var removal=await Plan("Removed");Assert.Equal("success",(await Import(removal)).Status);
        Assert.Equal(3L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.cashflow_voucher_occurrences WHERE voucher_identity='V2' AND decision_status='superseded'"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'"));
        Assert.Equal("forbidden",(await imports.ImportAsync(other,id,removal,Token)).Status);
        await Stage();var lost=await Plan();long receipts=await Count("SELECT count(*) FROM public.platform_upload_imports");
        await using(var proxy=fixture.CreateCommitProxy()) using(var proxiedPool=fixture.CreatePool(proxy:proxy))
        {
            var proxiedTx=new PostgresTransactions(proxiedPool,Options.Create(new PostgresOptions()));
            var proxied=new UploadImportService(proxiedTx,store,signer,staging,new UploadImportWriter(importOptions),audit,importOptions,clock);
            clock.OnRead=proxy.Arm;var outcome=await proxied.ImportAsync(owner,id,lost,Token);clock.OnRead=null;
            await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Token);
            Assert.True(outcome.Status=="success"||outcome.Code=="import_outcome_unknown_retry_same_operation",outcome.Code);
        }
        Assert.Equal("success",(await Import(lost)).Status);Assert.Equal(receipts+1,await Count("SELECT count(*) FROM public.platform_upload_imports"));
        await Sql("""
            INSERT INTO public.templates(id,template_name,rate_source) VALUES(2,'tpl_rate','auto');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,template_kind,binding_origin) VALUES(2,101,'Rate fixture','purchase_order','native');
            INSERT INTO public.master_columns VALUES(8,'col_8','Numeric',TRUE,'General',NULL),(9,'col_9','Numeric',TRUE,'General',NULL);
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(8,101,'Quantity','native'),(9,101,'Rate','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key) VALUES
                (2,1,0,'document_id',101,'col_1'),(2,2,1,'document_date',101,'col_2'),(2,3,2,'footing',101,'col_3'),(2,8,3,NULL,101,'col_8'),(2,9,4,NULL,101,'col_9'),(2,7,5,NULL,101,'col_7');
            INSERT INTO public.platform_template_upload_bindings VALUES(2,101,'{"amount":3,"quantity":8,"rate":9,"description":7}');
            """);
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("""UPDATE public.platform_template_upload_bindings SET bindings='{"amount":7}' WHERE template_id=2"""));
        var templateService=new TenantTemplateService(tx,audit);
        var rateDefinition=(await templateService.GetAsync(owner,2,Token)).Templates!.Single();
        var badBinding=new TemplateInput(rateDefinition.Name,rateDefinition.Columns.Select(c=>c.Id).ToArray(),Revision:rateDefinition.ConfigurationKey,UploadBindings:new(){{"rate",3},{"amount",3}});
        Assert.Equal(TemplateStatus.Invalid,(await templateService.SaveAsync(owner,2,badBinding,Token)).Status);
        var rename=new TemplateInput("Renamed rate fixture",rateDefinition.Columns.Select(c=>c.Id).ToArray(),Revision:rateDefinition.ConfigurationKey);
        Assert.Equal(TemplateStatus.Success,(await templateService.SaveAsync(owner,2,rename,Token)).Status);
        rateDefinition=(await templateService.GetAsync(owner,2,Token)).Templates!.Single();Assert.Equal(9,rateDefinition.UploadBindings!["rate"]);Assert.Equal("auto",rateDefinition.RateSource);
        Assert.Equal(TemplateStatus.NotFound,(await templateService.GetAsync(other,2,Token)).Status);
        int rateId;
        async Task StageRate()
        {
            using var file=File.OpenRead(Path.Combine(golden.DirectoryPath,"rates.xlsx"));var received=await staging.ReceiveAsync(owner,10,2,Guid.NewGuid(),"synthetic-rate.xlsx",file,Token);
            Assert.Equal("success",received.Status);rateId=received.FileId!.Value;
        }
        async Task<ValidationPlanRequest> RateReview(bool imported=false)
        {
            var source=new PreparationRequest(10,2,"Rates",RequestId:Guid.NewGuid(),Mappings:[new(1,1),new(2,2),new(3,3),new(8,4),new(9,imported?5:null),new(7,6)]);
            var prepared=await preparation.PrepareAsync(owner,rateId,source,Token);Assert.Equal("success",prepared.Status);
            source=source with {Sha256=prepared.Output!.Context.Sha256,ConfigurationKey=prepared.Output.Context.ConfigurationKey,MappingRevision=prepared.Output.Context.MappingRevision};
            return new(Guid.NewGuid(),new(new(new(new(source,"replace_range",4),true),"2026-04-01","2026-04-30",true)));
        }
        rateId=0;await StageRate();var rateReview=await RateReview();
        var ratePlan=(await plans.CreateAsync(owner,rateId,rateReview,Token));Assert.True(ratePlan.Status=="success",ratePlan.Code);
        var rateRequest=new UploadImportRequest(Guid.NewGuid(),ratePlan.Output!.PlanId!,ratePlan.Output.Signature!,rateReview.Review);
        await Sql("UPDATE public.platform_tenant_settings SET value='4' WHERE key='rate_precision'");
        Assert.Equal("derived_configuration_changed",(await imports.ImportAsync(owner,rateId,rateRequest,Token)).Code);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data WHERE template_id=2"));
        await Sql("UPDATE public.platform_tenant_settings SET value='2' WHERE key='rate_precision'");
        ratePlan=await plans.CreateAsync(owner,rateId,rateReview with {RequestId=Guid.NewGuid()},Token);
        rateRequest=new(Guid.NewGuid(),ratePlan.Output!.PlanId!,ratePlan.Output.Signature!,rateReview.Review);
        var rateApplied=await imports.ImportAsync(owner,rateId,rateRequest,Token);Assert.True(rateApplied.Status=="success",rateApplied.Code);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE template_id=2 AND row_data->>'col_1'='R1' AND row_data->>'col_9'='3.33' AND rate_source='derived'"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE template_id=2 AND row_data->>'col_1'='R2' AND row_data->>'col_9' IS NULL"));
        await StageRate();var rateSame=await plans.CreateAsync(owner,rateId,await RateReview(),Token);Assert.Equal("no_changes",rateSame.Output!.Status);
        var importedReview=await RateReview(true);var importedPlan=await plans.CreateAsync(owner,rateId,importedReview,Token);Assert.True(importedPlan.Status=="success",importedPlan.Code);
        var importedRequest=new UploadImportRequest(Guid.NewGuid(),importedPlan.Output!.PlanId!,importedPlan.Output.Signature!,importedReview.Review);
        await Sql("UPDATE public.platform_tenant_settings SET value='4' WHERE key='rate_precision'");
        var importedApplied=await imports.ImportAsync(owner,rateId,importedRequest,Token);Assert.True(importedApplied.Status=="success",importedApplied.Code);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE template_id=2 AND row_data->>'col_9'='001.234567890123456789' AND rate_source='imported'"));
        rateDefinition=(await templateService.GetAsync(owner,2,Token)).Templates!.Single();
        var rebind=new TemplateInput(rateDefinition.Name,rateDefinition.Columns.Select(c=>c.Id).ToArray(),Revision:rateDefinition.ConfigurationKey,UploadBindings:new(){{"amount",8},{"quantity",3},{"rate",9}});
        Assert.Equal(TemplateStatus.Review,(await templateService.SaveAsync(owner,2,rebind,Token)).Status);

    }
}
