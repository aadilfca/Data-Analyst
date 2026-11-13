using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.DataBank;
using AuditPlatform.Api.DataIntegrity;
using AuditPlatform.Api.StorageCleanup;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Uploads;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class StorageCleanupTests(ImportGoldenFixture golden) : IClassFixture<ImportGoldenFixture>
{
    private static CancellationToken Ct=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider {public DateTimeOffset Current=DateTimeOffset.UtcNow;public override DateTimeOffset GetUtcNow()=>Current;}
    private sealed class PrivateStorage : HttpMessageHandler
    {
        public int Failures=1,Deletes; public bool Public=false;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken ct)
        {
            if(request.Method==HttpMethod.Get) return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK){Content=new StringContent(JsonSerializer.Serialize(new {@public=Public}))});
            if(request.Method==HttpMethod.Delete)
            {Deletes++;return Task.FromResult(new HttpResponseMessage(Failures-->0?HttpStatusCode.ServiceUnavailable:HttpStatusCode.OK));}
            throw new InvalidOperationException("Unexpected private-storage request.");
        }
    }
    [Fact]
    public async Task DefaultsAndPrerequisitesRejectActivation()
    {
        await using var app=new WebApplicationFactory<Program>();using var client=app.CreateClient();
        Assert.False(app.Services.GetRequiredService<IOptions<StorageCleanupOptions>>().Value.Enabled);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync("/api/storage-cleanup/preview?client_id=10&template_id=1",Ct)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.PostAsJsonAsync("/api/storage-cleanup/compact",new {},Ct)).StatusCode);
        await using var invalid=new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>s.PostConfigure<StorageCleanupOptions>(o=>o.PurgeEnabled=true)));
        Assert.Throws<OptionsValidationException>(()=>invalid.CreateClient());
    }

    [Fact]
    public async Task OwnedPurgeRecoveryBackupProtectionAndRealReupload()
    {
        await using var fixture=new PostgresFixture();await fixture.InitializeAsync();await fixture.ApplyAuditSchemaAsync(Ct);
        await using var connection=await fixture.OpenControlConnectionAsync(Ct);
        async Task Sql(string sql) {await using var command=new NpgsqlCommand(sql,connection);await command.ExecuteNonQueryAsync(Ct);}
        async Task<long> Count(string sql) {await using var command=new NpgsqlCommand(sql,connection);return Convert.ToInt64(await command.ExecuteScalarAsync(Ct));}
        await Sql("""
            CREATE TABLE public.users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_active BOOLEAN DEFAULT TRUE,role TEXT,last_login TIMESTAMPTZ);
            CREATE TABLE public.clients(id INTEGER PRIMARY KEY,client_code TEXT,client_name TEXT,is_active BOOLEAN DEFAULT TRUE,industry TEXT,contact_person TEXT,email TEXT,phone TEXT,created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE public.sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES public.users(id),expiry TIMESTAMPTZ,selected_client_id INTEGER,selected_tenant_id INTEGER,context_version BIGINT NOT NULL DEFAULT 0);
            CREATE TABLE public.master_columns(id INTEGER PRIMARY KEY,column_name TEXT,data_type TEXT,is_active BOOLEAN DEFAULT TRUE,category TEXT DEFAULT 'General',notes TEXT);
            CREATE TABLE public.templates(id INTEGER PRIMARY KEY,template_name TEXT,created_by TEXT DEFAULT 'Admin',created_date TIMESTAMPTZ DEFAULT now(),is_active BOOLEAN DEFAULT TRUE,is_system BOOLEAN DEFAULT FALSE,has_subtotals_by_default BOOLEAN DEFAULT FALSE,data_purpose TEXT DEFAULT 'standard',ledger_columns TEXT DEFAULT '{}',rate_source TEXT);
            CREATE TABLE public.template_column_mappings(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,template_id INTEGER REFERENCES public.templates(id),column_id INTEGER REFERENCES public.master_columns(id),sort_order INTEGER,reconciliation_role TEXT,compare_for_changes BOOLEAN,comparison_review_required BOOLEAN DEFAULT FALSE);
            CREATE TABLE public.uploaded_files(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,file_name TEXT,file_path TEXT,file_type TEXT,record_count INTEGER,status TEXT,uploaded_by TEXT,client_id INTEGER,template_id INTEGER,ledger_account_code TEXT,ledger_account_name TEXT);
            CREATE TABLE public.upload_validation_plans(plan_id TEXT PRIMARY KEY,file_id INTEGER REFERENCES public.uploaded_files(id),client_id INTEGER,template_id INTEGER,file_sha256 TEXT,request_signature TEXT,plan_json JSONB,status TEXT CHECK(status IN('ready','applied','expired','superseded')),created_at TIMESTAMPTZ DEFAULT now(),expires_at TIMESTAMPTZ,applied_at TIMESTAMPTZ);
            CREATE TABLE public.saved_column_mappings(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,template_column_id INTEGER,excel_column TEXT,compare_for_changes BOOLEAN);
            CREATE TABLE public.audit_rule_documents(id INTEGER PRIMARY KEY,template_id INTEGER);
            CREATE TABLE public.audit_rule_document_fields(id INTEGER PRIMARY KEY,document_id INTEGER,master_column_id INTEGER,field_name TEXT);
            CREATE TABLE public.audit_rule_cross_checks(id INTEGER PRIMARY KEY,compare_field TEXT);
            CREATE TABLE public.client_template_upload_frequency(client_id INTEGER,template_id INTEGER,frequency TEXT);
            ALTER TABLE public.client_data ADD COLUMN template_id INTEGER,ADD COLUMN extract_date DATE,ADD COLUMN extract_amount REAL,
                ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE,ADD COLUMN change_detection_status TEXT DEFAULT 'unchanged',ADD COLUMN upload_id INTEGER REFERENCES public.uploaded_files(id),
                ADD COLUMN source_order_upload_id INTEGER,ADD COLUMN source_row_number INTEGER,ADD COLUMN document_group_id TEXT,ADD COLUMN data_quality_flag TEXT,
                ADD COLUMN rate_source TEXT,ADD COLUMN version INTEGER DEFAULT 1,ADD COLUMN source_identity TEXT,ADD COLUMN source_fingerprint TEXT,
                ADD COLUMN created_at TIMESTAMPTZ DEFAULT now(),ADD COLUMN created_by TEXT,
                ADD COLUMN derived_voucher_id TEXT,ADD COLUMN derived_account_code TEXT,ADD COLUMN derived_account_name TEXT,
                ADD COLUMN derived_transaction_date DATE,ADD COLUMN derived_transaction_month TEXT,ADD COLUMN derived_transaction_fortnight TEXT,
                ADD COLUMN derived_debit DOUBLE PRECISION DEFAULT 0,ADD COLUMN derived_credit DOUBLE PRECISION DEFAULT 0,
                ADD COLUMN derived_treasury_type TEXT,ADD COLUMN derived_treasury_account_code TEXT,ADD COLUMN derived_classification_status TEXT,
                ADD COLUMN derived_version INTEGER DEFAULT 1;
            CREATE UNIQUE INDEX idx_client_data_active_identity_unique ON public.client_data(client_id,template_id,source_identity)
                WHERE is_deleted=FALSE AND change_detection_status<>'deleted' AND source_identity IS NOT NULL AND source_identity<>'';
            CREATE TABLE public.client_data_history(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id INTEGER,template_id INTEGER,client_data_id INTEGER,
                action TEXT,changed_at TIMESTAMPTZ DEFAULT now(),changed_by TEXT,source_upload_id INTEGER,replacing_upload_id INTEGER,source_identity TEXT,row_data JSONB,
                changed_fields JSONB,ledger_account_code TEXT,ledger_account_name TEXT);
            CREATE TABLE public.verification_details(id INTEGER PRIMARY KEY,client_data_id INTEGER REFERENCES public.client_data(id),validation_status TEXT);
            CREATE TABLE public.voucher_line_documents(id INTEGER PRIMARY KEY,client_data_id INTEGER REFERENCES public.client_data(id));
            CREATE TABLE public.voucher_line_physical_bill_items(id INTEGER PRIMARY KEY,source_document_row_id INTEGER REFERENCES public.client_data(id));
            CREATE TABLE public.cashflow_prepared_lines(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,source_client_data_id INTEGER REFERENCES public.client_data(id));
            CREATE TABLE public.cash_bank_transaction_classifications(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,client_data_id INTEGER REFERENCES public.client_data(id));
            CREATE TABLE public.cashflow_voucher_occurrences(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER);
            CREATE TABLE public.cashflow_prepared_rollups(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER);
            CREATE TABLE public.cash_bank_summary_read_models(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER);
            CREATE TABLE public.cashflow_ledger_rollup_read_models(id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER);
            CREATE TABLE public.upload_lifecycle_runs(operation_id TEXT PRIMARY KEY,file_id INTEGER,client_id INTEGER,template_id INTEGER);
            CREATE TABLE public.verifications(id INTEGER PRIMARY KEY,file_id INTEGER REFERENCES public.uploaded_files(id));
            CREATE TABLE public.account_control_cashflow_settings(client_id INTEGER PRIMARY KEY,source_upload_id INTEGER REFERENCES public.uploaded_files(id),settings TEXT);
            CREATE TABLE public.trial_balance_uploads(id INTEGER PRIMARY KEY,client_id INTEGER);
            CREATE TABLE public.trial_balance_accounts(id INTEGER PRIMARY KEY,upload_id INTEGER,account_code TEXT,parent_account_code TEXT);
            CREATE TABLE public.report_balance_inputs(id INTEGER PRIMARY KEY,client_id INTEGER,group_label TEXT,account_code TEXT,period_month TEXT);
            INSERT INTO public.users(id,username,password_hash,role) VALUES(1,'owner','unused','admin'),(2,'other','unused','admin'),(3,'auditor','unused','user');
            INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(20,'B','Foreign secret');
            INSERT INTO public.sessions VALUES(repeat('a',64),1,now()+interval '2 hours',10,101,0),(repeat('b',64),2,now()+interval '2 hours',20,102,0),(repeat('c',64),3,now()+interval '2 hours',10,101,0);
            INSERT INTO public.master_columns(id,column_name,data_type) VALUES(1,'col_1','Text'),(2,'col_2','Date'),(3,'col_3','Numeric'),(4,'col_4','Text'),(5,'col_5','Text'),(6,'col_6','Text');
            INSERT INTO public.templates(id,template_name) VALUES(1,'tpl_fixture'),(2,'tpl_browser'),(5,'foreign');
            INSERT INTO public.client_data(client_id,template_id,row_data) VALUES(20,5,'{"sentinel":"preserve foreign exact"}');
            INSERT INTO public.trial_balance_uploads VALUES(1,10),(2,20);INSERT INTO public.trial_balance_accounts VALUES(1,1,'001',NULL),(2,2,'001',NULL);
            INSERT INTO public.report_balance_inputs VALUES(1,10,'Assets','001','2026-04');
            """);
        await fixture.ApplyTenantFoundationAsync(Ct);await fixture.ApplyEngagementFoundationAsync(Ct);await fixture.ApplyTenantUserRolesAsync(Ct);
        await fixture.ApplyTenantColumnsAsync(Ct);await fixture.ApplyTenantTemplatesAsync(Ct);await fixture.ApplySavedMappingsAsync(Ct);await fixture.ApplyTenantSettingsAsync(Ct);
        await Sql("""
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
            INSERT INTO public.platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
            INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Document','native'),(2,101,'Date','native'),(3,101,'Amount','native'),(4,101,'Memo','native'),(5,101,'Balance','native'),(6,101,'Extra','native');
            INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) VALUES(1,101,'Fixture','native'),(2,101,'Browser scope','native'),(5,102,'Foreign secret','native');
            INSERT INTO public.template_column_mappings(template_id,column_id,sort_order,reconciliation_role,tenant_id,storage_key,compare_for_changes)
                SELECT t,c,c-1,CASE WHEN c=1 THEN 'document_id' WHEN c=2 THEN 'document_date' WHEN c=3 THEN 'footing' END,101,'col_'||c,c<>5 FROM generate_series(1,2) t CROSS JOIN generate_series(1,6) c ORDER BY t,c;
            INSERT INTO public.platform_audit_engagements(id,firm_tenant_id,client_id,name) OVERRIDING SYSTEM VALUE VALUES(201,101,10,'Audit');
            INSERT INTO public.platform_engagement_assignments(engagement_id,firm_tenant_id,user_id,team_role) VALUES(201,101,3,'internee');
            """);
        await fixture.ApplyUploadStagingAsync(Ct);await fixture.ApplyUploadValidationPlansAsync(Ct);await fixture.ApplyUploadImportsAsync(Ct);
        await fixture.ApplyUploadDerivedFieldsAsync(Ct);await fixture.ApplyUploadImportReceiptsAsync(Ct);await fixture.ApplyUploadJobsAsync(Ct);
        await using(var migration=await connection.BeginTransactionAsync(Ct))
        {
            await using var command=new NpgsqlCommand(await File.ReadAllTextAsync(Path.Combine(Root(),"database","migrations","0017_storage_cleanup.sql"),Ct),connection,migration);
            await command.ExecuteNonQueryAsync(Ct);await migration.CommitAsync(Ct);
        }
        // Actual required structures, not a mocked Passed result.
        for(int i=0;i<IntegrityBaseline.Requirements.Length;i++)
        {
            var r=IntegrityBaseline.Requirements[i];if(r.Kind=="primary"||i==1) continue;
            await Sql($"CREATE {(r.Kind=="unique"?"UNIQUE ":"")}INDEX p33_required_{i} ON public.{r.Table}({string.Join(',',r.Columns)})");
        }
        string stagingRoot=Path.Combine(Path.GetTempPath(),"AuditPlatform-P33-"+Guid.NewGuid().ToString("N"));
        var clock=new Clock();await using var app=Host(fixture,stagingRoot,clock);app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
        string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
        var sessions=app.Services.GetRequiredService<PostgresSessionStore>();var owner=(await sessions.FindAsync(new string('a',64),Ct))!;
        var staff=(await sessions.FindAsync(new string('c',64),Ct))!;var other=(await sessions.FindAsync(new string('b',64),Ct))!;
        var templateService=app.Services.GetRequiredService<TenantTemplateService>();
        foreach(int id in new[]{1,2})
        {
            var templateState=(await templateService.GetAsync(owner,id,Ct)).Templates!.Single();
            var input=new TemplateInput(templateState.Name,templateState.Columns.Select(c=>c.Id).ToArray(),templateState.Purpose,templateState.Ledger,
                templateState.Columns.ToDictionary(c=>c.Id,c=>c.Role),templateState.Columns.ToDictionary(c=>c.Id,c=>c.Compare??true),templateState.RateSource,templateState.Subtotals,
                templateState.ConfigurationKey,templateState.Columns.ToDictionary(c=>c.Id,c=>c.Version),new Dictionary<string,int>{{"amount",3},{"description",4}});
            Assert.Equal(TemplateStatus.Success,(await templateService.SaveAsync(owner,id,input,Ct)).Status);
        }
        var evidence=app.Services.GetRequiredService<StorageCleanupService>();var purges=app.Services.GetRequiredService<StoragePurgeService>();
        var exports=app.Services.GetRequiredService<DataBankExportService>();var bounds=app.Services.GetRequiredService<IOptions<StorageCleanupOptions>>().Value;
        var staging=app.Services.GetRequiredService<UploadStagingService>();var imports=app.Services.GetRequiredService<UploadImportService>();
        var preparation=app.Services.GetRequiredService<UploadPreparationService>();var plans=app.Services.GetRequiredService<UploadValidationPlanService>();
        async Task<(int File,UploadImportRequest Request,UploadImportResult Result)> Import(string sheet="Base",int template=1)
        {
            await using var file=File.OpenRead(Path.Combine(golden.DirectoryPath,"plans.xlsx"));
            var staged=await staging.ReceiveAsync(owner,10,template,Guid.NewGuid(),"synthetic.xlsx",file,Ct);Assert.True(staged.Status=="success",staged.Code);
            int id=staged.FileId!.Value;
            // Preparation binds permanent column IDs, not mapping-row IDs.
            var source=new PreparationRequest(10,template,sheet,RequestId:Guid.NewGuid(),Mappings:[new(1,1),new(2,2),new(3,3),new(4,4),new(5,5),new(6,6)]);
            var prepared=await preparation.PrepareAsync(owner,id,source,Ct);Assert.True(prepared.Status=="success",prepared.Code);
            source=source with {Sha256=prepared.Output!.Context.Sha256,ConfigurationKey=prepared.Output.Context.ConfigurationKey,MappingRevision=prepared.Output.Context.MappingRevision};
            var identity=new IdentityRequest(new(new(new(source,"replace_range",5),true),"2026-04-01","2026-04-30",true));
            var review=new CorrectionRequest(identity.Coverage);
            var plan=await plans.CreateAsync(owner,id,new(Guid.NewGuid(),review),Ct);Assert.True(plan.Status=="success",plan.Code);Assert.Equal("ready",plan.Output!.Status);
            var request=new UploadImportRequest(Guid.NewGuid(),plan.Output.PlanId!,plan.Output.Signature!,review);
            var result=await imports.ImportAsync(owner,id,request,Ct);Assert.True(result.Status=="success",result.Code);
            return(id,request,result);
        }
        await Import();var imported=await Import("Changed");var browserImport=await Import("Base",2);
        string browserSourceKey=await Scalar(connection,$"SELECT storage_key::text FROM public.platform_upload_staging WHERE upload_id={browserImport.File}");
        string browserSourcePath=Path.Combine(stagingRoot,Guid.Parse(browserSourceKey).ToString("N")+".bin");
        byte[] browserSourceBytes=await File.ReadAllBytesAsync(browserSourcePath,Ct);
        Assert.Equal(3L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND template_id=1"));
        Assert.True(await Count("SELECT count(*) FROM public.client_data_history WHERE client_id=10")>0);
        await Sql($"INSERT INTO public.account_control_cashflow_settings VALUES(10,{imported.File},'preserve settings exactly'),(20,NULL,'preserve foreign settings')");
        async Task<CleanupPreview> Preview(string kind="purge",int template=1)
        {var result=await evidence.PreviewAsync(owner,10,template,kind,Ct);Assert.True(result.Status=="success",result.Code);return Assert.IsType<CleanupPreview>(result.Value);}
        async Task<(CleanupPreview Preview,DataBankDownload Backup,CleanupRequest Request)> Ready()
        {
            var preview=await Preview();Assert.True(preview.CanApply,string.Join(',',preview.Blockers));
            var backup=await exports.DownloadAsync(owner,new("backup",10,1),true,Ct,forPurge:true);Assert.True(backup.Status=="success",backup.Code);Assert.NotNull(backup.PurgeProof);
            return(preview,backup,new(Guid.NewGuid(),preview.PreviewToken,StoragePurgeService.PurgeConfirmation,backup.PurgeProof));
        }
        var directory=Path.Combine(Root(),".tools","p33-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(directory);
        string original=await Source(connection),preserved=await Preserved(connection),indexes=await Indexes(connection);
        var clean=await Preview();Assert.True(clean.CanApply,string.Join(',',clean.Blockers));
        Assert.Equal(original,await Source(connection));Assert.Equal(preserved,await Preserved(connection));
        Assert.Equal("forbidden",(await evidence.PreviewAsync(staff,10,1,"purge",Ct)).Status);
        Assert.Equal("forbidden",(await evidence.PreviewAsync(other,10,1,"purge",Ct)).Status);
        Assert.Equal("invalid",(await evidence.PreviewAsync(owner,10,0,"purge",Ct)).Status);
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("DELETE FROM public.platform_upload_staging"));
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("DELETE FROM public.platform_upload_validation_plans"));
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("UPDATE public.platform_upload_import_receipts SET receipt=receipt"));
        var ready=await Ready();
        // Revoked permission, cancellation and saturation cannot write a run.
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='member' WHERE tenant_id=101 AND user_id=1");
        Assert.Equal("forbidden",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct)).Status);
        await Sql("UPDATE public.platform_tenant_memberships SET tenant_role='admin' WHERE tenant_id=101 AND user_id=1");
        await using(var held=await connection.BeginTransactionAsync(Ct))
        {
            await using(var gate=new NpgsqlCommand("SELECT pg_advisory_xact_lock(746102)",connection,held)) await gate.ExecuteNonQueryAsync(Ct);
            using var cancellation=new CancellationTokenSource();
            var waiting=purges.ApplyAsync(owner,10,1,"purge",ready.Request,cancellation.Token);
            // Observe the actual blocked database command before testing the second caller.
            using var observation=new CancellationTokenSource(TimeSpan.FromSeconds(5));
            while(await Count("SELECT count(*) FROM pg_stat_activity WHERE wait_event='advisory' AND pid<>pg_backend_pid()") == 0)
                await Task.Delay(10,observation.Token);
            Assert.Equal("busy",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct)).Status);
            cancellation.Cancel();await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>waiting);
            await held.RollbackAsync(Ct);
        }
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_storage_cleanup_runs"));
        Assert.Equal("invalid",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request with {Confirmation="DELETE"},Ct)).Status);
        Assert.Equal("conflict",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request with {BackupToken=null},Ct)).Status);
        Assert.Equal("conflict",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request with {PreviewToken=ready.Request.PreviewToken+"x"},Ct)).Status);
        clock.Current=clock.Current.AddMinutes(6);
        Assert.Equal("conflict",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct)).Status);clock.Current=DateTimeOffset.UtcNow;
        await Sql("UPDATE public.client_data_history SET changed_by='changed-with-same-count' WHERE id=(SELECT min(id) FROM public.client_data_history)");
        Assert.Equal("conflict",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct)).Status);
        await Sql("UPDATE public.client_data_history SET changed_by='owner' WHERE id=(SELECT min(id) FROM public.client_data_history)");
        ready=await Ready();
        await Sql("INSERT INTO public.verification_details SELECT 1,min(id),'Pending' FROM public.client_data WHERE client_id=10");
        Assert.False((await Preview()).CanApply);Assert.Equal("conflict",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct)).Status);
        await Sql("DELETE FROM public.verification_details");
        await Sql("CREATE TABLE public.unreviewed_evidence(id INTEGER PRIMARY KEY,source_id INTEGER REFERENCES public.client_data(id) ON DELETE CASCADE); INSERT INTO public.unreviewed_evidence SELECT 1,min(id) FROM public.client_data WHERE client_id=10");
        Assert.False((await Preview()).CanApply);await Sql("DROP TABLE public.unreviewed_evidence");
        await Sql("ALTER TABLE public.voucher_line_documents RENAME TO hidden_documents");Assert.Equal("review",(await evidence.PreviewAsync(owner,10,1,"purge",Ct)).Status);
        await Sql("ALTER TABLE public.hidden_documents RENAME TO voucher_line_documents");
        // A real failure after source DELETEs must roll back all rows, metadata and settings.
        ready=await Ready();string before=await Source(connection),beforeSettings=await Settings(connection);
        await Sql("ALTER TABLE public.platform_storage_cleanup_artifacts ADD CONSTRAINT p33_injected_failure CHECK(kind<>'local')");
        await Assert.ThrowsAsync<PostgresException>(()=>purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct));
        Assert.Equal(before,await Source(connection));Assert.Equal(beforeSettings,await Settings(connection));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_storage_cleanup_runs"));
        await Sql("ALTER TABLE public.platform_storage_cleanup_artifacts DROP CONSTRAINT p33_injected_failure");

        // Active jobs block both source purge and temporary-plan cleanup.
        var privateOptions=Options.Create(new UploadJobOptions{Enabled=true,StorageUrl="https://synthetic.supabase.co",StorageBucket="jobs",StorageKey="synthetic-secret"});
        Guid objectId=Guid.NewGuid();string location=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(privateOptions.Value.StorageUrl.TrimEnd('/')+"\n"+privateOptions.Value.StorageBucket))).ToLowerInvariant();
        await Sql($"""
            INSERT INTO public.platform_upload_jobs(job_id,tenant_id,user_id,client_id,template_id,file_id,kind,input_hash,request_text,session_hash,context_version,source_hash,storage_hash,source_bytes,source_expires_at,expires_at,state,lease_version)
            VALUES('{objectId}',101,1,10,1,{imported.File},'no_changes',repeat('1',64),json_build_object()::text,repeat('2',64),0,repeat('3',64),'{location}',100,now()+interval '1 hour',now()+interval '30 minutes','queued',1)
            """);
        Assert.False((await Preview()).CanApply);Assert.False((await Preview("plans")).CanApply);
        await Sql($"UPDATE public.platform_upload_jobs SET state='failed' WHERE job_id='{objectId}'");
        Assert.False((await Preview()).CanApply);
        await Sql($"UPDATE public.platform_upload_jobs SET updated_at=now()-interval '10 minutes' WHERE job_id='{objectId}'");
        await File.WriteAllTextAsync(Path.Combine(stagingRoot,objectId.ToString("N")+".1.restore"),"synthetic fragment",Ct);
        ready=await Ready();
        await File.WriteAllBytesAsync(Path.Combine(directory,"backup.xlsx"),ready.Backup.Bytes!,Ct);
        await File.WriteAllTextAsync(Path.Combine(directory,"before.json"),JsonSerializer.Serialize(ready.Preview,CleanupJson.Options),Ct);
        await File.WriteAllTextAsync(Path.Combine(directory,"source-before.json"),await SourceRows(connection),Ct);
        // Drop the real server's COMMIT acknowledgement, then recover the durable result.
        await using var proxy=fixture.CreateCommitProxy();using var proxyPool=fixture.CreatePool(proxy:proxy);
        var uncertain=new StoragePurgeService(new PostgresTransactions(proxyPool,Options.Create(new PostgresOptions())),app.Services.GetRequiredService<PostgresReadSnapshot>(),evidence,
            app.Services.GetRequiredService<UploadValidationPlanStore>(),Options.Create(bounds),app.Services.GetRequiredService<IConfiguration>());
        proxy.Arm();var applied=await uncertain.ApplyAsync(owner,10,1,"purge",ready.Request,Ct);Assert.True(applied.Status=="success",applied.Code);
        await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Ct);
        var outcome=Assert.IsType<CleanupOperation>(applied.Value);Assert.Equal("pending",outcome.ArtifactStatus);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND template_id=1"));Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history WHERE client_id=10 AND template_id=1"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.uploaded_files WHERE client_id=10 AND template_id=1"));Assert.Equal(preserved,await Preserved(connection));Assert.Equal(indexes,await Indexes(connection));
        Assert.Equal("preserve settings exactly",await Scalar(connection,"SELECT settings FROM public.account_control_cashflow_settings WHERE client_id=10"));
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.account_control_cashflow_settings WHERE client_id=10 AND source_upload_id IS NOT NULL"));
        Assert.True(Directory.GetFiles(stagingRoot,"*.bin").Length>0); // Database commit precedes file work.
        Assert.Equal("success",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct)).Status);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_storage_cleanup_runs"));
        Assert.Equal("conflict",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request with {BackupToken=ready.Request.BackupToken+"x"},Ct)).Status);
        Assert.Equal("invalid",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request with {Confirmation="DELETE"},Ct)).Status);
        // A new worker instance resumes leased artifact work, with one transient storage failure.
        var storage=new PrivateStorage();bounds.ArtifactWorkerEnabled=true;
        var worker=new StorageArtifactCleanupService(app.Services.GetRequiredService<PostgresTransactions>(),staging,new UploadJobSourceStore(privateOptions,new HttpClient(storage)),Options.Create(bounds));
        for(int i=0;i<12&&storage.Deletes==0;i++) Assert.True(await worker.ProcessOnceAsync(Ct));
        Assert.Equal(1,storage.Deletes);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_storage_cleanup_artifacts WHERE kind='object' AND state='pending' AND attempts=1"));
        // Persist an expired lease as if the process exited after its next claim.
        await Sql("UPDATE public.platform_storage_cleanup_artifacts SET state='working',worker_id=gen_random_uuid(),lease_until=now()-interval '1 second',lease_version=lease_version+1 WHERE kind='object' AND state='pending'");
        var restarted=new StorageArtifactCleanupService(app.Services.GetRequiredService<PostgresTransactions>(),new UploadStagingService(app.Services.GetRequiredService<PostgresTransactions>(),app.Services.GetRequiredService<AuditPlatform.Api.Logging.PostgresAuditWriter>(),app.Services.GetRequiredService<IOptions<UploadStagingOptions>>()),new UploadJobSourceStore(privateOptions,new HttpClient(storage)),Options.Create(bounds));
        for(int i=0;i<8;i++) if(!await restarted.ProcessOnceAsync(Ct)) break;
        Assert.Equal(2,storage.Deletes);Assert.Single(Directory.GetFiles(stagingRoot,"*.bin"));Assert.Empty(Directory.GetFiles(stagingRoot,"*.restore"));
        Assert.Equal(browserSourceBytes,await File.ReadAllBytesAsync(browserSourcePath,Ct));
        Assert.Equal("complete",Assert.IsType<CleanupOperation>((await purges.StatusAsync(owner,10,1,ready.Request.OperationId,Ct)).Value).ArtifactStatus);
        bounds.ArtifactWorkerEnabled=false;
        var replay=await imports.ImportAsync(owner,imported.File,imported.Request,Ct);Assert.NotEqual("success",replay.Status);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND template_id=1"));
        // Actual native import after purge: same workbook values/multiplicity, once.
        var reloaded=await Import("Changed");string current=await Content(connection);
        Assert.Equal(3L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND template_id=1"));
        Assert.Equal("success",(await imports.ImportAsync(owner,reloaded.File,reloaded.Request,Ct)).Status);Assert.Equal(current,await Content(connection));
        Assert.Equal("success",(await purges.ApplyAsync(owner,10,1,"purge",ready.Request,Ct)).Status);Assert.Equal(current,await Content(connection));
        Assert.Equal(preserved,await Preserved(connection));Assert.Equal(indexes,await Indexes(connection));
        Assert.Equal("Passed",(await app.Services.GetRequiredService<DataIntegrityService>().ReadAsync(owner,10,Ct)).Value!.Safeguards.Status);
        await File.WriteAllTextAsync(Path.Combine(directory,"source-after-reupload.json"),await SourceRows(connection),Ct);
        await File.WriteAllTextAsync(Path.Combine(directory,"outcome.json"),JsonSerializer.Serialize(outcome,CleanupJson.Options),Ct);
        // Temporary-plan cleanup retains recent compact retry envelopes and current data.
        await Sql($"INSERT INTO public.upload_validation_plans(plan_id,file_id,client_id,template_id,status,expires_at) VALUES(repeat('f',32),{reloaded.File},10,1,'expired',now()-interval '1 minute')");
        var planPreview=await Preview("plans");var planRequest=new CleanupRequest(Guid.NewGuid(),planPreview.PreviewToken,StoragePurgeService.PlanConfirmation);
        Assert.Equal("success",(await purges.ApplyAsync(owner,10,1,"plans",planRequest,Ct)).Status);Assert.Equal(current,await Content(connection));
        Assert.True(await Count("SELECT count(*) FROM public.platform_upload_validation_plans")>0);
        // Limits and HTTP/session/context/CSRF gates.
        bounds.MaximumRows=1;Assert.Equal("too_large",(await evidence.PreviewAsync(owner,10,1,"purge",Ct)).Status);bounds.MaximumRows=100000;
        using var http=new HttpClient{BaseAddress=new Uri(address)};
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync("/api/storage-cleanup/preview?client_id=10&template_id=1",Ct)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+new string('a',64));
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync("/api/storage-cleanup/preview?client_id=10&template_id=1",Ct)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
        await Sql("INSERT INTO public.templates(id,template_name) SELECT i,'Large scope '||i FROM generate_series(100,119) i; INSERT INTO public.platform_tenant_templates(template_id,tenant_id,display_name,binding_origin) SELECT i,101,repeat('界',196)||lpad(i::text,4,'0'),'native' FROM generate_series(100,119) i");
        bounds.MaximumResponseBytes=4096;
        Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await http.GetAsync("/api/storage-cleanup/scopes?client_id=10",Ct)).StatusCode);
        bounds.MaximumResponseBytes=131072;await Sql("UPDATE public.templates SET is_active=FALSE WHERE id BETWEEN 100 AND 119");
        Assert.Equal(HttpStatusCode.Forbidden,(await http.PostAsJsonAsync("/api/storage-cleanup/apply?client_id=10&template_id=1&kind=plans",planRequest,Ct)).StatusCode);
        http.DefaultRequestHeaders.Add("Origin",address);
        Assert.Equal(HttpStatusCode.OK,(await http.PostAsJsonAsync("/api/storage-cleanup/apply?client_id=10&template_id=1&kind=plans",planRequest,Ct)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.PostAsJsonAsync("/api/storage-cleanup/index-maintenance",new {},Ct)).StatusCode);
        // Browser owns a separate imported scope, leaving re-upload evidence intact.
        await Sql($"INSERT INTO public.upload_validation_plans(plan_id,file_id,client_id,template_id,status,expires_at) VALUES(repeat('e',32),{browserImport.File},10,2,'expired',now()-interval '1 minute')");
        var pg=new NpgsqlConnectionStringBuilder(fixture.ConnectionString);
        await File.WriteAllTextAsync(Path.Combine(directory,"fixture.json"),JsonSerializer.Serialize(new {address,port=fixture.Port,database=pg.Database,user=pg.Username,password=pg.Password,context=TenantSessionService.ContextKey(owner)}),Ct);
        await Python("p33_cleanup_oracle.py",directory);await Python("p33_cleanup_browser.py",directory);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=10 AND template_id=2"));
        bounds.Enabled=false;bounds.PurgeEnabled=false;await Python("p33_cleanup_browser.py",directory,"disabled");
        // A provider that becomes public cannot receive deletes. Three failed
        // attempts become durable review, including across worker instances.
        bounds.Enabled=true;bounds.PurgeEnabled=true;
        Guid refused=Guid.NewGuid();
        await Sql($"""
            INSERT INTO public.platform_upload_jobs(job_id,tenant_id,user_id,client_id,template_id,file_id,kind,input_hash,request_text,session_hash,context_version,source_hash,storage_hash,source_bytes,source_expires_at,expires_at,state)
            VALUES('{refused}',101,1,10,1,{reloaded.File},'no_changes',repeat('1',64),json_build_object()::text,repeat('2',64),0,repeat('3',64),'{location}',100,now()+interval '1 hour',now()+interval '30 minutes','failed');
            UPDATE public.platform_upload_jobs SET updated_at=now()-interval '10 minutes' WHERE job_id='{refused}';
            """);
        var refusedReady=await Ready();Assert.Equal("success",(await purges.ApplyAsync(owner,10,1,"purge",refusedReady.Request,Ct)).Status);
        bounds.ArtifactWorkerEnabled=true;storage.Public=true;int priorDeletes=storage.Deletes;
        for(int i=0;i<8;i++) if(!await restarted.ProcessOnceAsync(Ct)) break;
        Assert.Equal(priorDeletes,storage.Deletes);
        Assert.Equal(1L,await Count($"SELECT count(*) FROM public.platform_storage_cleanup_artifacts WHERE source_key='{refused}' AND state='review' AND attempts=3"));
        Assert.False(await worker.ProcessOnceAsync(Ct));
        Assert.Equal("review_required",Assert.IsType<CleanupOperation>((await purges.StatusAsync(owner,10,1,refusedReady.Request.OperationId,Ct)).Value).ArtifactStatus);
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("UPDATE public.platform_storage_cleanup_artifacts SET source_key=gen_random_uuid()"));
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("UPDATE public.platform_storage_cleanup_artifacts SET attempts=4"));
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("UPDATE public.platform_storage_cleanup_runs SET deleted_counts='{}'"));
        await File.WriteAllTextAsync(Path.Combine(directory,"retry-review.json"),JsonSerializer.Serialize(new {passed=true,attempts=3,privateDeletes=storage.Deletes-priorDeletes}),Ct);
    }

    private static string Root(){DirectoryInfo? d=new(AppContext.BaseDirectory);while(d is not null&&!File.Exists(Path.Combine(d.FullName,"AuditPlatform.slnx"))) d=d.Parent;return d!.FullName;}
    private static async Task<string> Scalar(NpgsqlConnection connection,string sql)
    {await using var command=new NpgsqlCommand(sql,connection);return (string)(await command.ExecuteScalarAsync(Ct))!;}
    private static Task<string> Content(NpgsqlConnection c)=>Scalar(c,"SELECT COALESCE(string_agg(row_data::text,E'\\n' ORDER BY source_row_number,id),'') FROM public.client_data WHERE client_id=10 AND template_id=1");
    private static Task<string> SourceRows(NpgsqlConnection c)=>Scalar(c,"SELECT COALESCE(jsonb_agg(row_data ORDER BY source_row_number,id),'[]'::jsonb)::text FROM public.client_data WHERE client_id=10 AND template_id=1");
    private static async Task<string> Source(NpgsqlConnection c)
    {
        var values=new List<string>();foreach(string table in new[]{"client_data","client_data_history","uploaded_files","upload_validation_plans","platform_upload_staging","platform_upload_validation_plans","platform_upload_imports","platform_upload_import_receipts","platform_upload_jobs"})
            values.Add(await Scalar(c,$"SELECT md5(COALESCE(string_agg(t::text,E'\\n' ORDER BY t::text),'')) FROM public.{table} t"));return string.Join('/',values);
    }
    private static Task<string> Settings(NpgsqlConnection c)=>Scalar(c,"SELECT jsonb_agg(to_jsonb(t) ORDER BY client_id)::text FROM public.account_control_cashflow_settings t");
    private static async Task<string> Preserved(NpgsqlConnection c)
    {
        var values=new List<string>{await Scalar(c,"SELECT jsonb_agg(to_jsonb(t) ORDER BY id)::text FROM public.client_data t WHERE client_id<>10 OR template_id<>1")};
        foreach(string table in new[]{"client_data_history","uploaded_files","upload_validation_plans","platform_upload_staging","platform_upload_validation_plans","platform_upload_imports","platform_upload_import_receipts","platform_upload_jobs"})
            values.Add(await Scalar(c,$"SELECT md5(COALESCE(string_agg(t::text,E'\\n' ORDER BY t::text),'')) FROM public.{table} t WHERE client_id<>10 OR template_id<>1"));
        foreach(string table in new[]{"templates","template_column_mappings","master_columns","trial_balance_uploads","trial_balance_accounts","report_balance_inputs","platform_tenant_columns","platform_tenant_templates"})
            values.Add(await Scalar(c,$"SELECT md5(COALESCE(string_agg(t::text,E'\\n' ORDER BY t::text),'')) FROM public.{table} t"));return string.Join('/',values);
    }
    private static Task<string> Indexes(NpgsqlConnection c)=>Scalar(c,"SELECT string_agg(indexdef,E'\\n' ORDER BY indexname) FROM pg_indexes WHERE schemaname='public'");
    private static WebApplicationFactory<Program> Host(PostgresFixture f,string root,Clock clock)=>new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>
    {
        s.AddSingleton<TimeProvider>(clock);
        s.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=f.ConnectionString;});s.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
        s.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);s.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);s.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
        s.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);s.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);s.PostConfigure<SettingsManagementOptions>(o=>o.Enabled=true);s.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
        s.PostConfigure<DataBankOptions>(o=>{o.Enabled=true;o.HistoryEnabled=true;});s.PostConfigure<DataBankExportOptions>(o=>o.Enabled=true);s.PostConfigure<DataIntegrityOptions>(o=>o.Enabled=true);
        s.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=root;});s.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);s.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);
        s.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true);s.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true);s.PostConfigure<UploadIdentityOptions>(o=>o.Enabled=true);s.PostConfigure<UploadCorrectionOptions>(o=>o.Enabled=true);
        s.PostConfigure<UploadValidationPlanOptions>(o=>{o.Enabled=true;o.SigningKey=Convert.ToBase64String(new byte[32]);});s.PostConfigure<UploadImportOptions>(o=>o.Enabled=true);
        s.PostConfigure<StorageCleanupOptions>(o=>{o.Enabled=true;o.PurgeEnabled=true;o.SigningKey=Convert.ToBase64String(Enumerable.Repeat((byte)33,32).ToArray());});
    }));
    private static async Task Python(string script,string directory,string? mode=null)
    {
        var start=new ProcessStartInfo(Path.Combine(Root(),"..","venv","Scripts","python.exe")){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(Root(),"tests",script));start.ArgumentList.Add(directory);if(mode is not null) start.ArgumentList.Add(mode);
        using var p=Process.Start(start)!;var output=p.StandardOutput.ReadToEndAsync();var errors=p.StandardError.ReadToEndAsync();
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(Ct);timeout.CancelAfter(TimeSpan.FromMinutes(4));
        try{await p.WaitForExitAsync(timeout.Token);}catch{p.Kill(entireProcessTree:true);throw;}Assert.True(p.ExitCode==0,await output+await errors);
    }
}
