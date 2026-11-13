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
using System.Collections.Concurrent;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;

namespace AuditPlatform.Api.Tests;

public sealed partial class UploadJobTests(ImportGoldenFixture golden) : IClassFixture<ImportGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private sealed class Clock : TimeProvider
    {public DateTimeOffset Current=DateTimeOffset.UtcNow;public Action? OnRead;public override DateTimeOffset GetUtcNow() {OnRead?.Invoke();return Current;}}
    private static UploadValidationPlanOptions Settings()=>new(){Enabled=true,SigningKey=Convert.ToBase64String(Enumerable.Range(0,32).Select(i=>(byte)i).ToArray())};
    private static IdentityRequest Request(string sheet="Base",string mode="replace_range")=>
        new(new(new(new(new(10,1,sheet,"rows",RequestId:Guid.NewGuid()),mode,5),true),"2026-04-01","2026-04-30",true));
    [Fact]
    public async Task DurableOwnershipRecoveryAndCancellation()
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
        using var pool=fixture.CreatePool();var tx=new PostgresTransactions(pool,Options.Create(new PostgresOptions()));
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
        async Task<IdentityRequest> Bound(string sheet="Base",bool manual=false,bool all=false)
        {
            var source=new PreparationRequest(10,1,sheet,RequestId:Guid.NewGuid(),Mappings:manual?
                [new(1,1),new(2,2),new(3,3),new(4,4),new(5,5),new(6,all?6:null)]:null);
            var prepared=(await preparation.PrepareAsync(owner,id,source,Token)).Output!;
            source=source with {Sha256=prepared.Context.Sha256,ConfigurationKey=prepared.Context.ConfigurationKey,MappingRevision=prepared.Context.MappingRevision};
            return Request(sheet) with {Coverage=Request(sheet).Coverage with {Footing=new(new(source,"replace_range",5),true)}};
        }


        await fixture.ApplyUploadValidationPlansAsync(Token);
        await Sql("""
            ALTER TABLE public.client_data ADD COLUMN created_at TIMESTAMPTZ DEFAULT '2026-01-01';
            ALTER TABLE public.client_data ADD COLUMN created_by TEXT;
            ALTER TABLE public.client_data ADD COLUMN source_fingerprint TEXT;
            ALTER TABLE public.client_data ADD COLUMN extract_amount REAL;
            ALTER TABLE public.client_data ADD COLUMN derived_account_code TEXT;
            CREATE UNIQUE INDEX idx_client_data_active_identity_unique ON public.client_data(client_id,template_id,source_identity)
                WHERE is_deleted=FALSE AND change_detection_status<>'deleted' AND source_identity IS NOT NULL AND source_identity<>'';
            SELECT setval(pg_get_serial_sequence('public.client_data','id'),1000);
            CREATE TABLE public.client_data_history(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id INTEGER,template_id INTEGER,client_data_id INTEGER,
                action TEXT,changed_at TIMESTAMPTZ DEFAULT now(),changed_by TEXT,source_upload_id INTEGER,replacing_upload_id INTEGER,source_identity TEXT,row_data JSONB,changed_fields JSONB);
            CREATE TABLE public.verification_details(id INTEGER PRIMARY KEY,client_data_id INTEGER,validation_status TEXT);
            CREATE TABLE public.voucher_line_documents(client_data_id INTEGER);
            CREATE TABLE public.voucher_line_physical_bill_items(source_document_row_id INTEGER);
            CREATE TABLE public.cashflow_prepared_lines(source_client_data_id INTEGER);
            """);
        await fixture.ApplyUploadImportsAsync(Token);await fixture.ApplyUploadImportReceiptsAsync(Token);
        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadImportsAsync(Token));
        var corrections=new UploadCorrectionService(identity,Options.Create(new UploadCorrectionOptions{Enabled=true}));
        var settings=Options.Create(Settings());var signer=new UploadValidationPlanSigner(settings);
        var store=new UploadValidationPlanStore(tx,settings,clock);var plans=new UploadValidationPlanService(corrections,store,signer,settings,clock);
        var importOptions=Options.Create(new UploadImportOptions{Enabled=true,BatchSize=1});var writer=new UploadImportWriter(importOptions);
        UploadImportService Importer(PostgresTransactions? transaction=null)=>new(transaction??tx,store,signer,staging,writer,audit,importOptions,clock);
        var imports=Importer();
        await fixture.ApplyUploadJobsAsync(Token);

        await Assert.ThrowsAsync<PostgresException>(()=>fixture.ApplyUploadJobsAsync(Token));
        await using var storage=await StorageServer.Start(Token);
        var jobOptions=Options.Create(new UploadJobOptions{Enabled=true,StorageUrl=storage.Url,StorageBucket="jobs",StorageKey="synthetic-secret"});
        var config=new ConfigurationBuilder().AddInMemoryCollection().Build();
        var jobStore=new UploadJobStore(tx,store,signer,sessions,audit,jobOptions,config);
        var receiptService=new UploadImportReceiptService(tx,store,corrections,staging,importOptions,clock);
        UploadJobService Jobs(UploadImportService? importer=null)=>new(jobStore,
            new UploadStagingService(tx,audit,Options.Create(new UploadStagingOptions{Enabled=true,RootDirectory=directory}),clock),
            new UploadJobSourceStore(jobOptions,new HttpClient(new HttpClientHandler{AllowAutoRedirect=false}){Timeout=Timeout.InfiniteTimeSpan}),
            importer??imports,receiptService,jobOptions,NullLogger<UploadJobService>.Instance);
        var jobs=Jobs();
        async Task<UploadJobRequest> Input(string kind="import",string sheet="Changed")
        {
            var bound=await Bound(sheet);var review=new CorrectionRequest(bound.Coverage);
            if(kind=="no_changes") return new(Guid.NewGuid(),kind,review);
            var p=await plans.CreateAsync(owner,id,new(Guid.NewGuid(),review),Token);Assert.True(p.Status=="success",p.Code);Assert.Equal("ready",p.Output!.Status);
            return new(Guid.NewGuid(),kind,review,p.Output.PlanId!,p.Output.Signature!);
        }
        async Task<UploadJobOutput> Submit(UploadJobRequest input)
        {var r=await jobs.SubmitAsync(owner,id,input,Token);Assert.True(r.Status=="success",r.Code);return r.Job!;}
        async Task<UploadJobOutput> Status(Guid job)
        {var r=await jobs.GetAsync(owner,id,job,Token);Assert.True(r.Status=="success",r.Code);return r.Job!;}
        async Task<string> State()
        {await using var c=new NpgsqlCommand("SELECT jsonb_agg(to_jsonb(c) ORDER BY id)::text FROM public.client_data c",connection);return (string)(await c.ExecuteScalarAsync(Token))!;}
        string original=await State();string privatePath=Directory.GetFiles(directory,"*.bin").Single();byte[] originalBytes=await File.ReadAllBytesAsync(privatePath,Token);
        async Task Reset()
        {
            storage.AfterRead=null;storage.Failures=0;storage.Public=false;storage.AbortUpload=false;
            await Sql("DELETE FROM public.platform_upload_jobs;DELETE FROM public.client_data_history;DELETE FROM public.platform_upload_imports;DELETE FROM public.client_data WHERE client_id=10");
            await using(var c=new NpgsqlCommand("INSERT INTO public.client_data OVERRIDING SYSTEM VALUE SELECT * FROM jsonb_populate_recordset(NULL::public.client_data,$1::jsonb) WHERE client_id=10",connection))
            {c.Parameters.AddWithValue(original);await c.ExecuteNonQueryAsync(Token);}
            await Sql($"UPDATE public.uploaded_files SET status='Pending' WHERE id={id};UPDATE public.platform_tenant_memberships SET is_active=TRUE;UPDATE public.sessions SET selected_client_id=10 WHERE user_id=1");
            await File.WriteAllBytesAsync(privatePath,originalBytes,Token);
        }
        async Task Wait(Func<Task<bool>> condition)
        {
            using var wait=CancellationTokenSource.CreateLinkedTokenSource(Token);wait.CancelAfter(TimeSpan.FromSeconds(15));
            while(!await condition()) await Task.Delay(30,wait.Token);
        }
        // Immutable request ownership, same-plan convergence and early cancellation.
        var input=await Input();var submitted=await Submit(input);
        Assert.Equal(submitted.Id,(await Submit(input)).Id);
        Assert.Equal(submitted.Id,(await Submit(input with {RequestId=Guid.NewGuid()})).Id);
        Assert.Equal("conflict",(await jobs.SubmitAsync(owner,id,input with {Signature=new string('0',64)},Token)).Status);
        Assert.Equal("not_found",(await jobs.GetAsync(other,id,submitted.Id,Token)).Status);
        var extra=input with {RequestId=Guid.NewGuid(),Kind="no_changes",PlanId=null,Signature=null};
        jobOptions.Value.MaximumJobsPerOwner=1;Assert.Equal("job_capacity",(await jobs.SubmitAsync(owner,id,extra,Token)).Code);jobOptions.Value.MaximumJobsPerOwner=8;
        jobOptions.Value.MaximumSourceBytes=originalBytes.Length;Assert.Equal("job_capacity",(await jobs.SubmitAsync(owner,id,extra,Token)).Code);jobOptions.Value.MaximumSourceBytes=268435456;
        config["Platform:DatabaseCapacityLimitBytes"]="1";Assert.Equal("job_storage_capacity",(await jobs.SubmitAsync(owner,id,extra,Token)).Code);config["Platform:DatabaseCapacityLimitBytes"]="500000000";
        await Assert.ThrowsAsync<PostgresException>(()=>Sql("UPDATE public.platform_upload_jobs SET source_hash=repeat('0',64)"));
        Assert.Equal("cancelled",(await jobs.CancelAsync(owner,id,submitted.Id,Token)).Job!.State);
        Assert.False(await jobs.RunOnceAsync(Token));Assert.Equal(original,await State());
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_imports"));
        await Reset();
        // Expiry and supersession require a new review; jobs never renew a plan.
        input=await Input();submitted=await Submit(input);await Input();
        await jobs.RunOnceAsync(Token);Assert.Equal("review_required",(await Status(submitted.Id)).State);Assert.Equal(original,await State());await Reset();
        input=await Input();submitted=await Submit(input);
        await Sql("ALTER TABLE public.platform_upload_jobs DISABLE TRIGGER platform_upload_job_identity;UPDATE public.platform_upload_jobs SET expires_at=now()-interval '1 second';ALTER TABLE public.platform_upload_jobs ENABLE TRIGGER platform_upload_job_identity");
        Assert.False(await jobs.RunOnceAsync(Token));Assert.Equal("expired",(await Status(submitted.Id)).State);Assert.Equal(original,await State());await Reset();
        // Session selection changes pause work; only an explicit owner resume reattaches it.
        input=await Input();submitted=await Submit(input);await Sql("UPDATE public.sessions SET selected_client_id=20 WHERE user_id=1");
        Assert.True(await jobs.RunOnceAsync(Token));Assert.Equal("forbidden",(await jobs.GetAsync(owner,id,submitted.Id,Token)).Status);
        await Sql("UPDATE public.sessions SET selected_client_id=10 WHERE user_id=1");
        Assert.Equal("authorization_required",(await Status(submitted.Id)).State);
        Assert.Equal("queued",(await jobs.ResumeAsync(owner,id,submitted.Id,Token)).Job!.State);
        await Jobs().RunOnceAsync(Token);Assert.Equal("succeeded",(await Status(submitted.Id)).State);
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_imports"));
        await Reset();
        // Failed storage and retry exhaustion cannot mutate canonical data.
        input=await Input();submitted=await Submit(input);storage.Failures=10;
        for(int attempt=1;attempt<=3;attempt++)
        {Assert.True(await jobs.RunOnceAsync(Token));Assert.Equal(attempt,(await Status(submitted.Id)).Attempts);await Sql("UPDATE public.platform_upload_jobs SET next_attempt_at=now()");}
        Assert.False(await jobs.RunOnceAsync(Token));Assert.Equal("failed",(await Status(submitted.Id)).State);Assert.Equal(original,await State());
        await Reset();
        // Private-bucket enforcement and corrupt object rejection are final review states.
        input=await Input();submitted=await Submit(input);storage.Public=true;
        await jobs.RunOnceAsync(Token);Assert.Equal("review_required",(await Status(submitted.Id)).State);Assert.Equal(original,await State());await Reset();
        input=await Input();submitted=await Submit(input);storage.Objects[submitted.Id.ToString("N")]=[1,2,3];
        await jobs.RunOnceAsync(Token);Assert.Equal("review_required",(await Status(submitted.Id)).State);Assert.Equal(original,await State());await Reset();
        // A lost storage upload acknowledgement is reconciled using immutable bytes.
        input=await Input();submitted=await Submit(input);storage.AbortUpload=true;
        await jobs.RunOnceAsync(Token);Assert.Equal("retry_wait",(await Status(submitted.Id)).State);storage.AbortUpload=false;
        await Sql("UPDATE public.platform_upload_jobs SET next_attempt_at=now()");await Jobs().RunOnceAsync(Token);
        Assert.Equal("succeeded",(await Status(submitted.Id)).State);Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_imports"));await Reset();
        // Cancellation during a source transfer fences its later completion.
        input=await Input();submitted=await Submit(input);
        storage.AfterRead=async()=>Assert.Equal("cancelled",(await jobs.CancelAsync(owner,id,submitted.Id,Token)).Job!.State);
        await jobs.RunOnceAsync(Token);Assert.Equal("cancelled",(await Status(submitted.Id)).State);Assert.Equal(original,await State());await Reset();
        // A stale lease cannot publish after a replacement owner has claimed it.
        input=await Input();submitted=await Submit(input);bool replaced=false;
        storage.AfterRead=async()=>
        {
            if(replaced) return;replaced=true;
            await Sql("UPDATE public.platform_upload_jobs SET lease_version=lease_version+1,worker_id='12345678-1234-1234-1234-123456789abc',lease_until=now()-interval '1 second'");
        };
        await jobs.RunOnceAsync(Token);Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_imports"));
        storage.AfterRead=null;await Jobs().RunOnceAsync(Token);Assert.Equal("succeeded",(await Status(submitted.Id)).State);Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_imports"));await Reset();
        // Stop a worker inside a real write transaction. A new instance restores
        // its missing local file and imports once from the private object.
        input=await Input();submitted=await Submit(input);
        await Sql("CREATE FUNCTION public.p28_wait() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock(90828);RETURN NEW;END $$;CREATE TRIGGER p28_wait AFTER INSERT ON public.client_data_history FOR EACH ROW EXECUTE FUNCTION public.p28_wait();SELECT pg_advisory_lock(90828)");
        using(var stopped=CancellationTokenSource.CreateLinkedTokenSource(Token))
        {
            var running=jobs.RunOnceAsync(stopped.Token);
            await Wait(async()=>await Count("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=90828 AND NOT granted")>0);
            var watch=Stopwatch.StartNew();Assert.Equal("running",(await Status(submitted.Id)).State);Assert.True(watch.Elapsed<TimeSpan.FromSeconds(3));
            Assert.Equal("job_finishing_or_busy",(await jobs.CancelAsync(owner,id,submitted.Id,Token)).Code);
            await stopped.CancelAsync();await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>running);
        }
        await Sql("SELECT pg_advisory_unlock(90828);DROP TRIGGER p28_wait ON public.client_data_history;DROP FUNCTION public.p28_wait()");
        Assert.Equal(original,await State());Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_imports"));
        File.Delete(privatePath);await Sql("UPDATE public.platform_upload_jobs SET lease_until=now()-interval '1 second'");
        await Task.WhenAll(Jobs().RunOnceAsync(Token),Jobs().RunOnceAsync(Token));
        var completed=await Status(submitted.Id);Assert.Equal("succeeded",completed.State);Assert.Equal(2,completed.Attempts);Assert.True(File.Exists(privatePath));
        Assert.Equal(originalBytes,await File.ReadAllBytesAsync(privatePath,Token));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_imports"));Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data_history"));
        Assert.Equal(completed.Completion,(await Submit(input)).Completion);
        Assert.Equal(completed.Completion,(await jobs.CancelAsync(owner,id,submitted.Id,Token)).Job!.Completion);await Reset();
        // Failure of the job's final update rolls back accounting, receipt and outcome too.
        input=await Input();submitted=await Submit(input);
        await Sql("CREATE FUNCTION public.p28_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.state='succeeded' THEN RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='synthetic';END IF;RETURN NEW;END $$;CREATE TRIGGER p28_fail BEFORE UPDATE ON public.platform_upload_jobs FOR EACH ROW EXECUTE FUNCTION public.p28_fail()");
        await jobs.RunOnceAsync(Token);Assert.Equal(original,await State());Assert.Equal(0L,await Count("SELECT count(*) FROM public.client_data_history"));Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_import_receipts"));
        await Sql("DROP TRIGGER p28_fail ON public.platform_upload_jobs;DROP FUNCTION public.p28_fail();UPDATE public.platform_upload_jobs SET next_attempt_at=now()");
        await jobs.RunOnceAsync(Token);Assert.Equal("succeeded",(await Status(submitted.Id)).State);await Reset();
        // Actual dropped PostgreSQL COMMIT acknowledgement preserves one outcome/job receipt.
        input=await Input();submitted=await Submit(input);
        await using(var proxy=fixture.CreateCommitProxy())
        {
            using var proxiedPool=fixture.CreatePool(timeoutSeconds:10,proxy:proxy);var proxiedTx=new PostgresTransactions(proxiedPool,Options.Create(new PostgresOptions()));
            // Arm from the importer clock only: staging also reads its clock,
            // and arming there would drop the preliminary read transaction.
            var commitClock=new Clock{OnRead=()=>proxy.Arm()};
            var lostImport=new UploadImportService(proxiedTx,store,signer,staging,writer,audit,importOptions,commitClock);
            await Jobs(lostImport).RunOnceAsync(Token);
            await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Token);
        }
        Assert.Equal("succeeded",(await Status(submitted.Id)).State);Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_imports"));Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data_history"));
        File.Delete(privatePath);Assert.Equal("succeeded",(await Submit(input)).State);await Reset();
        // Unchanged checks also complete atomically, with no accounting mutation.
        input=await Input("no_changes","Base");submitted=await Submit(input);await jobs.RunOnceAsync(Token);
        Assert.Equal("succeeded",(await Status(submitted.Id)).State);Assert.Equal(original,await State());Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_imports"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_import_receipts WHERE kind='no_changes'"));
        await HttpChecks(input,submitted.Id);
        // Cleanup touches registered temporary objects only, after a transfer grace period.
        string leftover=Path.Combine(directory,submitted.Id.ToString("N")+".1.restore"),unrelated=Path.Combine(directory,"unrelated.restore");
        await File.WriteAllTextAsync(leftover,"synthetic interrupted restore",Token);await File.WriteAllTextAsync(unrelated,"preserve",Token);
        await Sql("UPDATE public.platform_upload_jobs SET updated_at=now()-interval '1 hour'");
        jobOptions.Value.StorageBucket="other";
        var changedTarget=await Record.ExceptionAsync(()=>jobs.CleanupAsync(Token));Assert.NotNull(changedTarget);Assert.Equal("job_storage_configuration_changed",changedTarget.Message);
        Assert.Equal(0L,await Count("SELECT count(*) FROM public.platform_upload_jobs WHERE source_deleted"));jobOptions.Value.StorageBucket="jobs";
        await jobs.CleanupAsync(Token);
        Assert.False(File.Exists(leftover));Assert.True(File.Exists(unrelated));
        Assert.False(storage.Objects.ContainsKey(submitted.Id.ToString("N")));Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_jobs WHERE source_deleted"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.platform_upload_import_receipts WHERE kind='no_changes'"));
        Assert.Equal(1L,await Count("SELECT count(*) FROM public.client_data WHERE client_id=20 AND row_data->>'sentinel'='unchanged'"));

        async Task HttpChecks(UploadJobRequest request,Guid jobId)
        {
            await using var app=new WebApplicationFactory<Program>().WithWebHostBuilder(builder=>builder.ConfigureServices(services=>
            {
                services.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=fixture.ConnectionString;});
                services.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
                services.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);services.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);services.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
                services.PostConfigure<ColumnManagementOptions>(o=>o.Enabled=true);services.PostConfigure<TemplateManagementOptions>(o=>o.Enabled=true);services.PostConfigure<SavedMappingOptions>(o=>o.Enabled=true);
                services.PostConfigure<UploadStagingOptions>(o=>{o.Enabled=true;o.RootDirectory=directory;});services.PostConfigure<UploadPreparationOptions>(o=>o.Enabled=true);
                services.PostConfigure<UploadInputValidationOptions>(o=>o.Enabled=true);services.PostConfigure<UploadFootingOptions>(o=>o.Enabled=true);services.PostConfigure<UploadCoverageOptions>(o=>o.Enabled=true);
                services.PostConfigure<UploadIdentityOptions>(o=>o.Enabled=true);services.PostConfigure<UploadCorrectionOptions>(o=>o.Enabled=true);services.PostConfigure<UploadValidationPlanOptions>(o=>{o.Enabled=true;o.SigningKey=Settings().SigningKey;});
                services.PostConfigure<UploadImportOptions>(o=>o.Enabled=true);services.PostConfigure<UploadJobOptions>(o=>{o.Enabled=true;o.StorageUrl=storage.Url;o.StorageBucket="jobs";o.StorageKey="synthetic-secret";});
                services.RemoveAll<IHostedService>(); // Deterministic API checks; workers are tested independently above.
            }));
            app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
            string address=app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=address;
            using var client=new HttpClient(new HttpClientHandler{UseCookies=false}){BaseAddress=new Uri(address)};string route=$"/api/upload/file/{id}/jobs";
            client.DefaultRequestHeaders.Add("Origin",address);Assert.Equal(HttpStatusCode.Unauthorized,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Cookie","session_token="+owner.Token);client.DefaultRequestHeaders.Remove("Origin");Assert.Equal(HttpStatusCode.Forbidden,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
            client.DefaultRequestHeaders.Add("Origin",address);Assert.Equal(HttpStatusCode.Conflict,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);client.DefaultRequestHeaders.Add("X-App-Context",TenantSessionService.ContextKey(owner));
            Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsJsonAsync(route,request with {RequestId=Guid.Empty},Token)).StatusCode);
            using(var invalid=new StringContent("{\"requestId\":1}",System.Text.Encoding.UTF8,"application/json")) Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(route,invalid,Token)).StatusCode);
            string json=JsonSerializer.Serialize(request,new JsonSerializerOptions(JsonSerializerDefaults.Web));
            using(var duplicate=new StringContent(json.Insert(1,"\"requestId\":\""+request.RequestId+"\","),System.Text.Encoding.UTF8,"application/json")) Assert.Equal(HttpStatusCode.BadRequest,(await client.PostAsync(route,duplicate,Token)).StatusCode);
            using(var large=new HttpRequestMessage(HttpMethod.Post,route){Content=new StringContent("{\"padding\":\""+new string('x',1048576)+"\"}",System.Text.Encoding.UTF8,"application/json")})
            {large.Headers.TransferEncodingChunked=true;Assert.Equal(HttpStatusCode.RequestEntityTooLarge,(await client.SendAsync(large,Token)).StatusCode);}
            var result=await client.GetAsync(route+"/"+jobId,Token);Assert.Equal(HttpStatusCode.OK,result.StatusCode);Assert.Contains("no-store",result.Headers.CacheControl!.ToString(),StringComparison.Ordinal);
            Assert.Equal(HttpStatusCode.OK,(await client.PostAsJsonAsync(route,request,Token)).StatusCode);
        }
    }

    [Fact]
    public async Task DisabledRoutesAndStoragePrerequisites()
    {
        await using var app=new WebApplicationFactory<Program>();using var client=app.CreateClient();string route="/api/upload/file/1/jobs";
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.PostAsJsonAsync(route,new {},Token)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await client.GetAsync(route+"/"+Guid.NewGuid(),Token)).StatusCode);
        await using var invalid=new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>s.PostConfigure<UploadJobOptions>(o=>o.Enabled=true)));
        Assert.Throws<OptionsValidationException>(()=>invalid.CreateClient());
        Assert.False(new UploadJobOptions{Enabled=true,StorageUrl="http://remote.example",StorageBucket="jobs",StorageKey="key"}.ValidStorage());
    }

    private sealed class StorageServer(WebApplication app):IAsyncDisposable
    {
        internal string Url=>app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        internal ConcurrentDictionary<string,byte[]> Objects {get;}=new();
        internal bool Public,AbortUpload;internal int Failures;internal Func<Task>? AfterRead;
        internal static async Task<StorageServer> Start(CancellationToken ct)
        {
            var b=WebApplication.CreateSlimBuilder();b.Logging.ClearProviders();b.WebHost.ConfigureKestrel(s=>s.Listen(IPAddress.Loopback,0));var app=b.Build();var server=new StorageServer(app);
            app.Run(async context=>
            {
                if(context.Request.Headers.Authorization!="Bearer synthetic-secret"||context.Request.Headers["apikey"]!="synthetic-secret") {context.Response.StatusCode=403;return;}
                if(server.Failures>0) {Interlocked.Decrement(ref server.Failures);context.Response.StatusCode=503;return;}
                string path=context.Request.Path.Value!;
                if(path=="/storage/v1/bucket/jobs") {await context.Response.WriteAsJsonAsync(new {id="jobs",@public=server.Public},context.RequestAborted);return;}
                string[] parts=path.Split('/');string key=parts.Length>=3?parts[^2]:"";
                if(!Guid.TryParseExact(key,"N",out _)) {context.Response.StatusCode=400;return;}
                if(context.Request.Method=="POST")
                {
                    if(context.Request.Headers["x-upsert"]!="false") {context.Response.StatusCode=400;return;}
                    using var output=new MemoryStream();await context.Request.Body.CopyToAsync(output,context.RequestAborted);
                    context.Response.StatusCode=server.Objects.TryAdd(key,output.ToArray())?200:409;
                    if(server.AbortUpload) context.Abort();return;
                }
                if(context.Request.Method=="DELETE") {server.Objects.TryRemove(key,out _);context.Response.StatusCode=200;return;}
                if(!server.Objects.TryGetValue(key,out var bytes)) {context.Response.StatusCode=404;return;}
                if(server.AfterRead is {} action) await action();
                context.Response.ContentLength=bytes.Length;await context.Response.Body.WriteAsync(bytes,context.RequestAborted);
            });
            await app.StartAsync(ct);return server;
        }
        public async ValueTask DisposeAsync() {await app.StopAsync();await app.DisposeAsync();}
    }
}
