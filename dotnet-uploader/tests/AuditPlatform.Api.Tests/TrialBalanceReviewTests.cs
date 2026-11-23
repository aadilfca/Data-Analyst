using System.Diagnostics;
using System.Net;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.TrialBalance;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TrialBalanceReviewTests
{
    private static CancellationToken Ct=>TestContext.Current.CancellationToken;
    private static readonly TrialBalancePeriod Period=new("2026-01-01","2026-01-31","monthly");
    private static readonly Dictionary<string,string?> Mapping=new(){["account_code_column"]="Code",["account_name_column"]="Name",["type_column"]="Type",
        ["opening_dr_column"]="Opening Dr",["opening_cr_column"]="Opening Cr",["period_dr_column"]="Period Dr",["period_cr_column"]="Period Cr",["closing_dr_column"]="Closing Dr",["closing_cr_column"]="Closing Cr"};
    private static JsonElement Value(TrialBalanceReviewResult result){Assert.True(result.Status=="success",result.Status+"/"+result.Code);return result.Value!.Value;}
    private static TrialBalanceReviewOutcome Done(TrialBalanceReviewResult result){Assert.True(result.Status=="success",result.Status+"/"+result.Code);return Assert.IsType<TrialBalanceReviewOutcome>(result.Outcome);}
    private static TrialBalanceReviewRequest Draft(JsonElement page,params TrialBalanceReviewChange[] changes)=>new(Guid.NewGuid(),page.GetProperty("review_token").GetString()!,changes);
    private static int Id(JsonElement page,string code)=>page.GetProperty("accounts").EnumerateArray().Single(r=>r.GetProperty("account_code").GetString()==code).GetProperty("id").GetInt32();
    private static async Task<TrialBalanceReviewRequest> Preview(Harness h,int upload,string kind,TrialBalanceReviewRequest request)
        =>request with {ReviewToken=Value(await h.Service().PreviewAsync(h.Actor,10,upload,kind,request,Ct)).GetProperty("review_token").GetString()!};

    [Fact]
    public async Task DefaultOffConfigurationAndUnportedRoutesAreClosed()
    {
        await using var app=new WebApplicationFactory<Program>();Assert.False(app.Services.GetRequiredService<IOptions<TrialBalanceReviewOptions>>().Value.Enabled);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await app.CreateClient().GetAsync("/api/trial-balance/accounts?client_id=10&upload_id=1",Ct)).StatusCode);
        await using var invalid=new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>s.PostConfigure<TrialBalanceReviewOptions>(o=>o.UiEnabled=true)));
        Assert.Throws<OptionsValidationException>(()=>invalid.CreateClient());
    }

    [Fact]
    public async Task AtomicReviewReadonlyLegacyAndNativeHttpChecks()
    {
        await using var h=await Harness.Create();int upload=await h.Seed();var service=h.Service();
        Assert.Equal("unauthenticated",(await service.ReadAsync(null,new(10,upload),Ct)).Status);
        foreach(var actor in new[]{h.Other,h.Staff})Assert.Equal("forbidden",(await service.ReadAsync(actor,new(10,upload),Ct)).Status);
        Assert.Equal("forbidden",(await service.ReadAsync(h.Actor,new(20,upload),Ct)).Status);
        var page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));Assert.True(page.GetProperty("can_edit").GetBoolean());Assert.Equal(3,page.GetProperty("summary").GetProperty("total_count").GetInt32());
        string sourceBefore=await h.Protected(upload);
        await h.Sql("INSERT INTO account_control_assignments(client_id,account_code,verification_workstream) VALUES(10,'001','treasury'); INSERT INTO account_category_assignments(client_id,account_code,category_id) VALUES(10,'001',9)");
        page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));int parent=Id(page,"001"),leaf=Id(page,"001-01");
        foreach(string invalid in new[]{"A-1","1--2"," 10","10-","-10","١٠"})
            Assert.Equal("invalid",(await service.PreviewAsync(h.Actor,10,upload,"codes",Draft(page,new TrialBalanceReviewChange(parent,"001",invalid)),Ct)).Status);
        Assert.Equal("conflict",(await service.PreviewAsync(h.Actor,10,upload,"codes",Draft(page,new TrialBalanceReviewChange(parent,"001","001-01")),Ct)).Status);
        Assert.Equal("conflict",(await service.PreviewAsync(h.Actor,10,upload,"codes",Draft(page,new TrialBalanceReviewChange(parent,"stale","010")),Ct)).Status);
        var draft=Draft(page,new TrialBalanceReviewChange(parent,"001","010"),new(leaf,"001-01","010-01"));var signed=await Preview(h,upload,"codes",draft);
        await h.Sql("ALTER TABLE audit_log ADD CONSTRAINT p36_reject CHECK(action<>'TRIAL_BALANCE_REVIEW') NOT VALID");
        Assert.Equal("review",(await service.SaveAsync(h.Actor,10,upload,"codes",signed,Ct)).Status);Assert.Equal(sourceBefore,await h.Protected(upload));
        Assert.Equal(0,await h.Count("platform_trial_balance_review_operations"));Assert.Equal(1,await h.Scalar("SELECT count(*) FROM trial_balance_accounts WHERE account_code='001'"));
        await h.Sql("ALTER TABLE audit_log DROP CONSTRAINT p36_reject");
        var outcome=Done(await service.SaveAsync(h.Actor,10,upload,"codes",signed,Ct));Assert.Equal(2,outcome.CorrectedCount);Assert.Equal(sourceBefore,await h.Protected(upload));
        Assert.Equal(outcome,Done(await h.Service().SaveAsync(h.Actor,10,upload,"codes",signed,Ct)));Assert.Equal(1,await h.Count("platform_trial_balance_review_operations"));
        Assert.Equal(1,await h.Scalar("SELECT count(*) FROM account_control_assignments WHERE account_code='010' AND verification_workstream='treasury'"));
        Assert.Equal(1,await h.Scalar("SELECT count(*) FROM account_control_assignments WHERE account_code='001' AND verification_workstream='treasury'"));
        page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));Assert.True(page.GetProperty("validation_controls").GetProperty("stale").GetBoolean());
        var only=Value(await service.ReadAsync(h.Actor,new(10,upload,Limit:1,Search:"010-01"),Ct));
        Assert.Equal(page.GetProperty("validation_controls").GetProperty("current_reconciliation").GetRawText(),only.GetProperty("validation_controls").GetProperty("current_reconciliation").GetRawText());
        var reclass=await Preview(h,upload,"classification",Draft(page,new TrialBalanceReviewChange(leaf,"010-01",IsLeaf:false)));
        Assert.Equal("conflict",(await service.SaveAsync(h.Actor,10,upload,"classification",reclass with {Changes=[new(leaf,"010-01",IsLeaf:true)]},Ct)).Status);
        Done(await service.SaveAsync(h.Actor,10,upload,"classification",reclass,Ct));Assert.Equal(sourceBefore,await h.Protected(upload));
        page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));Assert.False(page.GetProperty("accounts").EnumerateArray().Single(r=>r.GetProperty("id").GetInt32()==leaf).GetProperty("is_leaf").GetBoolean());
        Assert.False(page.GetProperty("adjustments")[0].GetProperty("included_in_footing").GetBoolean());
        Assert.Equal("23514",(await Assert.ThrowsAsync<PostgresException>(()=>h.Sql("UPDATE trial_balance_accounts SET closing_dr=999"))).SqlState);
        Assert.Equal("23514",(await Assert.ThrowsAsync<PostgresException>(()=>h.Sql("UPDATE platform_trial_balance_review_operations SET outcome='{}'"))).SqlState);
        await h.Sql($"INSERT INTO account_set_versions(source_upload_id) VALUES({upload})");
        page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));Assert.False(page.GetProperty("can_edit").GetBoolean());await h.Sql("DELETE FROM account_set_versions");
        await h.Sql($"CREATE TABLE p36_reference(id integer PRIMARY KEY,account_id integer REFERENCES trial_balance_accounts(id) ON DELETE CASCADE);INSERT INTO p36_reference SELECT 1,id FROM trial_balance_accounts WHERE upload_id={upload} LIMIT 1");
        Assert.False(Value(await service.ReadAsync(h.Actor,new(10,upload),Ct)).GetProperty("can_edit").GetBoolean());await h.Sql("DROP TABLE p36_reference");
        await h.Sql("INSERT INTO trial_balance_uploads(client_id,as_of_date,file_name,status,period_start,period_end) VALUES(10,'2025-01-31','legacy.xlsx','parsed','2025-01-01','2025-01-31')");
        int legacy=(int)await h.Scalar("SELECT id FROM trial_balance_uploads WHERE file_name='legacy.xlsx'");
        var old=Value(await service.ReadAsync(h.Actor,new(10,legacy),Ct));Assert.False(old.GetProperty("can_edit").GetBoolean());Assert.Equal(JsonValueKind.Object,old.GetProperty("validation_controls").GetProperty("current_reconciliation").ValueKind);
        await h.Sql($"INSERT INTO trial_balance_accounts(upload_id,account_code,classification,opening_dr,opening_cr) VALUES({legacy},'RAW01','leaf',1e30,0),({legacy},'RAW02','leaf',0,1e-30)");
        var unsafeFooting=Value(await service.ReadAsync(h.Actor,new(10,legacy),Ct)).GetProperty("validation_controls");Assert.Equal(JsonValueKind.Null,unsafeFooting.GetProperty("current_reconciliation").ValueKind);
        int browser=await h.Seed(new("2026-02-01","2026-02-28","monthly"));
        string browserBefore=await h.Protected(browser);
        using(var client=new HttpClient{BaseAddress=new Uri(h.Address)})
        {
            client.DefaultRequestHeaders.Add("Cookie","session_token="+h.Actor.Token);
            client.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(h.Actor));
            Assert.Equal(HttpStatusCode.OK,(await client.GetAsync($"/api/trial-balance/accounts?client_id=10&upload_id={browser}",Ct)).StatusCode);
            Assert.Equal(HttpStatusCode.OK,(await client.GetAsync("/static/trial_balance.html",Ct)).StatusCode);
        }
        Assert.Equal(browserBefore,await h.Protected(browser));Assert.Equal(2,await h.Count("platform_trial_balance_review_operations"));
        TestContext.Current.TestOutputHelper!.WriteLine("P36 evidence: "+h.Directory);
    }

    [Fact]
    public async Task HttpContextExpiryTamperingAndDisabledUiAreGuarded()
    {
        await using var h=await Harness.Create();int upload=await h.Seed();using var http=new HttpClient{BaseAddress=new Uri(h.Address)};
        string url=$"/api/trial-balance/accounts?client_id=10&upload_id={upload}";
        Assert.Equal(HttpStatusCode.Unauthorized,(await http.GetAsync(url,Ct)).StatusCode);
        http.DefaultRequestHeaders.Add("Cookie","session_token="+new string('a',64));
        Assert.Equal(HttpStatusCode.Conflict,(await http.GetAsync(url,Ct)).StatusCode);
        http.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(h.Actor));
        Assert.Equal(HttpStatusCode.OK,(await http.GetAsync(url,Ct)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.GetAsync("/api/trial-balance/upload",Ct)).StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.GetAsync("/api/account-control-center/cashflow/setup?client_id=10",Ct)).StatusCode);
        h.ReviewFlags.UiEnabled=false;Assert.Equal(HttpStatusCode.ServiceUnavailable,(await http.GetAsync("/static/trial_balance.html",Ct)).StatusCode);h.ReviewFlags.UiEnabled=true;
        var clock=new ShiftClock();var service=h.Service(clock:clock);var page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));
        var draft=Draft(page,new TrialBalanceReviewChange(Id(page,"001"),"001","099"));
        Assert.Equal("invalid",(await service.PreviewAsync(h.Actor,10,upload,"codes",draft with {ReviewToken=draft.ReviewToken+"x"},Ct)).Status);
        var preview=Value(await service.PreviewAsync(h.Actor,10,upload,"codes",draft,Ct));var signed=draft with {ReviewToken=preview.GetProperty("review_token").GetString()!};
        clock.Now=clock.Now.AddMinutes(6);Assert.Equal("conflict",(await service.SaveAsync(h.Actor,10,upload,"codes",signed,Ct)).Status);
        Assert.Equal(0,await h.Count("platform_trial_balance_review_operations"));
    }

    [Fact]
    public async Task StaleRevokedConflictCapacityLostCommitAndScale()
    {
        await using var h=await Harness.Create();int upload=await h.Seed();var service=h.Service();var page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));int parent=Id(page,"001");
        var draft=Draft(page,new TrialBalanceReviewChange(parent,"001","020"));var signed=await Preview(h,upload,"codes",draft);
        await h.Sql("UPDATE sessions SET context_version=1 WHERE token=repeat('a',64)");Assert.Equal("forbidden",(await service.SaveAsync(h.Actor,10,upload,"codes",signed,Ct)).Status);await h.Sql("UPDATE sessions SET context_version=0 WHERE token=repeat('a',64)");
        await h.Sql("INSERT INTO account_control_assignments(client_id,account_code,verification_workstream) VALUES(10,'001','treasury'),(10,'020','purchases')");
        Assert.Equal("conflict",(await service.SaveAsync(h.Actor,10,upload,"codes",signed,Ct)).Status);
        page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));Assert.Equal("tb_assignment_conflict",(await service.PreviewAsync(h.Actor,10,upload,"codes",Draft(page,new TrialBalanceReviewChange(parent,"001","020")),Ct)).Code);
        await h.Sql("DELETE FROM account_control_assignments WHERE account_code='020'");page=Value(await service.ReadAsync(h.Actor,new(10,upload),Ct));signed=await Preview(h,upload,"codes",Draft(page,new TrialBalanceReviewChange(parent,"001","020")));
        Assert.Equal("tb_database_capacity",(await h.Service(capacity:1).SaveAsync(h.Actor,10,upload,"codes",signed,Ct)).Code);
        h.ReviewFlags.MaximumSnapshotBytes=1024;Assert.Equal("too_large",(await service.ReadAsync(h.Actor,new(10,upload),Ct)).Status);h.ReviewFlags.MaximumSnapshotBytes=33554432;
        using(var cancel=new CancellationTokenSource()){cancel.Cancel();Assert.Equal("cancelled",(await service.SaveAsync(h.Actor,10,upload,"codes",signed,cancel.Token)).Status);}
        await using(var blocker=await h.Fixture.OpenControlConnectionAsync(Ct))
        {
            await using var transaction=await blocker.BeginTransactionAsync(Ct);await using(var command=new NpgsqlCommand("SELECT pg_advisory_xact_lock(746102)",blocker,transaction))await command.ExecuteNonQueryAsync(Ct);
            h.ReviewFlags.TimeoutSeconds=1;var pending=service.SaveAsync(h.Actor,10,upload,"codes",signed,Ct);await h.Locked();
            Assert.Equal("busy",(await service.SaveAsync(h.Actor,10,upload,"codes",signed,Ct)).Status);Assert.Equal("timeout",(await pending).Status);await transaction.RollbackAsync(Ct);h.ReviewFlags.TimeoutSeconds=60;
        }
        await using(var proxy=h.Fixture.CreateCommitProxy())
        {
            using var pool=h.Fixture.CreatePool(1,3,proxy);var lossy=h.Service(new PostgresTransactions(pool,Options.Create(new PostgresOptions{CommandTimeoutSeconds=3})));
            proxy.Arm();var outcome=Done(await lossy.SaveAsync(h.Actor,10,upload,"codes",signed,Ct));await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5),Ct);
            Assert.Equal(outcome,Done(await h.Service().StatusAsync(h.Actor,10,upload,signed.OperationId,Ct)));Assert.Equal(outcome,Done(await h.Service().SaveAsync(h.Actor,10,upload,"codes",signed,Ct)));
        }
        Assert.Equal(1,await h.Count("platform_trial_balance_review_operations"));
        int large=await h.Seed(new("2026-03-01","2026-03-31","monthly"),"large");
        var timer=Stopwatch.StartNew();page=Value(await service.ReadAsync(h.Actor,new(10,large),Ct));Assert.Equal(10000,page.GetProperty("pagination").GetProperty("total").GetInt32());Assert.Equal(100,page.GetProperty("accounts").GetArrayLength());
        var change=page.GetProperty("accounts")[0];signed=await Preview(h,large,"classification",Draft(page,new TrialBalanceReviewChange(change.GetProperty("id").GetInt32(),change.GetProperty("account_code").GetString()!,IsLeaf:false)));
        Done(await service.SaveAsync(h.Actor,10,large,"classification",signed,Ct));
        await File.WriteAllTextAsync(Path.Combine(h.Directory,"scale-result.json"),JsonSerializer.Serialize(new {accounts=10000,read_preview_apply_ms=timer.Elapsed.TotalMilliseconds,peak_working_set_bytes=Process.GetCurrentProcess().PeakWorkingSet64}),Ct);
        using(var client=new HttpClient{BaseAddress=new Uri(h.Address)})
        {
            client.DefaultRequestHeaders.Add("Cookie","session_token="+h.Actor.Token);
            client.DefaultRequestHeaders.Add("X-App-Context",AuditPlatform.Api.Tenancy.TenantSessionService.ContextKey(h.Actor));
            using var response=await client.GetAsync($"/api/trial-balance/accounts?client_id=10&upload_id={large}&limit=100",Ct);
            Assert.Equal(HttpStatusCode.OK,response.StatusCode);
            using var body=JsonDocument.Parse(await response.Content.ReadAsStringAsync(Ct));
            Assert.Equal(10000,body.RootElement.GetProperty("pagination").GetProperty("total").GetInt32());
        }
        TestContext.Current.TestOutputHelper!.WriteLine("P36 scale evidence: "+h.Directory);
    }

    // Owned PostgreSQL/schema/session fixture copied from the accepted P35 setup.
    // P35's source fixture and test file remain unchanged.
    private sealed class Harness : IAsyncDisposable
    {
        internal PostgresFixture Fixture {get;}=new();
        internal string Directory {get;}=Path.Combine(Root(),".tools","p36-"+Guid.NewGuid().ToString("N"));
        internal TrialBalanceStorageOptions Flags {get;}=new(){Enabled=true,SigningKey=Convert.ToBase64String(System.Security.Cryptography.RandomNumberGenerator.GetBytes(32))};
        private WebApplicationFactory<Program> app=null!;private NpgsqlConnection connection=null!;
        internal VerifiedSession Actor=null!,Other=null!,Staff=null!;
        internal string Address="";
        internal TrialBalanceReviewOptions ReviewFlags=>app.Services.GetRequiredService<IOptions<TrialBalanceReviewOptions>>().Value;
        internal TrialBalanceReviewService Service(PostgresTransactions? writes=null,long capacity=500000000,TimeProvider? clock=null)=>new(writes??app.Services.GetRequiredService<PostgresTransactions>(),app.Services.GetRequiredService<PostgresReadSnapshot>(),app.Services.GetRequiredService<PostgresAuditWriter>(),Options.Create(ReviewFlags),capacity,clock);
        internal async Task<int> Seed(TrialBalancePeriod? period=null,string name="base")
        {
            var source=Source(name);if(name=="large")source=source with {Review=new(Mapping,NoTotalRow:true)};
            var storage=Storage();var preview=await storage.PreviewImportAsync(Actor,10,period??Period,source,Ct);
            Assert.True(preview.Status=="success"&&preview.Preview!.CanApply,preview.Status+"/"+preview.Code+"/"+string.Join(',',preview.Preview?.Blockers??[]));
            var result=await storage.ImportAsync(Actor,10,period??Period,source,new(Guid.NewGuid(),preview.Preview!.PreviewToken,"STORE TRIAL BALANCE"),Ct);
            Assert.True(result.Status=="success",result.Status+"/"+result.Code);return result.Outcome!.UploadId!.Value;
        }
        internal async Task<string> Protected(int upload)
        {
            string sql="SELECT md5(COALESCE((SELECT jsonb_agg(to_jsonb(a)-ARRAY['account_code','parent_account_code','level','classification','manually_overridden'] ORDER BY id)::text FROM trial_balance_accounts a WHERE upload_id="+upload+"),'')||COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM trial_balance_rollforward_adjustments x WHERE upload_id="+upload+"),'')||(SELECT to_jsonb(u)::text FROM trial_balance_uploads u WHERE id="+upload+"))";
            await using var command=new NpgsqlCommand(sql,connection);return (string)(await command.ExecuteScalarAsync(Ct))!;
        }

        internal TrialBalanceStorageService Storage(PostgresTransactions? writes=null,long capacity=500000000,TimeProvider? clock=null)=>new(writes??app.Services.GetRequiredService<PostgresTransactions>(),app.Services.GetRequiredService<PostgresReadSnapshot>(),app.Services.GetRequiredService<PostgresAuditWriter>(),Options.Create(Flags),capacity,clock);
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
                await h.Sql(await File.ReadAllTextAsync(Path.Combine(Root(),"database","migrations","0019_trial_balance_review.sql"),Ct));
                h.app=new WebApplicationFactory<Program>().WithWebHostBuilder(b=>b.ConfigureServices(s=>{
                    s.PostConfigure<PostgresOptions>(o=>{o.Enabled=true;o.ConnectionString=h.Fixture.ConnectionString;});s.PostConfigure<AuthenticationOptions>(o=>{o.Enabled=true;o.PublicOrigin="http://127.0.0.1:5001";});
                    s.PostConfigure<TrialBalanceReviewOptions>(o=>{o.Enabled=true;o.UiEnabled=true;o.SigningKey=Convert.ToBase64String(System.Security.Cryptography.RandomNumberGenerator.GetBytes(32));});
                    s.PostConfigure<ApplicationShellOptions>(o=>o.Enabled=true);s.PostConfigure<UserManagementOptions>(o=>o.Enabled=true);s.PostConfigure<ClientManagementOptions>(o=>o.Enabled=true);
                }));
                h.app.UseKestrel(server=>server.Listen(IPAddress.Loopback,0));
                h.Address=h.app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
                h.app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.PublicOrigin=h.Address;
                var sessions=h.app.Services.GetRequiredService<PostgresSessionStore>();h.Actor=(await sessions.FindAsync(new string('a',64),Ct))!;h.Other=(await sessions.FindAsync(new string('b',64),Ct))!;h.Staff=(await sessions.FindAsync(new string('c',64),Ct))!;
                Assert.NotNull(h.Actor);NativeCompatibilityFixtureFactory.ExtractScenario("p36",h.Directory);
                var synthetic=h.Source();
                Assert.True(TrialBalanceParser.Parse(synthetic.Path,".xlsx",synthetic.Review,cancellationToken:Ct).Valid);
                return h;
            }catch{await h.DisposeAsync();throw;}
        }
        public async ValueTask DisposeAsync(){if(app is not null)await app.DisposeAsync();if(connection is not null)await connection.DisposeAsync();await Fixture.DisposeAsync();}
    }
    private sealed class ShiftClock : TimeProvider {internal DateTimeOffset Now=DateTimeOffset.UtcNow;public override DateTimeOffset GetUtcNow()=>Now;}
    private static string Root(){DirectoryInfo? f=new(AppContext.BaseDirectory);while(f is not null&&!File.Exists(Path.Combine(f.FullName,"AuditPlatform.slnx")))f=f.Parent;return f!.FullName;}
}
