using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Controllers;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Reconciliation;
using AuditPlatform.Api.TrialBalance;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ReconciliationSetupTests
{
    private static readonly CancellationToken Ct = TestContext.Current.CancellationToken;

    [Fact]
    public async Task Setup_is_disabled_by_default()
    {
        var options = Options.Create(new ReconciliationSetupOptions());
        var service = new ReconciliationSetupService(null!, null!, options);
        var controller = new ReconciliationSetupController(service, options)
        {
            ControllerContext = new ControllerContext { HttpContext = new DefaultHttpContext() }
        };

        var response = await controller.Groups(10, Ct);

        Assert.Equal(503, Assert.IsType<ObjectResult>(response).StatusCode);
        Assert.Equal("disabled", (await service.ListGroupsAsync(null, 10, Ct)).Status);
    }

    [Fact]
    public async Task Setup_replaces_only_its_scope_and_preserves_missing_evidence_and_independent_categories()
    {
        await using var harness = await Harness.CreateAsync();
        var service = harness.Service;

        var suggestions = await service.AccountSuggestionsAsync(harness.Owner, 10, "Bank", Ct);
        Assert.Equal("success", suggestions.Status);
        Assert.Equal("Bank", suggestions.Value!.Value.GetProperty("accounts")[1].GetProperty("suggested_group").GetString());
        Assert.False(suggestions.Value.Value.GetProperty("accounts")[1].GetProperty("is_selected").GetBoolean());

        var saveGroup = await service.SaveGroupAsync(harness.Owner, 10,
            new AccountGroupUpdateRequest { GroupLabel = " bank ", AccountCodes = [" 100-1 "] }, Ct);
        Assert.Equal("success", saveGroup.Status);
        Assert.Equal("bank", saveGroup.Value!.Value.GetProperty("group_label").GetString());
        var selectedSuggestions = await service.AccountSuggestionsAsync(harness.Owner, 10, "Bank", Ct);
        Assert.True(selectedSuggestions.Value!.Value.GetProperty("accounts")[1].GetProperty("is_selected").GetBoolean());

        var outside = await service.ReplaceBalancesAsync(harness.Owner, 10, new ReconciliationBalanceInputsUpdateRequest
        {
            GroupLabel = "Bank", Balances = [new() { AccountCode = "200", PeriodMonth = "2026-01", OpeningBalance = 0, ActualClosingBalance = 10 }]
        }, Ct);
        Assert.Equal("invalid", outside.Status);
        Assert.Equal("account_not_in_selected_group", outside.Code);
        Assert.Equal("One or more balance account codes are not members of the selected account group",
            outside.Value!.Value.GetProperty("message").GetString());

        var initialBalances = await service.ReplaceBalancesAsync(harness.Owner, 10, new ReconciliationBalanceInputsUpdateRequest
        {
            GroupLabel = "Bank",
            Balances =
            [
                new() { AccountCode = "100-1", PeriodMonth = "2026-01-31", OpeningBalance = 0, ActualClosingBalance = 120 },
                new() { AccountCode = "100-1", PeriodMonth = "2026-02", OpeningBalance = 0, ActualClosingBalance = 0 }
            ]
        }, Ct);
        Assert.Equal("success", initialBalances.Status);
        Assert.Equal(2, initialBalances.Value!.Value.GetProperty("count").GetInt32());
        Assert.Equal(0, initialBalances.Value.Value.GetProperty("balances")[0].GetProperty("opening_balance").GetDouble());

        await harness.SqlAsync("""
            CREATE FUNCTION public.p40_reject_balance_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.actual_closing_balance=777 THEN RAISE EXCEPTION 'P40 rollback check' USING ERRCODE='23514'; END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER p40_reject_balance_insert BEFORE INSERT ON public.report_balance_inputs
                FOR EACH ROW EXECUTE FUNCTION public.p40_reject_balance_insert();
            """);
        var failedReplacement = await service.ReplaceBalancesAsync(harness.Owner, 10, new ReconciliationBalanceInputsUpdateRequest
        {
            GroupLabel = "Bank", Balances = [new() { AccountCode = "100-1", PeriodMonth = "2026-01", OpeningBalance = 5, ActualClosingBalance = 777 }]
        }, Ct);
        Assert.Equal("review", failedReplacement.Status);
        var afterRollback = await service.ListBalancesAsync(harness.Owner, 10, "Bank", Ct);
        Assert.Equal(2, afterRollback.Value!.Value.GetProperty("balances").GetArrayLength());
        Assert.Equal(120, afterRollback.Value.Value.GetProperty("balances")[0].GetProperty("actual_closing_balance").GetDouble());
        await harness.SqlAsync("DROP TRIGGER p40_reject_balance_insert ON public.report_balance_inputs; DROP FUNCTION public.p40_reject_balance_insert();");

        var missingMonth = await service.ReplaceBalancesAsync(harness.Owner, 10, new ReconciliationBalanceInputsUpdateRequest
        {
            GroupLabel = "Bank", Balances = [new() { AccountCode = "100-1", PeriodMonth = "2026-01", OpeningBalance = 0, ActualClosingBalance = 120 }]
        }, Ct);
        Assert.Equal("success", missingMonth.Status);
        var savedBalances = await service.ListBalancesAsync(harness.Owner, 10, "Bank", Ct);
        Assert.Single(savedBalances.Value!.Value.GetProperty("balances").EnumerateArray());
        Assert.Equal("2026-01", savedBalances.Value.Value.GetProperty("balances")[0].GetProperty("period_month").GetString());

        var duplicateMonth = await service.ReplaceBalancesAsync(harness.Owner, 10, new ReconciliationBalanceInputsUpdateRequest
        {
            GroupLabel = "Bank",
            Balances =
            [
                new() { AccountCode = "100-1", PeriodMonth = "2026-01", OpeningBalance = 0, ActualClosingBalance = 1 },
                new() { AccountCode = "100-1", PeriodMonth = "2026-01-31", OpeningBalance = 0, ActualClosingBalance = 1 }
            ]
        }, Ct);
        Assert.Equal("invalid", duplicateMonth.Status);
        var nonIsoMonth = await service.ReplaceBalancesAsync(harness.Owner, 10, new ReconciliationBalanceInputsUpdateRequest
        {
            GroupLabel = "Bank", Balances = [new() { AccountCode = "100-1", PeriodMonth = "11/01/2026", OpeningBalance = 0, ActualClosingBalance = 1 }]
        }, Ct);
        Assert.Equal("invalid", nonIsoMonth.Status);

        var createSet = await service.CreateCategorySetAsync(harness.Owner, 10,
            new ReportCategorySetCreateRequest { PurposeKey = " Ledger Summary ", SetName = " Ledger Summary Categories " }, Ct);
        Assert.Equal("success", createSet.Status);
        int setId = createSet.Value!.Value.GetProperty("id").GetInt32();
        Assert.Equal("ledger_summary", createSet.Value.Value.GetProperty("purpose_key").GetString());
        Assert.Equal("conflict", (await service.CreateCategorySetAsync(harness.Owner, 10,
            new ReportCategorySetCreateRequest { PurposeKey = "ledger_summary", SetName = "Duplicate" }, Ct)).Status);

        var categories = await service.ReplaceCategoriesAsync(harness.Owner, setId, new ReportCategoryDefinitionsUpdateRequest
        {
            Categories =
            [
                new() { CategoryKey = "operating", Label = "Operating" },
                new() { CategoryKey = "operating expense", Label = "Operating expense", ParentCategoryKey = "operating", SortOrder = 1 }
            ]
        }, Ct);
        Assert.Equal("success", categories.Status);
        Assert.Equal("operating_expense", categories.Value!.Value.GetProperty("categories")[1].GetProperty("category_key").GetString());

        var assignments = await service.ReplaceAssignmentsAsync(harness.Owner, setId,
            new ReportCategoryAssignmentsUpdateRequest
            {
                Assignments = [new() { AccountCode = "100-1", CategoryKey = "Operating Expense" }]
            }, Ct);
        Assert.Equal("success", assignments.Status);
        var listedAssignments = await service.ListAssignmentsAsync(harness.Owner, setId, Ct);
        Assert.Equal("operating_expense", listedAssignments.Value!.Value.GetProperty("assignments")[0].GetProperty("category_key").GetString());

        var invalidParent = await service.ReplaceCategoriesAsync(harness.Owner, setId,
            new ReportCategoryDefinitionsUpdateRequest { Categories = [new() { CategoryKey = "bad", Label = "Bad", ParentCategoryKey = "missing" }] }, Ct);
        Assert.Equal("invalid", invalidParent.Status);
        Assert.Equal("Parent category does not exist in this replacement", invalidParent.Value!.Value.GetProperty("message").GetString());
        Assert.Equal("missing", invalidParent.Value.Value.GetProperty("category_keys")[0].GetString());
        Assert.Equal(2, (await service.ListCategoriesAsync(harness.Owner, setId, Ct)).Value!.Value.GetProperty("categories").GetArrayLength());

        var foreignRead = await service.ListCategoriesAsync(harness.OtherTenant, setId, Ct);
        Assert.Equal("not_found", foreignRead.Status);
        Assert.Equal("forbidden", (await service.ListGroupsAsync(harness.OtherTenant, 10, Ct)).Status);
        Assert.Equal(1, await harness.ScalarAsync("SELECT count(*) FROM public.account_control_categories WHERE client_id=10"));
    }

    private sealed class Harness : IAsyncDisposable
    {
        private readonly PostgresFixture fixture;
        private readonly PostgresDataSourceFactory pool;
        private readonly NpgsqlConnection connection;
        internal VerifiedSession Owner { get; } = new(new string('a', 64), 1, "owner", "admin",
            DateTime.UtcNow.AddHours(1), 10, "Alpha", "A", true, 101, 0);
        internal VerifiedSession OtherTenant { get; } = new(new string('b', 64), 2, "other", "admin",
            DateTime.UtcNow.AddHours(1), 20, "Beta", "B", true, 102, 0);
        internal ReconciliationSetupService Service { get; }

        private Harness(PostgresFixture fixture, PostgresDataSourceFactory pool, NpgsqlConnection connection,
            ReconciliationSetupService service)
        { this.fixture = fixture; this.pool = pool; this.connection = connection; Service = service; }

        internal static async Task<Harness> CreateAsync()
        {
            var fixture = new PostgresFixture();
            PostgresDataSourceFactory? pool = null;
            NpgsqlConnection? connection = null;
            try
            {
                await fixture.InitializeAsync();
                await fixture.ApplyAuditSchemaAsync(Ct);
                connection = await fixture.OpenControlConnectionAsync(Ct);
                await SqlAsync(connection, """
                    CREATE TABLE public.users(id integer PRIMARY KEY,username text NOT NULL,password_hash text NOT NULL,
                        is_active boolean NOT NULL DEFAULT true,role text,last_login timestamptz);
                    CREATE TABLE public.clients(id integer PRIMARY KEY,client_code text NOT NULL,client_name text NOT NULL,
                        is_active boolean NOT NULL DEFAULT true,industry text,contact_person text,email text,phone text,
                        created_at timestamptz NOT NULL DEFAULT now());
                    CREATE TABLE public.sessions(token text PRIMARY KEY,user_id integer NOT NULL REFERENCES public.users(id),
                        expiry timestamptz NOT NULL,selected_client_id integer REFERENCES public.clients(id),
                        selected_tenant_id integer,context_version bigint NOT NULL DEFAULT 0);
                    INSERT INTO public.users(id,username,password_hash,role) VALUES(1,'owner','unused','admin'),(2,'other','unused','admin');
                    INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(20,'B','Beta');
                    INSERT INTO public.sessions VALUES
                        (repeat('a',64),1,now()+interval '1 hour',10,101,0),
                        (repeat('b',64),2,now()+interval '1 hour',20,102,0);
                    CREATE TABLE public.trial_balance_uploads(id integer PRIMARY KEY,client_id integer NOT NULL REFERENCES public.clients(id),
                        as_of_date date,file_name text,status text NOT NULL,row_count integer DEFAULT 0,notes text,
                        uploaded_at timestamptz NOT NULL DEFAULT now(),period_start date,period_end date,period_type text,validation_controls jsonb);
                    CREATE TABLE public.trial_balance_accounts(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        upload_id integer NOT NULL REFERENCES public.trial_balance_uploads(id) ON DELETE CASCADE,
                        account_code text NOT NULL,source_account_code text,account_name text,source_account_type text,
                        parent_account_code text,level integer NOT NULL DEFAULT 1,classification text NOT NULL,
                        debit_amount real,credit_amount real,opening_dr real,opening_cr real,period_dr real,period_cr real,
                        closing_dr real,closing_cr real,manually_overridden boolean NOT NULL DEFAULT false,sort_order integer NOT NULL DEFAULT 0,
                        UNIQUE(upload_id,account_code));
                    CREATE TABLE public.account_groups(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        client_id integer NOT NULL REFERENCES public.clients(id),group_label text NOT NULL,account_code text NOT NULL,
                        is_active boolean NOT NULL DEFAULT true,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),
                        UNIQUE(client_id,group_label,account_code));
                    CREATE TABLE public.report_balance_inputs(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        client_id integer NOT NULL REFERENCES public.clients(id),group_label text NOT NULL,account_code text NOT NULL,
                        period_month text NOT NULL,opening_balance numeric NOT NULL,actual_closing_balance numeric NOT NULL,
                        updated_at timestamptz DEFAULT now(),UNIQUE(client_id,group_label,account_code,period_month));
                    CREATE TABLE public.report_category_sets(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        client_id integer NOT NULL REFERENCES public.clients(id),purpose_key text NOT NULL,set_name text NOT NULL,
                        is_active boolean NOT NULL DEFAULT true,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),
                        UNIQUE(client_id,purpose_key));
                    CREATE TABLE public.report_categories(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        category_set_id integer NOT NULL REFERENCES public.report_category_sets(id) ON DELETE CASCADE,
                        category_key text NOT NULL,label text NOT NULL,parent_category_key text,sort_order integer NOT NULL DEFAULT 0,
                        is_active boolean NOT NULL DEFAULT true,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),
                        UNIQUE(category_set_id,category_key));
                    CREATE TABLE public.report_account_category_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        client_id integer NOT NULL REFERENCES public.clients(id),category_set_id integer NOT NULL REFERENCES public.report_category_sets(id) ON DELETE CASCADE,
                        account_code text NOT NULL,category_id integer NOT NULL REFERENCES public.report_categories(id) ON DELETE CASCADE,
                        updated_at timestamptz DEFAULT now(),UNIQUE(category_set_id,account_code));
                    CREATE TABLE public.account_control_categories(id integer PRIMARY KEY,client_id integer NOT NULL,category_type text NOT NULL);
                    INSERT INTO public.account_control_categories VALUES(1,10,'financial_statement');
                    INSERT INTO public.trial_balance_uploads(id,client_id,as_of_date,file_name,status,row_count,period_end,period_type)
                        VALUES(30,10,'2026-11-01','trial-balance.xlsx','parsed',4,'2026-11-01','monthly'),
                              (40,20,'2026-11-01','other.xlsx','parsed',1,'2026-11-01','monthly');
                    INSERT INTO public.trial_balance_accounts(upload_id,account_code,account_name,source_account_type,classification,sort_order)
                        VALUES(30,'100','Bank group',NULL,'parent',1),(30,'100-1','Bank leaf','bank','leaf',2),
                              (30,'100-2','Other leaf',NULL,'leaf',3),(30,'200','Cash','cash','leaf',4),(40,'20-1','Other','bank','leaf',1);
                    """);
                await fixture.ApplyTenantFoundationAsync(Ct);
                await fixture.ApplyEngagementFoundationAsync(Ct);
                await fixture.ApplyTenantUserRolesAsync(Ct);
                await SqlAsync(connection, """
                    INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE VALUES(101,'A','audit_firm'),(102,'B','business');
                    INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role) VALUES(101,1,'admin'),(102,2,'admin');
                    INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES(10,101),(20,102);
                    """);
                pool = fixture.CreatePool();
                var postgresOptions = Options.Create(new PostgresOptions { Enabled = true, ConnectionString = fixture.ConnectionString });
                var transactions = new PostgresTransactions(pool, postgresOptions);
                var logOptions = Options.Create(new LogPersistenceOptions());
                var audit = new PostgresAuditWriter(new LogSanitizer(logOptions), logOptions);
                var service = new ReconciliationSetupService(transactions, audit,
                    Options.Create(new ReconciliationSetupOptions { Enabled = true }));
                var harness = new Harness(fixture, pool, connection, service);
                await transactions.ExecuteAsync(async (db, ct) =>
                { _ = await TrialBalanceStorageStore.Authorize(db, harness.Owner, 10, false, ct); return true; });
                return harness;
            }
            catch
            {
                if (pool is not null) await pool.DisposeAsync();
                if (connection is not null) await connection.DisposeAsync();
                await fixture.DisposeAsync();
                throw;
            }
        }

        internal async Task SqlAsync(string sql)
        {
            await using var command = new NpgsqlCommand(sql, connection);
            await command.ExecuteNonQueryAsync(Ct);
        }

        internal async Task<long> ScalarAsync(string sql)
        {
            await using var command = new NpgsqlCommand(sql, connection);
            return Convert.ToInt64(await command.ExecuteScalarAsync(Ct));
        }

        public async ValueTask DisposeAsync()
        { await connection.DisposeAsync(); await pool.DisposeAsync(); await fixture.DisposeAsync(); }

        private static async Task SqlAsync(NpgsqlConnection connection, string sql)
        {
            await using var command = new NpgsqlCommand(sql, connection);
            await command.ExecuteNonQueryAsync(Ct);
        }
    }
}
