using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.TrialBalance;
using AuditPlatform.Api.Verification;
using AuditPlatform.Api.Controllers;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class VerificationSetupTests
{
    private static readonly CancellationToken Ct = TestContext.Current.CancellationToken;

    [Fact]
    public async Task Setup_stays_disabled_by_default()
    {
        var options = Options.Create(new VerificationSetupOptions());
        var service = new VerificationSetupService(null!, null!, options);

        var result = await service.LatestAccountsAsync(null, 10, Ct);
        var controller = new VerificationSetupController(service, options)
        {
            ControllerContext = new ControllerContext { HttpContext = new DefaultHttpContext() }
        };
        var response = await controller.LatestAccounts(10, Ct);

        Assert.Equal("disabled", result.Status);
        Assert.Equal("verification_setup_disabled", result.Code);
        Assert.Equal(503, Assert.IsType<ObjectResult>(response).StatusCode);
    }

    [Fact]
    public async Task Assignments_preserve_other_categories_and_account_sets_follow_selector_lifecycle()
    {
        await using var harness = await Harness.CreateAsync();
        var service = harness.Service;

        var save = await service.SaveAssignmentAsync(harness.Owner,
            new WorkstreamAssignmentRequest
            {
                ClientId = 10, TrialBalanceUploadId = 30, AccountCode = "10-1-1",
                VerificationWorkstream = "local_vendor"
            }, Ct);
        Assert.Equal("success", save.Status);

        var bulk = await service.ApplyToChildrenAsync(harness.Owner,
            new WorkstreamBulkAssignmentRequest
            {
                ClientId = 10, TrialBalanceUploadId = 30, ParentAccountCode = "10-1",
                VerificationWorkstream = "import_vendor"
            }, Ct);
        Assert.Equal("success", bulk.Status);
        Assert.Equal(2, bulk.Value!.Value.GetProperty("leaf_count").GetInt32());
        Assert.Equal((71, 81, "import_vendor"), await harness.AssignmentAsync("10-1-1"));
        Assert.Equal(((int?)null, (int?)null, (string?)null), await harness.AssignmentAsync("10-10-1"));

        var create = await service.CreateSetAsync(harness.Owner, new AccountSetCreateRequest
        {
            ClientId = 10, SetKey = "  Vendor  Set ", DisplayName = "Vendor Set", Description = " Draft ",
            Selectors =
            [
                new("include_subtree", "10-1", 0),
                new("exclude_exact", "10-1-2", 1)
            ]
        }, Ct);
        Assert.Equal("success", create.Status);
        int setId = create.Value!.Value.GetProperty("id").GetInt32();
        int versionId = create.Value.Value.GetProperty("versions")[0].GetProperty("id").GetInt32();
        Assert.Equal("vendor_set", create.Value.Value.GetProperty("set_key").GetString());

        var preview = await service.PreviewAsync(harness.Owner, versionId, null, Ct);
        Assert.Equal("success", preview.Status);
        var leaves = preview.Value!.Value.GetProperty("resolved_leaf_accounts");
        Assert.Single(leaves.EnumerateArray());
        Assert.Equal("10-1-1", leaves[0].GetProperty("account_code").GetString());

        await harness.SqlAsync("""
            CREATE FUNCTION public.p39_reject_publish_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.action='ACCOUNT_SET_VERSION_PUBLISHED' THEN RAISE EXCEPTION 'P39 rollback check' USING ERRCODE='23514'; END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER p39_reject_publish_audit BEFORE INSERT ON public.audit_log
                FOR EACH ROW EXECUTE FUNCTION public.p39_reject_publish_audit();
            """);
        var rejectedPublish = await service.PublishAsync(harness.Owner, versionId, Ct);
        Assert.Equal("review", rejectedPublish.Status);
        var afterRollback = await service.GetSetAsync(harness.Owner, setId, Ct);
        Assert.Equal(JsonValueKind.Null, afterRollback.Value!.Value.GetProperty("active_version_id").ValueKind);
        Assert.Equal("draft", afterRollback.Value.Value.GetProperty("versions")[0].GetProperty("status").GetString());
        await harness.SqlAsync("DROP TRIGGER p39_reject_publish_audit ON public.audit_log; DROP FUNCTION public.p39_reject_publish_audit();");

        var publish = await service.PublishAsync(harness.Owner, versionId, Ct);
        Assert.Equal("success", publish.Status);
        var immutable = await service.UpdateDraftAsync(harness.Owner, versionId,
            new AccountSetDraftRequest { DisplayName = "Changed", Selectors = [] }, Ct);
        Assert.Equal("conflict", immutable.Status);

        var other = await service.GetSetAsync(harness.OtherTenant, setId, Ct);
        Assert.Equal("not_found", other.Status);
        Assert.Equal("forbidden", (await service.ListSetsAsync(harness.OtherTenant, 10, Ct)).Status);

        var revision = await service.CreateRevisionDraftAsync(harness.Owner, setId, Ct);
        Assert.Equal("success", revision.Status);
        Assert.Equal("draft", revision.Value!.Value.GetProperty("versions")[0].GetProperty("status").GetString());
        var secondDraft = await service.CreateRevisionDraftAsync(harness.Owner, setId, Ct);
        Assert.Equal("conflict", secondDraft.Status);
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
        internal VerificationSetupService Service { get; }

        private Harness(PostgresFixture fixture, PostgresDataSourceFactory pool, PostgresTransactions transactions,
            PostgresAuditWriter audit, NpgsqlConnection connection)
        {
            this.fixture = fixture; this.pool = pool; this.connection = connection;
            Service = new(transactions, audit, Options.Create(new VerificationSetupOptions { Enabled = true }));
        }

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
                    INSERT INTO public.users(id,username,password_hash,role) VALUES
                        (1,'owner','unused','admin'),(2,'other','unused','admin');
                    INSERT INTO public.clients(id,client_code,client_name) VALUES(10,'A','Alpha'),(20,'B','Beta');
                    INSERT INTO public.sessions VALUES
                        (repeat('a',64),1,now()+interval '1 hour',10,101,0),
                        (repeat('b',64),2,now()+interval '1 hour',20,102,0);
                    CREATE TABLE public.trial_balance_uploads(id integer PRIMARY KEY,client_id integer NOT NULL REFERENCES public.clients(id),
                        as_of_date date,file_name text,status text NOT NULL,row_count integer DEFAULT 0,notes text,
                        uploaded_at timestamptz NOT NULL DEFAULT now(),period_start date,period_end date,period_type text,validation_controls jsonb);
                    CREATE TABLE public.trial_balance_accounts(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        upload_id integer NOT NULL REFERENCES public.trial_balance_uploads(id) ON DELETE CASCADE,
                        account_code text NOT NULL,source_account_code text,account_name text,parent_account_code text,
                        level integer NOT NULL DEFAULT 1,classification text NOT NULL CHECK(classification IN('parent','leaf')),
                        debit_amount real,credit_amount real,opening_dr real,opening_cr real,period_dr real,period_cr real,
                        closing_dr real,closing_cr real,source_account_type text,manually_overridden boolean NOT NULL DEFAULT false,
                        category_id integer,sort_order integer NOT NULL DEFAULT 0,raw_row_data jsonb,created_at timestamptz DEFAULT now(),
                        UNIQUE(upload_id,account_code));
                    CREATE TABLE public.account_control_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        client_id integer NOT NULL REFERENCES public.clients(id),account_code text NOT NULL,
                        verification_workstream text,financial_statement_category_id integer,note_category_id integer,
                        updated_by text,updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(client_id,account_code));
                    CREATE TABLE public.account_sets(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        client_id integer NOT NULL REFERENCES public.clients(id),set_key text NOT NULL,active_version_id integer,
                        is_active boolean NOT NULL DEFAULT true,created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(client_id,set_key));
                    CREATE TABLE public.account_set_versions(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        account_set_id integer NOT NULL REFERENCES public.account_sets(id) ON DELETE CASCADE,
                        version_number integer NOT NULL,source_upload_id integer NOT NULL REFERENCES public.trial_balance_uploads(id),
                        display_name text NOT NULL,description text,status text NOT NULL DEFAULT 'draft'
                            CHECK(status IN('draft','published','superseded')),
                        created_at timestamptz NOT NULL DEFAULT now(),published_at timestamptz,
                        UNIQUE(account_set_id,version_number));
                    ALTER TABLE public.account_sets ADD CONSTRAINT account_sets_active_version_fk
                        FOREIGN KEY(active_version_id) REFERENCES public.account_set_versions(id);
                    CREATE TABLE public.account_set_selectors(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        account_set_version_id integer NOT NULL REFERENCES public.account_set_versions(id) ON DELETE CASCADE,
                        selector_type text NOT NULL CHECK(selector_type IN('include_subtree','include_exact','exclude_subtree','exclude_exact')),
                        account_code text NOT NULL,sort_order integer NOT NULL DEFAULT 0,
                        UNIQUE(account_set_version_id,selector_type,account_code));
                    INSERT INTO public.trial_balance_uploads(id,client_id,as_of_date,file_name,status,row_count,period_end,period_type)
                        VALUES(30,10,'2026-11-01','trial-balance.xlsx','parsed',4,'2026-11-01','monthly'),
                              (40,20,'2026-11-01','other.xlsx','parsed',1,'2026-11-01','monthly');
                    INSERT INTO public.trial_balance_accounts(upload_id,account_code,account_name,level,classification,sort_order)
                        VALUES(30,'10-1','Vendors',1,'parent',1),(30,'10-1-1','Local vendor',2,'leaf',2),
                              (30,'10-1-2','Import vendor',2,'leaf',3),(30,'10-10-1','Boundary account',2,'leaf',4),
                              (40,'20-1','Other account',1,'leaf',1);
                    INSERT INTO public.account_control_assignments(client_id,account_code,financial_statement_category_id,note_category_id)
                        VALUES(10,'10-1-1',71,81);
                    """);
                await fixture.ApplyTenantFoundationAsync(Ct);
                await fixture.ApplyEngagementFoundationAsync(Ct);
                await fixture.ApplyTenantUserRolesAsync(Ct);
                await SqlAsync(connection, """
                    INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE
                        VALUES(101,'A','audit_firm'),(102,'B','business');
                    INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role)
                        VALUES(101,1,'admin'),(102,2,'admin');
                    INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id)
                        VALUES(10,101),(20,102);
                    """);
                pool = fixture.CreatePool();
                var postgresOptions = Options.Create(new PostgresOptions
                    { Enabled = true, ConnectionString = fixture.ConnectionString });
                var transactions = new PostgresTransactions(pool, postgresOptions);
                var logOptions = Options.Create(new LogPersistenceOptions());
                var audit = new PostgresAuditWriter(new LogSanitizer(logOptions), logOptions);
                var harness = new Harness(fixture, pool, transactions, audit, connection);
                await transactions.ExecuteAsync((db, ct) => TrialBalanceStorageStore.Authorize(db, harness.Owner, 10, false, ct));
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

        internal async Task<(int? Financial, int? Note, string? Workstream)> AssignmentAsync(string code)
        {
            await using var command = new NpgsqlCommand("""
                SELECT financial_statement_category_id,note_category_id,verification_workstream
                FROM public.account_control_assignments WHERE client_id=10 AND account_code=$1
                """, connection);
            command.Parameters.AddWithValue(code);
            await using var reader = await command.ExecuteReaderAsync(Ct);
            if (!await reader.ReadAsync(Ct)) return (null, null, null);
            return (reader.IsDBNull(0) ? null : reader.GetInt32(0), reader.IsDBNull(1) ? null : reader.GetInt32(1),
                reader.IsDBNull(2) ? null : reader.GetString(2));
        }

        public async ValueTask DisposeAsync()
        {
            await connection.DisposeAsync();
            await pool.DisposeAsync();
            await fixture.DisposeAsync();
        }

        internal async Task SqlAsync(string sql)
        {
            await using var command = new NpgsqlCommand(sql, connection);
            await command.ExecuteNonQueryAsync(Ct);
        }

        private static async Task SqlAsync(NpgsqlConnection connection, string sql)
        {
            await using var command = new NpgsqlCommand(sql, connection);
            await command.ExecuteNonQueryAsync(Ct);
        }
    }
}
