using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Reports;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ReportInputTests
{
    private static CancellationToken Ct => TestContext.Current.CancellationToken;

    [Fact]
    public async Task CaptureIsScopedCompleteAndConfigurationFingerprinted()
    {
        var fixture = new PostgresFixture();
        PostgresDataSourceFactory? pool = null;
        await using var fixtureLifetime = fixture;
        try
        {
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
                CREATE TABLE public.uploaded_files(id integer PRIMARY KEY,file_name text NOT NULL,upload_date timestamptz,
                    client_id integer NOT NULL,template_id integer NOT NULL);
                CREATE TABLE public.templates(id integer PRIMARY KEY,template_name text NOT NULL);
                ALTER TABLE public.uploaded_files ADD COLUMN status text NOT NULL DEFAULT 'Imported';
                ALTER TABLE public.uploaded_files ADD COLUMN ledger_account_code text;
                ALTER TABLE public.uploaded_files ADD COLUMN ledger_account_name text;
                ALTER TABLE public.uploaded_files ADD COLUMN ledger_period_start date;
                ALTER TABLE public.uploaded_files ADD COLUMN ledger_period_end date;
                ALTER TABLE public.uploaded_files ADD COLUMN ledger_evidence jsonb;
                ALTER TABLE public.templates ADD COLUMN is_active boolean NOT NULL DEFAULT true;
                ALTER TABLE public.templates ADD COLUMN data_purpose text NOT NULL DEFAULT 'standard';
                ALTER TABLE public.templates ADD COLUMN ledger_columns text NOT NULL DEFAULT '{}';
                CREATE TABLE public.master_columns(id integer PRIMARY KEY,column_name text NOT NULL,data_type text NOT NULL,is_active boolean NOT NULL DEFAULT true);
                CREATE TABLE public.template_column_mappings(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    template_id integer NOT NULL,column_id integer NOT NULL,sort_order integer NOT NULL DEFAULT 0);
                ALTER TABLE public.client_data ADD COLUMN template_id integer NOT NULL DEFAULT 1;
                ALTER TABLE public.client_data ADD COLUMN upload_id integer NOT NULL DEFAULT 1;
                ALTER TABLE public.client_data ADD COLUMN version integer NOT NULL DEFAULT 1;
                ALTER TABLE public.client_data ADD COLUMN source_fingerprint text;
                ALTER TABLE public.client_data ADD COLUMN extract_date date;
                ALTER TABLE public.client_data ADD COLUMN extract_amount real;
                ALTER TABLE public.client_data ADD COLUMN is_deleted boolean NOT NULL DEFAULT false;
                CREATE TABLE public.account_control_categories(
                    id integer PRIMARY KEY,client_id integer NOT NULL,category_type text NOT NULL,category_key text NOT NULL,
                    label text NOT NULL,statement_section text,presentation_role text,income_statement_role text,
                    row_type text NOT NULL DEFAULT 'category',parent_category_id integer,source_statement_category_id integer,
                    sort_order integer NOT NULL DEFAULT 0,is_active boolean NOT NULL DEFAULT true);
                CREATE TABLE public.account_control_assignments(
                    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id integer NOT NULL,account_code text NOT NULL,
                    verification_workstream text,financial_statement_category_id integer,note_category_id integer);
                CREATE TABLE public.account_control_statement_calculations(
                    id integer PRIMARY KEY,client_id integer NOT NULL,statement_section text NOT NULL,
                    calculation_type text NOT NULL,label text NOT NULL,parent_heading_id integer,sort_order integer NOT NULL DEFAULT 0);
                CREATE TABLE public.account_control_statement_calculation_components(
                    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,calculation_id integer NOT NULL,
                    source_category_id integer,source_calculation_id integer,operator text NOT NULL,sort_order integer NOT NULL DEFAULT 0);
                CREATE TABLE public.account_control_profit_links(
                    client_id integer PRIMARY KEY,source_calculation_id integer NOT NULL,target_category_id integer NOT NULL);
                CREATE TABLE public.account_control_cashflow_settings(client_id integer PRIMARY KEY,
                    match_internal_transfers boolean NOT NULL DEFAULT true,receipt_presentation text NOT NULL DEFAULT 'gross',
                    source_upload_id integer,source_template_id integer);
                CREATE TABLE public.account_control_cashflow_categories(id integer PRIMARY KEY,client_id integer NOT NULL,
                    category_key text NOT NULL,label text NOT NULL,sort_order integer NOT NULL DEFAULT 0,is_active boolean NOT NULL DEFAULT true);
                CREATE TABLE public.account_control_cashflow_category_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    client_id integer NOT NULL,account_code text NOT NULL,category_id integer NOT NULL);
                CREATE TABLE public.account_control_cashflow_assignments(client_id integer NOT NULL,account_code text NOT NULL,
                    section text NOT NULL,category_label text,cash_bank_role text NOT NULL,treatment_type text NOT NULL);
                CREATE TABLE public.account_control_cashflow_role_categories(client_id integer NOT NULL,role_key text NOT NULL,category_id integer NOT NULL);
                CREATE TABLE public.account_control_cashflow_treasury_accounts(client_id integer NOT NULL,account_code text NOT NULL,
                    treasury_type text NOT NULL,is_active boolean NOT NULL DEFAULT true);
                CREATE TABLE public.account_control_cashflow_source_mappings(client_id integer NOT NULL,template_id integer NOT NULL,
                    role text NOT NULL,template_column_id integer NOT NULL);
                CREATE TABLE public.trial_balance_profit_status(
                    upload_id integer PRIMARY KEY,client_id integer NOT NULL,closing_status text NOT NULL,context_key text NOT NULL);
                INSERT INTO public.account_control_categories(id,client_id,category_type,category_key,label)
                    VALUES(1,10,'financial_statement','cash','Cash');
                INSERT INTO public.account_control_assignments(client_id,account_code,financial_statement_category_id)
                    VALUES(10,'100',1);
                CREATE TABLE public.report_category_sets(id integer PRIMARY KEY,client_id integer NOT NULL,purpose_key text NOT NULL,set_name text NOT NULL,is_active boolean NOT NULL DEFAULT true);
                CREATE TABLE public.report_categories(id integer PRIMARY KEY,category_set_id integer NOT NULL,category_key text NOT NULL,label text NOT NULL,parent_category_key text,sort_order integer NOT NULL DEFAULT 0,is_active boolean NOT NULL DEFAULT true);
                CREATE TABLE public.report_account_category_assignments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,client_id integer NOT NULL,category_set_id integer NOT NULL,account_code text NOT NULL,category_id integer NOT NULL);
                INSERT INTO public.report_category_sets VALUES(1,10,'ledger_summary','Ledger Summary',true);
                INSERT INTO public.report_categories VALUES(1,1,'cash','Cash',NULL,1,true);
                INSERT INTO public.report_account_category_assignments(client_id,category_set_id,account_code,category_id) VALUES(10,1,'100',1);
                INSERT INTO public.account_control_cashflow_settings(client_id,source_upload_id,source_template_id) VALUES(10,1,1);
                INSERT INTO public.account_control_cashflow_categories(id,client_id,category_key,label) VALUES(11,10,'receipts','Receipts');
                INSERT INTO public.account_control_cashflow_category_assignments(client_id,account_code,category_id) VALUES(10,'100',11);
                INSERT INTO public.account_control_cashflow_treasury_accounts(client_id,account_code,treasury_type) VALUES(10,'B1','bank');
                INSERT INTO public.templates(id,template_name,data_purpose,ledger_columns) VALUES(2,'ledger','account_ledger','{}');
                INSERT INTO public.uploaded_files(id,file_name,client_id,template_id,status,ledger_account_code,
                    ledger_period_start,ledger_period_end,ledger_evidence)
                    VALUES(3,'bank-ledger.xlsx',10,2,'Imported','B1','2026-01-01','2026-01-31',
                    '{"balance_check":{"status":"consistent"}}');
                INSERT INTO public.master_columns(id,column_name,data_type) VALUES(1,'voucher','Text');
                INSERT INTO public.template_column_mappings(template_id,column_id,sort_order) VALUES(2,1,0);
                CREATE TABLE public.trial_balance_uploads(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    client_id integer NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,as_of_date date,file_name text,
                    status text NOT NULL DEFAULT 'pending',row_count integer NOT NULL DEFAULT 0,notes text,
                    uploaded_at timestamptz DEFAULT now(),period_start date,period_end date,period_type text,validation_controls text);
                CREATE TABLE public.trial_balance_accounts(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    upload_id integer NOT NULL REFERENCES public.trial_balance_uploads(id) ON DELETE CASCADE,
                    account_code text NOT NULL,source_account_code text,account_name text,parent_account_code text,
                    level integer NOT NULL DEFAULT 1,classification text NOT NULL,debit_amount real,credit_amount real,
                    opening_dr real,opening_cr real,period_dr real,period_cr real,closing_dr real,closing_cr real,
                    source_account_type text,manually_overridden boolean NOT NULL DEFAULT false,category_id integer,
                    sort_order integer NOT NULL DEFAULT 0,raw_row_data jsonb,UNIQUE(upload_id,account_code));
                CREATE TABLE public.trial_balance_rollforward_adjustments(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    upload_id integer NOT NULL REFERENCES public.trial_balance_uploads(id) ON DELETE CASCADE,
                    account_id integer NOT NULL REFERENCES public.trial_balance_accounts(id) ON DELETE CASCADE,
                    source_excel_row integer NOT NULL,expected_closing_net numeric NOT NULL,original_closing_net numeric NOT NULL,
                    difference_net numeric NOT NULL,closing_dr_adjustment numeric NOT NULL DEFAULT 0,
                    closing_cr_adjustment numeric NOT NULL DEFAULT 0,created_at timestamptz DEFAULT now(),UNIQUE(upload_id,account_id));
                INSERT INTO public.uploaded_files VALUES(1,'journal.xlsx',now(),10,1),(2,'foreign.xlsx',now(),20,1);
                INSERT INTO public.client_data(client_id,template_id,upload_id,row_data,extract_amount,extract_date,source_fingerprint)
                    VALUES(10,1,1,'{"account":"100","amount":"0.10"}'::jsonb,0.1,'2026-01-31','source-a'),
                          (20,1,2,'{"account":"999","amount":"9"}'::jsonb,9,'2026-01-31','foreign');
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
            string root = FindRoot();
            await Sql(connection, await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0018_trial_balance_storage.sql"),Ct));
            await Sql(connection, await File.ReadAllTextAsync(Path.Combine(root,"database","migrations","0019_trial_balance_review.sql"),Ct));
            await Sql(connection, """
                INSERT INTO public.trial_balance_uploads(id,client_id,as_of_date,file_name,status,row_count,period_end,validation_controls) OVERRIDING SYSTEM VALUE
                    VALUES(30,10,'2026-01-31','tb.xlsx','parsed',1,'2026-01-31','{"account_classifications":[["100","leaf"]],"global_adjustments":[]}');
                INSERT INTO public.trial_balance_accounts(upload_id,account_code,account_name,classification,
                    debit_amount,credit_amount,closing_dr,closing_cr,raw_row_data)
                    VALUES(30,'100','Cash','leaf',0.1,0,0.1,0,'{"source":"trial-balance"}'::jsonb);
                INSERT INTO public.client_data(client_id,template_id,upload_id,row_data,source_fingerprint,extract_date)
                    VALUES(10,2,3,'{"voucher":"V1"}'::jsonb,'ledger-fingerprint','2026-01-15');
                INSERT INTO public.trial_balance_rollforward_adjustments(
                    upload_id,account_id,source_excel_row,expected_closing_net,original_closing_net,difference_net,
                    closing_dr_adjustment,closing_cr_adjustment)
                SELECT 30,id,5,0.1,0.09,0.01,0.01,0 FROM public.trial_balance_accounts WHERE upload_id=30;
                """);

            pool = fixture.CreatePool();
            var pgOptions = Microsoft.Extensions.Options.Options.Create(new PostgresOptions
                { Enabled = true, ConnectionString = fixture.ConnectionString });
            var snapshots = new PostgresReadSnapshot(pool, pgOptions);
            var service = new ReportInputService(snapshots,
                Microsoft.Extensions.Options.Options.Create(new ReportInputOptions()));
            var owner = new VerifiedSession(new string('a',64),1,"owner","admin",DateTime.UtcNow.AddHours(1),
                10,"Alpha","A",true,101,0);
            var other = new VerifiedSession(new string('b',64),2,"other","admin",DateTime.UtcNow.AddHours(1),
                20,"Beta","B",true,102,0);
            var request = new ReportInputRequest { ClientId=10,Kind=ReportInputKind.FinancialStatements,
                UploadId=1,TrialBalanceUploadId=30 };

            var first = await service.CaptureAsync(owner,request,Ct);
            Assert.Equal("success",first.Status);
            Assert.NotNull(first.Snapshot);
            Assert.Single(first.Snapshot.ClientDataUploads.Items);
            Assert.Single(first.Snapshot.ClientData.Items);
            Assert.Equal("0.10000000149011612",first.Snapshot.ClientData.Items[0].ExtractAmount!.ExactValue);
            Assert.Equal("0.10",ReportAmountValue.From("0.10","numeric").ExactValue);
            Assert.Throws<FormatException>(()=>ReportAmountValue.From("1e1001","numeric"));
            Assert.True(first.Snapshot.ClientData.Complete);
            Assert.NotNull(first.Snapshot.TrialBalanceUploads.Items[0].ValidationControlsJson);
            Assert.Single(first.Snapshot.TrialBalanceAccounts.Items);
            Assert.Single(first.Snapshot.TrialBalanceAdjustments.Items);
            Assert.Equal("0.01",first.Snapshot.TrialBalanceAdjustments.Items[0].DifferenceNet.ExactValue);
            Assert.Single(first.Snapshot.AccountAssignments.Items);
            var ledgerRequest=new ReportInputRequest { ClientId=10,Kind=ReportInputKind.LedgerSource,UploadId=1,
                TemplateId=1,PeriodStart="2026-01-01",PeriodEnd="2026-01-31",CategorySource=ReportAccountCategorySource.Cashflow };
            var cashflowInputs=await service.CaptureAsync(owner,ledgerRequest,Ct);
            Assert.Equal("success",cashflowInputs.Status);
            Assert.Single(cashflowInputs.Snapshot!.CashflowLedgerEvidenceUploads.Items);
            Assert.Single(cashflowInputs.Snapshot.CashflowLedgerEvidenceRows.Items);
            Assert.Equal("B1",cashflowInputs.Snapshot.CashflowLedgerEvidenceRows.Items[0].AccountCode);
            Assert.Equal("forbidden",(await service.CaptureAsync(other,request,Ct)).Status);
            Assert.Equal("not_found",(await service.CaptureAsync(owner,request with {UploadId=2},Ct)).Status);

            var repeat = await service.CaptureAsync(owner,request,Ct);
            Assert.Equal(first.Snapshot.Identity.Fingerprint,repeat.Snapshot!.Identity.Fingerprint);
            await Sql(connection,"UPDATE public.account_control_categories SET label='Cash and equivalents' WHERE id=1");
            var changed = await service.CaptureAsync(owner,request,Ct);
            Assert.NotEqual(first.Snapshot.Identity.ConfigurationFingerprint,changed.Snapshot!.Identity.ConfigurationFingerprint);

            await Sql(connection,"UPDATE public.client_data SET is_deleted=true WHERE client_id=10");
            var empty = await service.CaptureAsync(owner,request with {UploadId=1},Ct);
            Assert.Equal("success",empty.Status);
            Assert.True(empty.Snapshot!.ClientData.Complete);
            Assert.Empty(empty.Snapshot.ClientData.Items);
        }
        finally
        {
            if (pool is not null) await pool.DisposeAsync();
        }
    }

    private static async Task Sql(NpgsqlConnection connection,string sql)
    { await using var command=new NpgsqlCommand(sql,connection); await command.ExecuteNonQueryAsync(Ct); }

    private static string FindRoot()
    {
        DirectoryInfo? directory=new(AppContext.BaseDirectory);
        while(directory is not null&&!File.Exists(Path.Combine(directory.FullName,"AuditPlatform.slnx"))) directory=directory.Parent;
        return directory?.FullName??throw new DirectoryNotFoundException("Could not find the .NET solution root.");
    }
}
