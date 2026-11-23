using System.Diagnostics;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.TrialBalance;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TrialBalanceStorageTests
{
    private static CancellationToken Ct=>TestContext.Current.CancellationToken;
    private static readonly TrialBalancePeriod Period=new("2026-01-01","2026-01-31","monthly");
    private static readonly Dictionary<string,string?> Mapping=new() {
        ["account_code_column"]="Code",["account_name_column"]="Name",["type_column"]="Type",
        ["opening_dr_column"]="Opening Dr",["opening_cr_column"]="Opening Cr",["period_dr_column"]="Period Dr",
        ["period_cr_column"]="Period Cr",["closing_dr_column"]="Closing Dr",["closing_cr_column"]="Closing Cr"};
    private static TrialBalanceStorageRequest Request(TrialBalanceStoragePreview p)=>new(Guid.NewGuid(),p.PreviewToken,p.Kind=="import"?"STORE TRIAL BALANCE":"DELETE TRIAL BALANCE");
    private static TrialBalanceStoragePreview Ready(TrialBalanceStorageResult result)
    {Assert.True(result.Status=="success",result.Status+"/"+result.Code);Assert.NotNull(result.Preview);Assert.True(result.Preview.CanApply,string.Join(',',result.Preview.Blockers));return result.Preview;}
    private static TrialBalanceStorageOutcome Success(TrialBalanceStorageResult result)
    {Assert.True(result.Status=="success",result.Status+"/"+result.Code);Assert.NotNull(result.Outcome);return result.Outcome;}

    [Fact]
    public async Task DefaultOffAndInvalidConfigurationDoNotAcquireDatabase()
    {
        await using var app=new WebApplicationFactory<Program>();var flags=new TrialBalanceStorageOptions();
        var service=new TrialBalanceStorageService(app.Services.GetRequiredService<PostgresTransactions>(),app.Services.GetRequiredService<PostgresReadSnapshot>(),app.Services.GetRequiredService<PostgresAuditWriter>(),Options.Create(flags));
        Assert.False(flags.Enabled);Assert.Equal("disabled",(await service.ListAsync(null,10,Ct)).Status);
        flags.Enabled=true;Assert.Equal("unavailable",(await service.ListAsync(null,10,Ct)).Status);
        flags.SigningKey=Convert.ToBase64String(new byte[32]);flags.TimeoutSeconds=61;Assert.False(flags.Valid());
    }

    [Fact]
    public async Task CanonicalStorageExactPeriodRollbackOwnershipDependenciesAndIndependentOracle()
    {
        await using var h=await Harness.Create();var service=h.Service();var source=h.Source();
        Assert.Equal("unauthenticated",(await service.ListAsync(null,10,Ct)).Status);
        Assert.Equal("forbidden",(await service.PreviewImportAsync(h.Other,10,Period,source,Ct)).Status);
        Assert.Equal("forbidden",(await service.PreviewImportAsync(h.Staff,10,Period,source,Ct)).Status);
        Assert.Equal("forbidden",(await service.PreviewImportAsync(h.Actor,20,Period,source,Ct)).Status);
        Assert.Equal("invalid",(await service.PreviewImportAsync(h.Actor,10,Period with {Start="2026-02-01"},source,Ct)).Status);
        // A P34-safe float64 can still be unsafe in the actual REAL columns.
        foreach(string name in new[]{"real_precision","fraction_precision"})
        {var bad=h.Source(name) with {Review=new(Mapping,ExcludedExcelRows:[6,7],NoTotalRow:true)};Assert.Equal("tb_storage_precision_review",(await service.PreviewImportAsync(h.Actor,10,Period,bad,Ct)).Code);}
        Assert.Equal(0,await h.Count("trial_balance_uploads"));
        await h.Sql("INSERT INTO account_category_assignments(client_id,account_code,category_id) VALUES(10,'002',12); INSERT INTO account_control_assignments(client_id,account_code,verification_workstream) VALUES(10,'002','treasury'); INSERT INTO account_control_cashflow_assignments(client_id,account_code,section) VALUES(10,'002','operating'); INSERT INTO account_control_cashflow_category_assignments(client_id,account_code,category_id) VALUES(10,'002',7)");
        var preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));var request=Request(preview);
        Assert.Equal("tb_source_changed",(await service.ImportAsync(h.Actor,10,Period,h.Source("changed"),request,Ct)).Code);
        Assert.Equal("invalid",(await service.ImportAsync(h.Actor,10,Period,source,request with {PreviewToken=request.PreviewToken+"x"},Ct)).Status);
        Assert.Equal("tb_confirmation_required",(await service.ImportAsync(h.Actor,10,Period,source,request with {Confirmation="yes"},Ct)).Code);
        var first=Success(await service.ImportAsync(h.Actor,10,Period,source,request,Ct));Assert.Equal(3,first.AccountCount);Assert.Equal(2,first.LeafCount);Assert.Equal(1,first.ParentCount);Assert.Equal(1,first.RowAdjustmentCount);
        Assert.Equal(first,Success(await h.Service().ImportAsync(h.Actor,10,Period,source with {Path=source.Path+".missing"},request,Ct)));
        Assert.Equal(first,Success(await h.Service().StatusAsync(h.Actor,10,request.OperationId,Ct)));
        Assert.Equal("conflict",(await service.ImportAsync(h.Actor,10,Period,source,request with {Confirmation="STORE TRIAL BALANCE",PreviewToken=preview.PreviewToken+"x"},Ct)).Status);
        Assert.Equal(1,await h.Count("platform_trial_balance_operations"));Assert.Equal(1,await h.Count("audit_log"));
        string corrupt=Path.Combine(h.Directory,"corrupt.xlsx");await File.WriteAllTextAsync(corrupt,"not a workbook",Ct);
        var corruptPreview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));
        Assert.Equal("review",(await service.ImportAsync(h.Actor,10,Period,source with {Path=corrupt},Request(corruptPreview),Ct)).Status);
        Assert.Equal(1,await h.Count("trial_balance_uploads"));
        foreach(string table in new[]{"account_category_assignments","account_control_assignments","account_control_cashflow_assignments","account_control_cashflow_category_assignments"})
            Assert.Equal(1,await h.Scalar($"SELECT count(*) FROM {table} WHERE client_id=10 AND account_code='003'"));
        Assert.Equal("23514",(await Assert.ThrowsAsync<PostgresException>(()=>h.Sql("UPDATE trial_balance_accounts SET closing_dr=99 WHERE account_code='001-01'"))).SqlState);
        Assert.Equal("23514",(await Assert.ThrowsAsync<PostgresException>(()=>h.Sql($"DELETE FROM trial_balance_uploads WHERE id={first.UploadId}"))).SqlState);
        Assert.Equal("23514",(await Assert.ThrowsAsync<PostgresException>(()=>h.Sql("UPDATE platform_trial_balance_operations SET outcome='{}'"))).SqlState);
        var secondPeriod=new TrialBalancePeriod("2026-01-15","2026-02-15","custom");
        var otherPreview=Ready(await service.PreviewImportAsync(h.Actor,10,secondPeriod,source,Ct));var other=Success(await service.ImportAsync(h.Actor,10,secondPeriod,source,Request(otherPreview),Ct));
        Assert.Equal(2,await h.Count("trial_balance_uploads"));
        preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));Assert.Single(preview.Conflicts);Assert.Equal(first.UploadId,preview.Conflicts[0].Id);
        await h.Sql("UPDATE account_control_assignments SET verification_workstream='purchases' WHERE account_code='003'");
        Assert.Equal("conflict",(await service.ImportAsync(h.Actor,10,Period,source,Request(preview),Ct)).Status);
        preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));
        Assert.Equal("tb_assignment_conflict",(await service.ImportAsync(h.Actor,10,Period,source,Request(preview),Ct)).Code);
        Assert.Equal(1,await h.Scalar($"SELECT count(*) FROM trial_balance_uploads WHERE id={first.UploadId}"));
        await h.Sql("UPDATE account_control_assignments SET verification_workstream='treasury' WHERE account_code='003'");
        // Deliberate audit constraint fails after replacement writes: all source
        // changes and the operation ledger must roll back together.
        preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));
        await h.Sql("ALTER TABLE audit_log ADD CONSTRAINT p35_reject CHECK(action<>'TRIAL_BALANCE_IMPORT') NOT VALID");
        Assert.Equal("review",(await service.ImportAsync(h.Actor,10,Period,source,Request(preview),Ct)).Status);
        Assert.Equal(2,await h.Count("trial_balance_uploads"));Assert.Equal(2,await h.Count("platform_trial_balance_operations"));Assert.Equal(6,await h.Count("trial_balance_accounts"));
        await h.Sql("ALTER TABLE audit_log DROP CONSTRAINT p35_reject");
        preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));
        var replaced=Success(await service.ImportAsync(h.Actor,10,Period,source,Request(preview),Ct));Assert.Equal(1,replaced.RemovedUploads);Assert.NotEqual(first.UploadId,replaced.UploadId);
        Assert.Equal(1,await h.Scalar($"SELECT count(*) FROM trial_balance_uploads WHERE id={other.UploadId}"));
        await h.Sql($"INSERT INTO account_set_versions(source_upload_id) VALUES({replaced.UploadId})");
        var blocked=(await service.PreviewDeleteAsync(h.Actor,10,replaced.UploadId!.Value,Ct)).Preview!;Assert.False(blocked.CanApply);Assert.Contains("tb_protected_account_set_version",blocked.Blockers);
        await h.Sql("DELETE FROM account_set_versions; CREATE TABLE protected_reference(id integer PRIMARY KEY,account_id integer REFERENCES trial_balance_accounts(id) ON DELETE CASCADE)");
        await h.Sql($"INSERT INTO protected_reference SELECT 1,id FROM trial_balance_accounts WHERE upload_id={replaced.UploadId} LIMIT 1");
        blocked=(await service.PreviewDeleteAsync(h.Actor,10,replaced.UploadId.Value,Ct)).Preview!;Assert.Contains("tb_protected_or_cross_scope_reference",blocked.Blockers);
        await h.Sql("DROP TABLE protected_reference");
        preview=Ready(await service.PreviewDeleteAsync(h.Actor,10,replaced.UploadId.Value,Ct));
        await h.Sql("UPDATE sessions SET context_version=1 WHERE token=repeat('a',64)");
        Assert.Equal("forbidden",(await service.DeleteAsync(h.Actor,10,replaced.UploadId.Value,Request(preview),Ct)).Status);
        await h.Sql("UPDATE sessions SET context_version=0 WHERE token=repeat('a',64)");
        var removed=Success(await service.DeleteAsync(h.Actor,10,replaced.UploadId.Value,Request(preview),Ct));Assert.False(removed.AssignmentsCleared);
        Assert.Equal(2,await h.Count("account_category_assignments"));
        await h.Sql("CREATE TABLE mapping_reference(id integer PRIMARY KEY,assignment_id integer REFERENCES account_category_assignments(id) ON DELETE CASCADE); INSERT INTO mapping_reference SELECT 1,id FROM account_category_assignments WHERE client_id=10 LIMIT 1");
        blocked=(await service.PreviewDeleteAsync(h.Actor,10,other.UploadId!.Value,Ct)).Preview!;Assert.Contains("tb_protected_or_cross_scope_reference",blocked.Blockers);
        await h.Sql("DROP TABLE mapping_reference");
        preview=Ready(await service.PreviewDeleteAsync(h.Actor,10,other.UploadId!.Value,Ct));request=Request(preview);
        removed=Success(await service.DeleteAsync(h.Actor,10,other.UploadId.Value,request,Ct));Assert.True(removed.AssignmentsCleared);
        Assert.Equal(0,await h.Count("account_category_assignments"));Assert.Equal(2,await h.Count("account_control_assignments"));
        Assert.Equal(0,await h.Count("trial_balance_accounts"));Assert.Equal(0,await h.Count("trial_balance_rollforward_adjustments"));Assert.Equal(0,await h.Count("platform_trial_balance_uploads"));
        Assert.Equal(removed,Success(await h.Service().DeleteAsync(h.Actor,10,other.UploadId.Value,request,Ct)));
        await h.Sql("INSERT INTO trial_balance_uploads(client_id,as_of_date,file_name,status,period_start,period_end) VALUES(10,'2026-01-31','legacy.xlsx','parsed','2026-01-01','2026-01-31')");
        blocked=(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct)).Preview!;Assert.Contains("tb_legacy_ownership_review_required",blocked.Blockers);
        Assert.Equal(0,await h.Scalar("SELECT count(*) FROM trial_balance_uploads WHERE client_id=20"));
        TestContext.Current.TestOutputHelper!.WriteLine("P35 evidence: "+h.Directory);
    }

    [Fact]
    public async Task ReviewedSourceChoicesGlobalDifferencesTypeDriftExpiryAndTenThousandRows()
    {
        await using var h=await Harness.Create();var service=h.Service();
        var source=h.Source() with {Review=new(Mapping,ClassificationOverrides:new Dictionary<int,string>{{2,"leaf"}},ExcludedExcelRows:[6,7],NoTotalRow:true)};
        var preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));
        var saved=Success(await service.ImportAsync(h.Actor,10,Period,source,Request(preview),Ct));Assert.Equal(3,saved.LeafCount);Assert.True(saved.GlobalAdjustmentCount>0);
        Assert.Equal(1,await h.Scalar($"SELECT count(*) FROM trial_balance_uploads WHERE id={saved.UploadId} AND validation_controls::jsonb->'global_adjustments'->0->>'account_name'='Opening footing' AND validation_controls::jsonb->'global_adjustments'->0->>'global_adjustment'='true'"));
        Assert.Equal(1,await h.Scalar($"SELECT count(*) FROM trial_balance_accounts WHERE upload_id={saved.UploadId} AND account_code='001' AND manually_overridden"));
        Assert.Equal(100,await h.Scalar($"SELECT opening_dr FROM trial_balance_accounts WHERE upload_id={saved.UploadId} AND account_code='001'"));
        var clock=new ShiftClock();var expiring=h.Service(clock:clock);
        preview=Ready(await expiring.PreviewDeleteAsync(h.Actor,10,saved.UploadId!.Value,Ct));clock.Now=clock.Now.AddMinutes(6);
        Assert.Equal("tb_confirmation_stale",(await expiring.DeleteAsync(h.Actor,10,saved.UploadId.Value,Request(preview),Ct)).Code);
        preview=Ready(await service.PreviewDeleteAsync(h.Actor,10,saved.UploadId.Value,Ct));
        await h.Sql("UPDATE platform_tenant_memberships SET tenant_role='member' WHERE tenant_id=101 AND user_id=1");
        Assert.Equal("forbidden",(await service.DeleteAsync(h.Actor,10,saved.UploadId.Value,Request(preview),Ct)).Status);
        await h.Sql("UPDATE platform_tenant_memberships SET tenant_role='admin' WHERE tenant_id=101 AND user_id=1");
        Success(await service.DeleteAsync(h.Actor,10,saved.UploadId.Value,Request(preview),Ct));
        source=h.Source("numeric_code");preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));
        saved=Success(await service.ImportAsync(h.Actor,10,Period,source,Request(preview),Ct));
        Assert.Equal(1,await h.Scalar($"SELECT count(*) FROM trial_balance_accounts WHERE upload_id={saved.UploadId} AND source_account_code='2' AND account_code='003'"));
        var typedPeriod=new TrialBalancePeriod("2026-04-01","2026-04-30","monthly");source=h.Source("typed_error");
        preview=Ready(await service.PreviewImportAsync(h.Actor,10,typedPeriod,source,Ct));saved=Success(await service.ImportAsync(h.Actor,10,typedPeriod,source,Request(preview),Ct));
        Assert.Equal(1,await h.Scalar($"SELECT count(*) FROM trial_balance_accounts WHERE upload_id={saved.UploadId} AND account_code='001' AND source_account_type='#N/A' AND raw_row_data->>'Type'='#N/A'"));
        var large=new TrialBalanceStorageSource(Path.Combine(h.Directory,"large.xlsx"),".xlsx","large.xlsx",new(Mapping,NoTotalRow:true));
        var period=new TrialBalancePeriod("2026-03-01","2026-03-31","monthly");
        preview=Ready(await service.PreviewImportAsync(h.Actor,10,period,large,Ct));
        await h.Sql("ALTER TABLE trial_balance_accounts ALTER COLUMN opening_dr TYPE numeric(20,2)");
        Assert.Equal("tb_confirmation_stale",(await service.ImportAsync(h.Actor,10,period,large,Request(preview),Ct)).Code);
        preview=Ready(await service.PreviewImportAsync(h.Actor,10,period,large,Ct));
        var timer=Stopwatch.StartNew();saved=Success(await service.ImportAsync(h.Actor,10,period,large,Request(preview),Ct));
        Assert.Equal(10000,saved.AccountCount);Assert.Equal(10000,await h.Scalar($"SELECT count(*) FROM trial_balance_accounts WHERE upload_id={saved.UploadId}"));
        Assert.Equal(120000,await h.Scalar($"SELECT sum(closing_dr) FROM trial_balance_accounts WHERE upload_id={saved.UploadId}"));
        await File.WriteAllTextAsync(Path.Combine(h.Directory,"scale-result.json"),JsonSerializer.Serialize(new {accounts=saved.AccountCount,elapsed_ms=timer.Elapsed.TotalMilliseconds,peak_working_set_bytes=Process.GetCurrentProcess().PeakWorkingSet64}),Ct);
        TestContext.Current.TestOutputHelper!.WriteLine("P35 scale evidence: "+h.Directory);
    }

    [Fact]
    public async Task LostCommitRecoveryLimitsCancellationAndConcurrentApplyNeverReplay()
    {
        await using var h=await Harness.Create();var service=h.Service();var source=h.Source();
        var preview=Ready(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct));var request=Request(preview);
        var small=h.Service(capacity:1);Assert.Equal("tb_database_capacity",(await small.ImportAsync(h.Actor,10,Period,source,request,Ct)).Code);
        Assert.Equal(0,await h.Count("platform_trial_balance_operations"));
        h.Flags.MaximumSnapshotBytes=1024;Assert.Equal("too_large",(await service.PreviewImportAsync(h.Actor,10,Period,source,Ct)).Status);h.Flags.MaximumSnapshotBytes=33554432;
        using(var cancel=new CancellationTokenSource()){cancel.Cancel();Assert.Equal("cancelled",(await service.ImportAsync(h.Actor,10,Period,source,request,cancel.Token)).Status);}
        await using(var blocker=await h.Fixture.OpenControlConnectionAsync(Ct))
        {
            await using var transaction=await blocker.BeginTransactionAsync(Ct);
            await using(var command=new NpgsqlCommand("SELECT pg_advisory_xact_lock(746102)",blocker,transaction))await command.ExecuteNonQueryAsync(Ct);
            h.Flags.TimeoutSeconds=1;var pending=service.ImportAsync(h.Actor,10,Period,source,request,Ct);await h.Locked();
            Assert.Equal("busy",(await h.Service().ImportAsync(h.Actor,10,Period,source,Request(preview),Ct)).Status);
            Assert.Equal("timeout",(await pending).Status);await transaction.RollbackAsync(Ct);h.Flags.TimeoutSeconds=60;
        }
        Assert.Equal(0,await h.Count("trial_balance_uploads"));
        await using var proxy=h.Fixture.CreateCommitProxy();using var pool=h.Fixture.CreatePool(1,3,proxy);
        var pg=Options.Create(new PostgresOptions {CommandTimeoutSeconds=3});var lossy=h.Service(new PostgresTransactions(pool,pg));
        proxy.Arm();var applied=Success(await lossy.ImportAsync(h.Actor,10,Period,source,request,Ct));
        await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Ct);
        Assert.Equal(applied,Success(await h.Service().ImportAsync(h.Actor,10,Period,source,request,Ct)));
        Assert.Equal(1,await h.Count("trial_balance_uploads"));Assert.Equal(1,await h.Count("audit_log"));Assert.Equal(1,await h.Count("platform_trial_balance_operations"));
        h.Flags.MaximumOperationsPerClient=1;Assert.Contains("tb_operation_capacity",(await service.PreviewDeleteAsync(h.Actor,10,applied.UploadId!.Value,Ct)).Preview!.Blockers);
    }

    private sealed class Harness : IAsyncDisposable
    {
        internal PostgresFixture Fixture {get;}=new();
        internal string Directory {get;}=Path.Combine(Root(),".tools","p35-"+Guid.NewGuid().ToString("N"));
        internal TrialBalanceStorageOptions Flags {get;}=new(){Enabled=true,SigningKey=Convert.ToBase64String(System.Security.Cryptography.RandomNumberGenerator.GetBytes(32))};
        private WebApplicationFactory<Program> app=null!;private NpgsqlConnection connection=null!;
        internal VerifiedSession Actor=null!,Other=null!,Staff=null!;
        internal TrialBalanceStorageService Service(PostgresTransactions? writes=null,long capacity=500000000,TimeProvider? clock=null)=>new(writes??app.Services.GetRequiredService<PostgresTransactions>(),app.Services.GetRequiredService<PostgresReadSnapshot>(),app.Services.GetRequiredService<PostgresAuditWriter>(),Options.Create(Flags),capacity,clock);
        internal TrialBalanceStorageSource Source(string name="base")=>new(Path.Combine(Directory,name+".xlsx"),".xlsx","base.xlsx",new(Mapping,CodeOverrides:new Dictionary<int,string>{{5,"003"}},ExcludedExcelRows:[6],TotalExcelRow:7));
        internal async Task Sql(string sql){await using var c=new NpgsqlCommand(sql,connection);await c.ExecuteNonQueryAsync(Ct);}
        internal async Task<long> Scalar(string sql){await using var c=new NpgsqlCommand(sql,connection);return Convert.ToInt64(await c.ExecuteScalarAsync(Ct));}
        internal Task<long> Count(string table)=>Scalar("SELECT count(*) FROM public."+table);
        internal async Task Locked()
        {var watch=Stopwatch.StartNew();while(watch.Elapsed<TimeSpan.FromSeconds(5)){if(await Scalar("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")>0)return;await Task.Delay(10,Ct);}Assert.Fail("P35 writer did not reach owned lock.");}
        internal static async Task<Harness> Create()
        {
            var h=new Harness();try {
                await h.Fixture.InitializeAsync();await h.Fixture.ApplyAuditSchemaAsync(Ct);h.connection=await h.Fixture.OpenControlConnectionAsync(Ct);
                await h.Sql("""
                    CREATE TABLE users(id integer PRIMARY KEY,username text,password_hash text,is_active boolean DEFAULT true,role text,last_login timestamptz);
                    CREATE TABLE clients(id integer PRIMARY KEY,client_code text,client_name text,is_active boolean DEFAULT true,industry text,contact_person text,email text,phone text,created_at timestamptz DEFAULT now());
                    CREATE TABLE sessions(token text PRIMARY KEY,user_id integer REFERENCES users(id),expiry timestamptz,selected_client_id integer,selected_tenant_id integer,context_version bigint NOT NULL DEFAULT 0);
                    INSERT INTO users(id,username,password_hash,role) VALUES(1,'owner','unused','admin'),(2,'other','unused','admin'),(3,'staff','unused','user');
                    INSERT INTO clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(20,'B','Beta');
                    INSERT INTO sessions VALUES(repeat('a',64),1,now()+interval '1 hour',10,101,0),(repeat('b',64),2,now()+interval '1 hour',20,102,0),(repeat('c',64),3,now()+interval '1 hour',10,101,0);
                    CREATE TABLE trial_balance_uploads(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id integer NOT NULL REFERENCES clients(id) ON DELETE CASCADE,as_of_date date,file_name text,status text DEFAULT 'pending',row_count integer DEFAULT 0,notes text,uploaded_at timestamptz DEFAULT now(),period_start date,period_end date,period_type text,validation_controls text);
                    CREATE TABLE trial_balance_accounts(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,upload_id integer NOT NULL REFERENCES trial_balance_uploads(id) ON DELETE CASCADE,account_code text NOT NULL,source_account_code text,account_name text,parent_account_code text,level integer NOT NULL DEFAULT 1,classification text NOT NULL CHECK(classification IN('parent','leaf')),debit_amount real,credit_amount real,opening_dr real,opening_cr real,period_dr real,period_cr real,closing_dr real,closing_cr real,source_account_type text,manually_overridden boolean NOT NULL DEFAULT false,category_id integer,sort_order integer NOT NULL DEFAULT 0,raw_row_data jsonb,created_at timestamptz DEFAULT now(),UNIQUE(upload_id,account_code));
                    CREATE TABLE trial_balance_rollforward_adjustments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,upload_id integer NOT NULL REFERENCES trial_balance_uploads(id) ON DELETE CASCADE,account_id integer NOT NULL REFERENCES trial_balance_accounts(id) ON DELETE CASCADE,source_excel_row integer NOT NULL,expected_closing_net numeric NOT NULL,original_closing_net numeric NOT NULL,difference_net numeric NOT NULL,closing_dr_adjustment numeric NOT NULL DEFAULT 0,closing_cr_adjustment numeric NOT NULL DEFAULT 0,created_at timestamptz DEFAULT now(),UNIQUE(upload_id,account_id));
                    CREATE TABLE trial_balance_profit_status(upload_id integer PRIMARY KEY REFERENCES trial_balance_uploads(id) ON DELETE CASCADE,status text);
                    CREATE TABLE account_set_versions(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,source_upload_id integer REFERENCES trial_balance_uploads(id) ON DELETE RESTRICT);
                    CREATE TABLE account_category_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id integer REFERENCES clients(id),account_code text,category_id integer,updated_at timestamptz DEFAULT now(),UNIQUE(client_id,account_code));
                    CREATE TABLE account_control_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id integer REFERENCES clients(id),account_code text,verification_workstream text,financial_statement_category_id integer,note_category_id integer,updated_by text,updated_at timestamptz DEFAULT now(),UNIQUE(client_id,account_code));
                    CREATE TABLE account_control_cashflow_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id integer REFERENCES clients(id),account_code text,section text DEFAULT 'unassigned',category_label text,cash_bank_role text DEFAULT 'none',treatment_type text DEFAULT 'standard',updated_by text,updated_at timestamptz DEFAULT now(),UNIQUE(client_id,account_code));
                    CREATE TABLE account_control_cashflow_category_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id integer REFERENCES clients(id),account_code text,category_id integer,updated_by text,updated_at timestamptz DEFAULT now(),UNIQUE(client_id,account_code));
                    """);
                await h.Fixture.ApplyTenantFoundationAsync(Ct);await h.Fixture.ApplyEngagementFoundationAsync(Ct);await h.Fixture.ApplyTenantUserRolesAsync(Ct);
                await h.Sql("""
                    INSERT INTO platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
                    INSERT INTO platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin'),(101,3,'member');
                    INSERT INTO platform_tenant_client_homes VALUES(10,101,now()),(20,102,now());
                    """);
                await h.Sql(await File.ReadAllTextAsync(Path.Combine(Root(),"database","migrations","0018_trial_balance_storage.sql"),Ct));
                h.app=new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>{
                    s.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=h.Fixture.ConnectionString;});s.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
                    s.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);s.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);s.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
                }));
                var sessions=h.app.Services.GetRequiredService<PostgresSessionStore>();h.Actor=(await sessions.FindAsync(new string('a',64),Ct))!;h.Other=(await sessions.FindAsync(new string('b',64),Ct))!;h.Staff=(await sessions.FindAsync(new string('c',64),Ct))!;
                Assert.NotNull(h.Actor);NativeCompatibilityFixtureFactory.ExtractScenario("p35",h.Directory);
                var synthetic=h.Source();
                Assert.True(TrialBalanceParser.Parse(synthetic.Path,".xlsx",synthetic.Review,cancellationToken:Ct).Valid);
                return h;
            }catch{await h.DisposeAsync();throw;}
        }
        public async ValueTask DisposeAsync(){if(app is not null)await app.DisposeAsync();if(connection is not null)await connection.DisposeAsync();await Fixture.DisposeAsync();}
    }
    private sealed class ShiftClock : TimeProvider
    {internal DateTimeOffset Now=DateTimeOffset.UtcNow;public override DateTimeOffset GetUtcNow()=>Now;}
    private static string Root(){DirectoryInfo? f=new(AppContext.BaseDirectory);while(f is not null&&!File.Exists(Path.Combine(f.FullName,"AuditPlatform.slnx")))f=f.Parent;return f!.FullName;}
}
