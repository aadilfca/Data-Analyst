using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Verification;
using AuditPlatform.Api.Controllers;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class VerificationRuntimeAcceptanceTests
{
    private static CancellationToken Ct => TestContext.Current.CancellationToken;

    [Fact]
    public async Task VerificationIsOffByDefaultAndRuntimeIsOwnerScopedForStartAndPreview()
    {
        var disabledOptions = Options.Create(new VerificationRuntimeOptions());
        var disabledController = new VerificationController(null!, null!, null!, null!, null!, null!, null!, disabledOptions)
        {
            ControllerContext = new ControllerContext { HttpContext = new DefaultHttpContext() }
        };
        var disabledResponse = await disabledController.Preview(10, "Bank", "2026-01-01", "2026-01-31", 1, 50, "cash", Ct);
        Assert.Equal(503, Assert.IsType<ObjectResult>(disabledResponse).StatusCode);

        await using var fixture = new PostgresFixture();
        await fixture.InitializeAsync();
        await fixture.ApplyAuditSchemaAsync(Ct);
        await using var connection = await fixture.OpenControlConnectionAsync(Ct);
        await Sql(connection, """
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
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS extract_date date;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS extract_amount numeric;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS is_deleted boolean NOT NULL DEFAULT false;
            ALTER TABLE public.client_data ADD COLUMN IF NOT EXISTS change_detection_status text;
            CREATE TABLE public.platform_audit_rule_state(tenant_id integer PRIMARY KEY,config_version bigint NOT NULL);
            CREATE TABLE public.platform_audit_rule_snapshots(tenant_id integer PRIMARY KEY,config_version bigint NOT NULL,
                schema_version integer NOT NULL,evaluator_version integer NOT NULL,payload jsonb NOT NULL);
            CREATE TABLE public.verification_sessions(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                client_id integer NOT NULL,cashbook_name text NOT NULL,period_start date NOT NULL,period_end date NOT NULL,
                created_by text NOT NULL,status text NOT NULL,total_transactions integer NOT NULL,session_type text NOT NULL,
                rulebook_version integer NOT NULL);
            CREATE TABLE public.verification_details(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                session_id integer NOT NULL,client_data_id integer NOT NULL,transaction_ref text,status text,
                validation_status text,range_status text NOT NULL,cleared_status text,matrix_version_applied integer);
            INSERT INTO public.trial_balance_uploads(id,client_id,as_of_date,file_name,status,row_count,period_end)
                VALUES(30,10,'2026-01-31','tb.xlsx','parsed',2,'2026-01-31');
            INSERT INTO public.trial_balance_accounts(upload_id,account_code,account_name,level,classification)
                VALUES(30,'B1','Bank',1,'leaf');
            INSERT INTO public.account_control_assignments(client_id,account_code,verification_workstream)
                VALUES(10,'B1','cash');
            INSERT INTO public.client_data(client_id,row_data,extract_date,extract_amount,change_detection_status) VALUES
                (10,'{"Voucher ID":"V1","Account Name":"Bank","Account Code":"B1","Debit":"100","Credit":"0"}',
                    '2026-01-10',100,'active'),
                (10,'{"Voucher ID":"V1","Account Name":"Expense","Account Code":"E1","Debit":"0","Credit":"100"}',
                    '2026-01-10',100,'active');
            INSERT INTO public.platform_audit_rule_state VALUES(101,1),(102,1);
            INSERT INTO public.platform_audit_rule_snapshots VALUES(101,1,2,1,'{}'),(102,1,2,1,'{}');
            """);
        await fixture.ApplyTenantFoundationAsync(Ct);
        await fixture.ApplyEngagementFoundationAsync(Ct);
        await fixture.ApplyTenantUserRolesAsync(Ct);
        await Sql(connection, """
            INSERT INTO public.platform_tenants(id,name,tenant_type) OVERRIDING SYSTEM VALUE
                VALUES(101,'A','audit_firm'),(102,'B','business');
            INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role)
                VALUES(101,1,'admin'),(102,2,'admin');
            INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES(10,101),(20,102);
            """);

        await using var pool = fixture.CreatePool();
        var transactions = new PostgresTransactions(pool, Options.Create(new PostgresOptions()));
        var logOptions = Options.Create(new LogPersistenceOptions());
        var audit = new PostgresAuditWriter(new LogSanitizer(logOptions), logOptions);
        var service = new VerificationSessionService(transactions, audit,
            Options.Create(new VerificationRuntimeOptions { Enabled = true }));
        var owner = Session('a', 1, "owner", 10, "Alpha", "A", 101);
        var otherTenant = Session('b', 2, "other", 20, "Beta", "B", 102);

        var started = await service.StartAsync(owner, new VerificationSessionStartRequest
        {
            ClientId = 10, CashbookName = "Bank", PeriodStart = "2026-01-01", PeriodEnd = "2026-01-31",
            SessionType = "cash"
        }, Ct);
        Assert.Equal("success", started.Status);
        int sessionId = started.Value!.Value.GetProperty("session_id").GetInt32();
        Assert.Equal(1, started.Value.Value.GetProperty("total_transactions").GetInt32());

        var preview = await service.PreviewAsync(owner, 10, "Bank", "2026-01-01", "2026-01-31", "cash", 1, 50, Ct);
        Assert.Equal("success", preview.Status);
        Assert.Equal(1, preview.Value!.Value.GetProperty("total_rows").GetInt32());
        Assert.Equal("forbidden", (await service.PreviewAsync(otherTenant, 10, "Bank", "2026-01-01",
            "2026-01-31", "cash", 1, 50, Ct)).Status);
        Assert.True(sessionId > 0);
    }

    private static VerifiedSession Session(char token, int user, string username, int client, string name,
        string code, int tenant) => new(new string(token, 64), user, username, "admin", DateTime.UtcNow.AddHours(1),
            client, name, code, true, tenant, 0);

    private static async Task Sql(NpgsqlConnection connection, string sql)
    { await using var command = new NpgsqlCommand(sql, connection); await command.ExecuteNonQueryAsync(Ct); }
}
