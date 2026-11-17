using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.TrialBalance;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Reports;

/// <summary>Captures bounded, typed report inputs without running calculations or writing data.</summary>
public sealed class ReportInputService(PostgresReadSnapshot snapshots, IOptions<ReportInputOptions> options)
{
    private sealed class CapacityExceededException : Exception;
    private sealed class CaptureBudget(int rows, int bytes)
    {
        public int Rows { get; private set; }
        public int Bytes { get; private set; }
        public void Add(int byteCount)
        {
            Rows = checked(Rows + 1);
            Bytes = checked(Bytes + byteCount);
            if (Rows > rows || Bytes > bytes) throw new CapacityExceededException();
        }
    }

    public async Task<ReportInputCaptureResult> CaptureAsync(VerifiedSession? session,
        ReportInputRequest request, CancellationToken ct = default)
    {
        string? invalid = null;
        if (request is null || !ValidRequest(request, out var normalized, out invalid))
            return new("invalid", Code: invalid ?? "report_input_request_invalid");
        if (!options.Value.Valid()) return new("unavailable", Code: "report_input_configuration");

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            return await snapshots.ExecuteAsync(async (db, token) =>
            {
                _ = await TrialBalanceStorageStore.Authorize(db, session, normalized.ClientId, false, token);
                if (normalized.UploadId is { } requestedUpload
                    && !await ClientUploadExistsAsync(db, normalized.ClientId, requestedUpload, token))
                    return new ReportInputCaptureResult("not_found", Code: "report_input_upload_not_found");
                var budget = new CaptureBudget(options.Value.MaximumRows, options.Value.MaximumBytes);
                var clientDataUploads = Empty<ReportLedgerUpload>();
                var clientData = Empty<ReportClientDataRow>();
                var uploads = Empty<ReportTrialBalanceUpload>();
                var accounts = Empty<ReportTrialBalanceAccount>();
                var trialBalanceAdjustments = Empty<ReportTrialBalanceAdjustment>();
                var assignments = Empty<ReportAccountAssignment>();
                var mappings = Empty<ReportCategoryMapping>();
                var categories = Empty<ReportCategory>();
                var calculations = Empty<ReportStatementCalculation>();
                var components = Empty<ReportStatementComponent>();
                var profitLinks = Empty<ReportProfitLink>();
                var profitStatuses = Empty<ReportProfitStatus>();
                var cashflowSettings = Empty<ReportCashflowSettings>();
                var cashflowAssignments = Empty<ReportCashflowAccountAssignment>();
                var cashflowRoleCategories = Empty<ReportCashflowRoleCategory>();
                var treasuryAccounts = Empty<ReportCashflowTreasuryAccount>();
                var columnMappings = Empty<ReportCashflowColumnMapping>();
                var ledgerEvidenceUploads = Empty<ReportCashflowLedgerEvidenceUpload>();
                var ledgerEvidenceRows = Empty<ReportCashflowLedgerEvidenceRow>();

                var captureRequest = normalized;
                long? cashflowReportRevision = null;
                if (normalized.Kind == ReportInputKind.LedgerSource
                    && normalized.CategorySource == ReportAccountCategorySource.Cashflow)
                {
                    cashflowSettings = await ReadCashflowSettingsAsync(db, normalized.ClientId, budget, token);
                    var selectedSource = cashflowSettings.Items.SingleOrDefault();
                    if (selectedSource?.SourceUploadId is null && selectedSource?.SourceTemplateId is null)
                        return new ReportInputCaptureResult("unavailable", Code: "cashflow_source_unconfigured");
                    if (normalized.UploadId is { } requestedSourceUpload && requestedSourceUpload != selectedSource.SourceUploadId
                        || normalized.TemplateId is { } requestedTemplate && requestedTemplate != selectedSource.SourceTemplateId)
                        return new ReportInputCaptureResult("conflict", Code: "cashflow_source_selection_changed");
                    captureRequest = normalized with
                    {
                        UploadId = selectedSource.SourceUploadId,
                        TemplateId = selectedSource.SourceTemplateId
                    };
                    cashflowReportRevision = await ReadCashflowReportRevisionAsync(db, normalized.ClientId, token);
                }
                clientDataUploads = await ResolveClientDataUploadsAsync(db, captureRequest, budget, token);
                clientData = normalized.MetadataOnly
                    ? Complete<ReportClientDataRow>([], 0)
                    : await ReadClientDataAsync(db, captureRequest, clientDataUploads.Items, budget, token);

                bool captureTrialBalance = normalized.Kind == ReportInputKind.FinancialStatements
                    || normalized.IncludeLatestTrialBalance
                    || normalized.TrialBalanceUploadId is not null;
                uploads = captureTrialBalance
                    ? await ReadTrialBalanceUploadAsync(db, normalized.ClientId,
                        normalized.TrialBalanceUploadId, budget, token, true)
                    : Empty<ReportTrialBalanceUpload>();
                int? trialBalanceUploadId = uploads.Items.SingleOrDefault()?.Id;
                if (normalized.TrialBalanceUploadId is not null && trialBalanceUploadId is null)
                    return new ReportInputCaptureResult("not_found", Code: "report_input_trial_balance_not_found");
                if (trialBalanceUploadId is not null)
                {
                    accounts = await ReadTrialBalanceAccountsAsync(db, normalized.ClientId,
                        trialBalanceUploadId.Value, budget, token, true);
                    trialBalanceAdjustments = await ReadTrialBalanceAdjustmentsAsync(db, normalized.ClientId,
                        trialBalanceUploadId.Value, budget, token);
                    if (normalized.SelectedAccountCodes is not null
                        && normalized.SelectedAccountCodes.Except(accounts.Items
                            .Where(row => string.Equals(row.Classification, "leaf", StringComparison.OrdinalIgnoreCase))
                            .Select(row => row.AccountCode), StringComparer.Ordinal).Any())
                        return new ReportInputCaptureResult("invalid", Code: "report_input_account_selection_invalid");
                }
                else if (normalized.SelectedAccountCodes is not null)
                    return new ReportInputCaptureResult("invalid", Code: "report_input_account_selection_invalid");

                if (normalized.Kind == ReportInputKind.FinancialStatements)
                {
                    assignments = await ReadStatementAssignmentsAsync(db, normalized.ClientId, budget, token);
                    categories = await ReadStatementCategoriesAsync(db, normalized.ClientId, budget, token);
                    calculations = await ReadStatementCalculationsAsync(db, normalized.ClientId, budget, token);
                    components = await ReadStatementComponentsAsync(db, normalized.ClientId, budget, token);
                    profitLinks = await ReadProfitLinksAsync(db, normalized.ClientId, budget, token);
                    if (trialBalanceUploadId is not null)
                        profitStatuses = await ReadProfitStatusesAsync(db, normalized.ClientId,
                            trialBalanceUploadId.Value, budget, token);
                }
                else
                {
                    var categorySource = normalized.CategorySource;
                    mappings = await ReadCategoryMappingsAsync(db, normalized.ClientId, categorySource, budget, token);
                    categories = await ReadReportCategoriesAsync(db, normalized.ClientId, categorySource, budget, token);
                    if (categorySource == ReportAccountCategorySource.Cashflow)
                    {
                        // Vendor payment schedules use the same saved verification workstream
                        // assignments as the Python report; never infer vendors from captions.
                        assignments = await ReadStatementAssignmentsAsync(db, normalized.ClientId, budget, token);
                        cashflowAssignments = await ReadCashflowAssignmentsAsync(db, normalized.ClientId, budget, token);
                        cashflowRoleCategories = await ReadCashflowRoleCategoriesAsync(db, normalized.ClientId, budget, token);
                        treasuryAccounts = await ReadTreasuryAccountsAsync(db, normalized.ClientId, budget, token);
                        columnMappings = await ReadCashflowColumnMappingsAsync(db, normalized.ClientId,
                            captureRequest.TemplateId, budget, token);
                        string[] treasuryCodes = treasuryAccounts.Items.Select(row => row.AccountCode)
                            .Distinct(StringComparer.Ordinal).ToArray();
                        ledgerEvidenceUploads = await ReadCashflowLedgerEvidenceUploadsAsync(db, normalized.ClientId,
                            treasuryCodes, budget, token);
                        ledgerEvidenceRows = await ReadCashflowLedgerEvidenceRowsAsync(db, normalized.ClientId,
                            ledgerEvidenceUploads.Items, budget, token);
                    }
                }

                DateTime capturedAt = await CaptureTimestampAsync(db, token);
                var scope = new ReportInputScope(normalized.ClientId, normalized.Kind, normalized.UploadId,
                    captureRequest.TemplateId, clientDataUploads.Items.Select(row => row.Id).ToArray(),
                    trialBalanceUploadId, normalized.PeriodStart, normalized.PeriodEnd,
                    normalized.Kind == ReportInputKind.FinancialStatements ? null : normalized.CategorySource,
                    normalized.SelectedAccountCodes, normalized.TreasuryView)
                { IncludeVoucherContext=normalized.IncludeVoucherContext };
                string configurationFingerprint = Hash(new
                {
                    scope.ClientId, scope.Kind,
                    TrialBalanceUploadId = normalized.IncludeLatestTrialBalance ? null : scope.TrialBalanceUploadId,
                    scope.CategorySource,
                    TrialBalanceUploads = normalized.IncludeLatestTrialBalance ? Array.Empty<ReportTrialBalanceUpload>() : uploads.Items,
                    Accounts = normalized.IncludeLatestTrialBalance ? Array.Empty<ReportTrialBalanceAccount>() : accounts.Items,
                    TrialBalanceAdjustments = normalized.IncludeLatestTrialBalance ? Array.Empty<ReportTrialBalanceAdjustment>() : trialBalanceAdjustments.Items,
                    Assignments = assignments.Items,
                    CategoryMappings = mappings.Items, Categories = categories.Items,
                    Calculations = calculations.Items, Components = components.Items,
                    ProfitLinks = profitLinks.Items, ProfitStatuses = profitStatuses.Items,
                    CashflowSettings = cashflowSettings.Items, CashflowAssignments = cashflowAssignments.Items,
                    CashflowRoleCategories = cashflowRoleCategories.Items, TreasuryAccounts = treasuryAccounts.Items,
                    CashflowColumnMappings = columnMappings.Items,
                    CashflowLedgerEvidenceUploads = ledgerEvidenceUploads.Items,
                    CashflowLedgerEvidenceRows = ledgerEvidenceRows.Items
                });
                string fingerprint = Hash(new
                {
                    Scope = scope, ClientDataUploads = clientDataUploads.Items, ClientData = clientData.Items,
                    Configuration = configurationFingerprint
                });
                var identity = new ReportInputIdentity("report-input-v1", fingerprint,
                    configurationFingerprint, DateTime.SpecifyKind(capturedAt, DateTimeKind.Utc));
                var snapshot = new ReportInputSnapshot(scope, identity, clientDataUploads, clientData, uploads,
                    accounts, trialBalanceAdjustments, assignments, mappings, categories, calculations, components, profitLinks,
                    profitStatuses, cashflowSettings, cashflowAssignments, cashflowRoleCategories,
                    treasuryAccounts, columnMappings)
                {
                    CashflowLedgerEvidenceUploads = ledgerEvidenceUploads,
                    CashflowLedgerEvidenceRows = ledgerEvidenceRows,
                    CashflowReportRevision = cashflowReportRevision
                };
                return new ReportInputCaptureResult("success", snapshot);
            }, deadline.Token);
        }
        catch (CapacityExceededException) { return new("too_large", Code: "report_input_capacity"); }
        catch (TrialBalanceStorageRejected error) { return new(error.Status, Code: error.Code); }
        catch (OperationCanceledException)
        { return new(ct.IsCancellationRequested ? "cancelled" : "timeout", Code: "report_input_timeout"); }
        catch (PostgresException error) when (error.SqlState is "40001" or "40P01" or "55P03")
        { return new("conflict", Code: "report_input_concurrent_change"); }
        catch (FormatException)
        { return new("invalid", Code: "report_input_amount_invalid"); }
        catch (Exception error) when (error is NpgsqlException or TimeoutException)
        { return new("unavailable", Code: "report_input_source_unavailable"); }
    }

    private async Task<ReportInputDataset<ReportLedgerUpload>> ResolveClientDataUploadsAsync(
        PostgresTransactionContext db, ReportInputRequest request, CaptureBudget budget, CancellationToken ct)
    {
        string predicate = request.UploadId is not null ? "f.id=$2"
            : request.TemplateId is not null ? "f.template_id=$2" : "TRUE";
        bool chooseLatestPerFile = request.TemplateId is not null;
        string limit = chooseLatestPerFile ? "$3" : request.UploadId is null && request.TemplateId is null ? "$2" : "$3";
        string sql = chooseLatestPerFile ? $"""
            WITH eligible AS (
                SELECT f.id,f.client_id,f.template_id,f.file_name,f.upload_date,
                       ROW_NUMBER() OVER (PARTITION BY COALESCE(NULLIF(BTRIM(f.file_name),''),f.id::text)
                           ORDER BY f.upload_date DESC NULLS LAST,f.id DESC) AS rank
                FROM public.uploaded_files f
                WHERE f.client_id=$1 AND {predicate}
                  AND EXISTS (SELECT 1 FROM public.client_data cd WHERE cd.upload_id=f.id
                      AND cd.client_id=$1 AND cd.template_id=f.template_id AND cd.is_deleted IS FALSE)
            )
            SELECT id,client_id,template_id,file_name,upload_date FROM eligible WHERE rank=1
            ORDER BY upload_date DESC NULLS LAST,id DESC LIMIT {limit}
            """ : $"""
            SELECT f.id,f.client_id,f.template_id,f.file_name,f.upload_date
            FROM public.uploaded_files f
            WHERE f.client_id=$1 AND {predicate}
              AND EXISTS (SELECT 1 FROM public.client_data cd WHERE cd.upload_id=f.id
                  AND cd.client_id=$1 AND cd.template_id=f.template_id AND cd.is_deleted IS FALSE)
            ORDER BY f.upload_date DESC NULLS LAST,f.id DESC LIMIT {limit}
            """;
        await using var command = db.CreateCommand(sql);
        command.Parameters.AddWithValue(request.ClientId);
        if (request.UploadId is { } upload) command.Parameters.AddWithValue(upload);
        else if (request.TemplateId is { } template) command.Parameters.AddWithValue(template);
        command.Parameters.AddWithValue(options.Value.MaximumRows + 1);
        return await ReadAsync(command, reader => new ReportLedgerUpload(reader.GetInt32(0), reader.GetInt32(1),
            reader.GetInt32(2), reader.GetString(3), NullableDateTime(reader, 4)), budget, ct);
    }

    private async Task<ReportInputDataset<ReportClientDataRow>> ReadClientDataAsync(
        PostgresTransactionContext db, ReportInputRequest request, IReadOnlyList<ReportLedgerUpload> uploads,
        CaptureBudget budget, CancellationToken ct)
    {
        if (uploads.Count == 0) return Complete<ReportClientDataRow>([], 0);
        var filters = new List<string> { "cd.client_id=$1", "cd.is_deleted IS FALSE", "cd.upload_id=ANY($2)" };
        string PeriodFilter(string alias)
        {
            var clauses=new List<string>();
            if(request.PeriodStart is not null)clauses.Add($"{alias}.extract_date >= $3::date");
            if(request.PeriodEnd is not null)clauses.Add($"{alias}.extract_date < ${(request.PeriodStart is null ? 3 : 4)}::date + INTERVAL '1 day'");
            return clauses.Count==0?"TRUE":string.Join(" AND ",clauses);
        }
        if(request.IncludeVoucherContext)
            filters.Add($"({PeriodFilter("cd")} OR cd.derived_voucher_id IN (SELECT DISTINCT context.derived_voucher_id FROM public.client_data context WHERE context.client_id=$1 AND context.is_deleted IS FALSE AND context.upload_id=ANY($2) AND {PeriodFilter("context")} AND NULLIF(BTRIM(context.derived_voucher_id),'') IS NOT NULL))");
        else if(request.PeriodStart is not null||request.PeriodEnd is not null)filters.Add(PeriodFilter("cd"));
        int capPosition = (request.PeriodStart is not null ? 1 : 0) + (request.PeriodEnd is not null ? 1 : 0) + 3;
        string sql = $"""
            SELECT id,template_id,upload_id,version,source_fingerprint,extract_date,
                   extract_amount::double precision::text,row_data::text
            FROM public.client_data cd WHERE {string.Join(" AND ", filters)}
            ORDER BY cd.extract_date NULLS LAST,cd.id LIMIT ${capPosition}
            """;
        await using var command = db.CreateCommand(sql);
        command.Parameters.AddWithValue(request.ClientId);
        command.Parameters.AddWithValue(uploads.Select(row => row.Id).ToArray());
        if (request.PeriodStart is not null) command.Parameters.AddWithValue(DateOnly.ParseExact(request.PeriodStart, "yyyy-MM-dd"));
        if (request.PeriodEnd is not null) command.Parameters.AddWithValue(DateOnly.ParseExact(request.PeriodEnd, "yyyy-MM-dd"));
        command.Parameters.AddWithValue(options.Value.MaximumRows + 1);
        return await ReadAsync(command, reader => new ReportClientDataRow(reader.GetInt32(0), reader.GetInt32(1),
            reader.GetInt32(2), reader.GetInt32(3), NullableString(reader, 4), NullableDateOnly(reader, 5),
            Amount(reader, 6, "real"), reader.GetString(7)), budget, ct);
    }

    private async Task<ReportInputDataset<ReportTrialBalanceUpload>> ReadTrialBalanceUploadAsync(
        PostgresTransactionContext db, int clientId, int? uploadId, CaptureBudget budget, CancellationToken ct,
        bool requested)
    {
        string sql = """
            SELECT id,client_id,as_of_date,file_name,period_start,period_end,period_type,uploaded_at,validation_controls::text
            FROM public.trial_balance_uploads WHERE client_id=$1 AND status='parsed'
              AND ($2::integer IS NULL OR id=$2)
            ORDER BY period_end DESC NULLS LAST,uploaded_at DESC NULLS LAST,id DESC LIMIT $3
            """;
        await using var command = db.CreateCommand(sql);
        command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue((object?)uploadId ?? DBNull.Value);
        command.Parameters.AddWithValue(options.Value.MaximumRows + 1);
        return await ReadAsync(command, r => new ReportTrialBalanceUpload(r.GetInt32(0), r.GetInt32(1),
            DateOnly.FromDateTime(r.GetDateTime(2)), r.GetString(3), NullableDateOnly(r, 4), NullableDateOnly(r, 5),
            NullableString(r, 6), NullableDateTime(r, 7), NullableString(r, 8)), budget, ct, requested);
    }

    private async Task<ReportInputDataset<ReportTrialBalanceAccount>> ReadTrialBalanceAccountsAsync(
        PostgresTransactionContext db, int clientId, int uploadId, CaptureBudget budget, CancellationToken ct, bool requested)
    {
        const string sql = """
            SELECT a.id,a.upload_id,a.account_code,a.source_account_code,a.account_name,a.parent_account_code,
                   a.level,a.classification,a.manually_overridden,a.sort_order,a.raw_row_data::text,
                   a.debit_amount::double precision::text,a.credit_amount::double precision::text,
                   a.opening_dr::double precision::text,a.opening_cr::double precision::text,
                   a.period_dr::double precision::text,a.period_cr::double precision::text,
                   a.closing_dr::double precision::text,a.closing_cr::double precision::text
            FROM public.trial_balance_accounts a JOIN public.trial_balance_uploads u ON u.id=a.upload_id
            WHERE u.client_id=$1 AND a.upload_id=$2 ORDER BY a.sort_order,a.id LIMIT $3
            """;
        await using var command = db.CreateCommand(sql);
        command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(uploadId);
        command.Parameters.AddWithValue(options.Value.MaximumRows + 1);
        return await ReadAsync(command, r => new ReportTrialBalanceAccount(r.GetInt32(0),r.GetInt32(1),r.GetString(2),
            NullableString(r,3),NullableString(r,4),NullableString(r,5),r.GetInt32(6),r.GetString(7),r.GetBoolean(8),
            r.GetInt32(9),NullableString(r,10),Amount(r,11,"real"),Amount(r,12,"real"),Amount(r,13,"real"),
            Amount(r,14,"real"),Amount(r,15,"real"),Amount(r,16,"real"),Amount(r,17,"real"),Amount(r,18,"real")), budget, ct, requested);
    }

    private async Task<ReportInputDataset<ReportTrialBalanceAdjustment>> ReadTrialBalanceAdjustmentsAsync(
        PostgresTransactionContext db, int clientId, int uploadId, CaptureBudget budget, CancellationToken ct)
    {
        const string sql = """
            SELECT a.id,a.upload_id,a.account_id,a.source_excel_row,a.expected_closing_net::text,
                   a.original_closing_net::text,a.difference_net::text,a.closing_dr_adjustment::text,
                   a.closing_cr_adjustment::text
            FROM public.trial_balance_rollforward_adjustments a
            JOIN public.trial_balance_uploads u ON u.id=a.upload_id
            WHERE u.client_id=$1 AND a.upload_id=$2 ORDER BY a.id LIMIT $3
            """;
        await using var command=db.CreateCommand(sql);
        command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(uploadId);
        command.Parameters.AddWithValue(options.Value.MaximumRows+1);
        return await ReadAsync(command,r=>new ReportTrialBalanceAdjustment(r.GetInt32(0),r.GetInt32(1),
            r.GetInt32(2),r.GetInt32(3),ReportAmountValue.From(r.GetString(4),"numeric"),
            ReportAmountValue.From(r.GetString(5),"numeric"),ReportAmountValue.From(r.GetString(6),"numeric"),
            ReportAmountValue.From(r.GetString(7),"numeric"),ReportAmountValue.From(r.GetString(8),"numeric")),budget,ct);
    }

    private Task<ReportInputDataset<ReportAccountAssignment>> ReadStatementAssignmentsAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportAccountAssignment>(db,"SELECT client_id,account_code,verification_workstream,financial_statement_category_id,note_category_id FROM public.account_control_assignments WHERE client_id=$1 ORDER BY account_code LIMIT $2",[client],r=>new ReportAccountAssignment(r.GetInt32(0),r.GetString(1),NullableString(r,2),NullableInt(r,3),NullableInt(r,4)),b,ct);

    private Task<ReportInputDataset<ReportCategory>> ReadStatementCategoriesAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportCategory>(db,"SELECT id,client_id,category_type,category_key,label,statement_section,presentation_role,income_statement_role,row_type,parent_category_id,source_statement_category_id,sort_order,is_active FROM public.account_control_categories WHERE client_id=$1 ORDER BY sort_order,id LIMIT $2",[client],r=>new ReportCategory(r.GetInt32(0),r.GetInt32(1),r.GetString(2),r.GetString(3),r.GetString(4),NullableString(r,5),NullableString(r,6),NullableString(r,7),r.GetString(8),NullableInt(r,9),NullableInt(r,10),r.GetInt32(11),r.GetBoolean(12)),b,ct);

    private Task<ReportInputDataset<ReportStatementCalculation>> ReadStatementCalculationsAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportStatementCalculation>(db,"SELECT id,client_id,statement_section,calculation_type,label,parent_heading_id,sort_order FROM public.account_control_statement_calculations WHERE client_id=$1 ORDER BY sort_order,id LIMIT $2",[client],r=>new ReportStatementCalculation(r.GetInt32(0),r.GetInt32(1),r.GetString(2),r.GetString(3),r.GetString(4),NullableInt(r,5),r.GetInt32(6)),b,ct);

    private Task<ReportInputDataset<ReportStatementComponent>> ReadStatementComponentsAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportStatementComponent>(db,"SELECT c.calculation_id,c.source_category_id,c.source_calculation_id,c.operator,c.sort_order FROM public.account_control_statement_calculation_components c JOIN public.account_control_statement_calculations h ON h.id=c.calculation_id WHERE h.client_id=$1 ORDER BY c.calculation_id,c.sort_order,c.id LIMIT $2",[client],r=>new ReportStatementComponent(r.GetInt32(0),NullableInt(r,1),NullableInt(r,2),r.GetString(3),r.GetInt32(4)),b,ct);

    private Task<ReportInputDataset<ReportProfitLink>> ReadProfitLinksAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportProfitLink>(db,"SELECT client_id,source_calculation_id,target_category_id FROM public.account_control_profit_links WHERE client_id=$1 ORDER BY client_id LIMIT $2",[client],r=>new ReportProfitLink(r.GetInt32(0),r.GetInt32(1),r.GetInt32(2)),b,ct);

    private Task<ReportInputDataset<ReportProfitStatus>> ReadProfitStatusesAsync(PostgresTransactionContext db,int client,int upload,CaptureBudget b,CancellationToken ct) =>
        Query<ReportProfitStatus>(db,"SELECT upload_id,client_id,closing_status,context_key FROM public.trial_balance_profit_status WHERE client_id=$1 AND upload_id=$2 ORDER BY upload_id LIMIT $3",[client,upload],r=>new ReportProfitStatus(r.GetInt32(0),r.GetInt32(1),r.GetString(2),r.GetString(3)),b,ct);

    private Task<ReportInputDataset<ReportCategoryMapping>> ReadCategoryMappingsAsync(PostgresTransactionContext db,int client,ReportAccountCategorySource source,CaptureBudget b,CancellationToken ct) => source switch
    {
        ReportAccountCategorySource.FinancialStatement => Query<ReportCategoryMapping>(db,"SELECT client_id,account_code,'financial_statement',financial_statement_category_id,NULL::integer,NULL::text,NULL::text FROM public.account_control_assignments WHERE client_id=$1 AND financial_statement_category_id IS NOT NULL ORDER BY account_code LIMIT $2",[client],r=>new ReportCategoryMapping(r.GetInt32(0),r.GetString(1),r.GetString(2),NullableInt(r,3),NullableInt(r,4),NullableString(r,5),NullableString(r,6)),b,ct),
        ReportAccountCategorySource.Cashflow => Query<ReportCategoryMapping>(db,"SELECT a.client_id,a.account_code,'cashflow',c.id,NULL::integer,c.category_key,c.label FROM public.account_control_cashflow_category_assignments a JOIN public.account_control_cashflow_categories c ON c.id=a.category_id WHERE a.client_id=$1 ORDER BY a.account_code LIMIT $2",[client],r=>new ReportCategoryMapping(r.GetInt32(0),r.GetString(1),r.GetString(2),NullableInt(r,3),NullableInt(r,4),NullableString(r,5),NullableString(r,6)),b,ct),
        _ => Query<ReportCategoryMapping>(db,"SELECT a.client_id,a.account_code,'ledger_summary',c.id,s.id,c.category_key,c.label FROM public.report_account_category_assignments a JOIN public.report_categories c ON c.id=a.category_id JOIN public.report_category_sets s ON s.id=a.category_set_id WHERE a.client_id=$1 ORDER BY a.account_code,s.id LIMIT $2",[client],r=>new ReportCategoryMapping(r.GetInt32(0),r.GetString(1),r.GetString(2),NullableInt(r,3),NullableInt(r,4),NullableString(r,5),NullableString(r,6)),b,ct)
    };

    private Task<ReportInputDataset<ReportCategory>> ReadReportCategoriesAsync(PostgresTransactionContext db,int client,ReportAccountCategorySource source,CaptureBudget b,CancellationToken ct) => source switch
    {
        ReportAccountCategorySource.FinancialStatement => ReadStatementCategoriesAsync(db,client,b,ct),
        ReportAccountCategorySource.Cashflow => Query<ReportCategory>(db,"SELECT c.id,c.client_id,'cashflow',c.category_key,c.label,NULL::text,NULL::text,NULL::text,'category',NULL::integer,NULL::integer,c.sort_order,c.is_active FROM public.account_control_cashflow_categories c WHERE c.client_id=$1 ORDER BY c.sort_order,c.id LIMIT $2",[client],r=>new ReportCategory(r.GetInt32(0),r.GetInt32(1),r.GetString(2),r.GetString(3),r.GetString(4),NullableString(r,5),NullableString(r,6),NullableString(r,7),r.GetString(8),NullableInt(r,9),NullableInt(r,10),r.GetInt32(11),r.GetBoolean(12)),b,ct),
        _ => Query<ReportCategory>(db,"SELECT c.id,s.client_id,'ledger_summary',c.category_key,c.label,NULL::text,NULL::text,NULL::text,'category',NULL::integer,NULL::integer,c.sort_order,c.is_active FROM public.report_categories c JOIN public.report_category_sets s ON s.id=c.category_set_id WHERE s.client_id=$1 ORDER BY s.id,c.sort_order,c.id LIMIT $2",[client],r=>new ReportCategory(r.GetInt32(0),r.GetInt32(1),r.GetString(2),r.GetString(3),r.GetString(4),NullableString(r,5),NullableString(r,6),NullableString(r,7),r.GetString(8),NullableInt(r,9),NullableInt(r,10),r.GetInt32(11),r.GetBoolean(12)),b,ct)
    };

    private Task<ReportInputDataset<ReportCashflowSettings>> ReadCashflowSettingsAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportCashflowSettings>(db,"SELECT client_id,match_internal_transfers,receipt_presentation,source_upload_id,source_template_id FROM public.account_control_cashflow_settings WHERE client_id=$1 ORDER BY client_id LIMIT $2",[client],r=>new ReportCashflowSettings(r.GetInt32(0),r.GetBoolean(1),r.GetString(2),NullableInt(r,3),NullableInt(r,4)),b,ct);

    private static async Task<long> ReadCashflowReportRevisionAsync(PostgresTransactionContext db, int client, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT COALESCE((SELECT revision FROM public.cashflow_report_revisions WHERE client_id=$1),1)");
        command.Parameters.AddWithValue(client);
        object? value = await command.ExecuteScalarAsync(ct);
        return Convert.ToInt64(value, CultureInfo.InvariantCulture);
    }

    private Task<ReportInputDataset<ReportCashflowAccountAssignment>> ReadCashflowAssignmentsAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportCashflowAccountAssignment>(db,"SELECT client_id,account_code,section,category_label,cash_bank_role,treatment_type FROM public.account_control_cashflow_assignments WHERE client_id=$1 ORDER BY account_code LIMIT $2",[client],r=>new ReportCashflowAccountAssignment(r.GetInt32(0),r.GetString(1),r.GetString(2),NullableString(r,3),r.GetString(4),r.GetString(5)),b,ct);

    private Task<ReportInputDataset<ReportCashflowRoleCategory>> ReadCashflowRoleCategoriesAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportCashflowRoleCategory>(db,"SELECT client_id,role_key,category_id FROM public.account_control_cashflow_role_categories WHERE client_id=$1 ORDER BY role_key,category_id LIMIT $2",[client],r=>new ReportCashflowRoleCategory(r.GetInt32(0),r.GetString(1),r.GetInt32(2)),b,ct);

    private Task<ReportInputDataset<ReportCashflowTreasuryAccount>> ReadTreasuryAccountsAsync(PostgresTransactionContext db,int client,CaptureBudget b,CancellationToken ct) =>
        Query<ReportCashflowTreasuryAccount>(db,"SELECT account_code,treasury_type FROM public.account_control_cashflow_treasury_accounts WHERE client_id=$1 AND is_active IS TRUE ORDER BY account_code,treasury_type LIMIT $2",[client],r=>new ReportCashflowTreasuryAccount(r.GetString(0),r.GetString(1)),b,ct);

    private Task<ReportInputDataset<ReportCashflowColumnMapping>> ReadCashflowColumnMappingsAsync(PostgresTransactionContext db,int client,int? template,CaptureBudget b,CancellationToken ct) =>
        Query<ReportCashflowColumnMapping>(db,"SELECT m.template_id,m.role,c.column_name FROM public.account_control_cashflow_source_mappings m JOIN public.master_columns c ON c.id=m.template_column_id WHERE m.client_id=$1 AND ($2::integer IS NULL OR m.template_id=$2) ORDER BY m.template_id,m.role LIMIT $3",[client,(object?)template??DBNull.Value],r=>new ReportCashflowColumnMapping(r.GetInt32(0),r.GetString(1),r.GetString(2)),b,ct);

    private Task<ReportInputDataset<ReportCashflowLedgerEvidenceUpload>> ReadCashflowLedgerEvidenceUploadsAsync(
        PostgresTransactionContext db, int client, string[] treasuryCodes, CaptureBudget budget, CancellationToken ct) =>
        treasuryCodes.Length == 0 ? Task.FromResult(Complete<ReportCashflowLedgerEvidenceUpload>([], 0))
        : Query<ReportCashflowLedgerEvidenceUpload>(db, """
            SELECT u.id,u.template_id,u.ledger_account_code,u.ledger_account_name,
                   u.ledger_period_start,u.ledger_period_end,u.ledger_evidence::text,
                   t.ledger_columns::text,
                   COALESCE((SELECT json_agg(json_build_object('id',m.id,'column_name',m.column_name,'data_type',m.data_type)
                       ORDER BY p.sort_order)::text
                       FROM public.template_column_mappings p JOIN public.master_columns m
                         ON m.id=p.column_id AND m.is_active IS TRUE WHERE p.template_id=t.id),'[]')
            FROM public.uploaded_files u JOIN public.templates t ON t.id=u.template_id
            WHERE u.client_id=$1 AND u.status='Imported' AND u.ledger_account_code=ANY($2)
              AND t.data_purpose='account_ledger' AND t.is_active IS TRUE ORDER BY u.id LIMIT $3
            """, [client, treasuryCodes], r => new ReportCashflowLedgerEvidenceUpload(
                r.GetInt32(0),r.GetInt32(1),r.GetString(2),NullableString(r,3),NullableDateOnly(r,4),
                NullableDateOnly(r,5),NullableString(r,6),NullableString(r,7),r.GetString(8)), budget, ct);

    private Task<ReportInputDataset<ReportCashflowLedgerEvidenceRow>> ReadCashflowLedgerEvidenceRowsAsync(
        PostgresTransactionContext db, int client, IReadOnlyList<ReportCashflowLedgerEvidenceUpload> uploads,
        CaptureBudget budget, CancellationToken ct) => uploads.Count == 0
        ? Task.FromResult(Complete<ReportCashflowLedgerEvidenceRow>([], 0))
        : Query<ReportCashflowLedgerEvidenceRow>(db, """
            SELECT cd.id,cd.upload_id,cd.template_id,u.ledger_account_code,cd.version,
                   cd.source_fingerprint,cd.extract_date,cd.row_data::text
            FROM public.client_data cd JOIN public.uploaded_files u
              ON u.id=cd.upload_id AND u.client_id=cd.client_id AND u.template_id=cd.template_id
            WHERE cd.client_id=$1 AND cd.is_deleted IS FALSE AND u.status='Imported' AND cd.upload_id=ANY($2)
            ORDER BY cd.id LIMIT $3
            """, [client, uploads.Select(row => row.Id).ToArray()], r => new ReportCashflowLedgerEvidenceRow(
                r.GetInt32(0),r.GetInt32(1),r.GetInt32(2),r.GetString(3),r.GetInt32(4),
                NullableString(r,5),NullableDateOnly(r,6),r.GetString(7)), budget, ct);

    private async Task<DateTime> CaptureTimestampAsync(PostgresTransactionContext db,CancellationToken ct)
    { await using var command=db.CreateCommand("SELECT transaction_timestamp()"); return (DateTime)(await command.ExecuteScalarAsync(ct))!; }

    private static async Task<bool> ClientUploadExistsAsync(PostgresTransactionContext db,int client,int upload,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.uploaded_files WHERE id=$1 AND client_id=$2)");
        command.Parameters.AddWithValue(upload); command.Parameters.AddWithValue(client);
        return (bool)(await command.ExecuteScalarAsync(ct))!;
    }

    private async Task<ReportInputDataset<T>> Query<T>(PostgresTransactionContext db,string sql,object[] values,
        Func<NpgsqlDataReader,T> map,CaptureBudget budget,CancellationToken ct)
    {
        await using var command=db.CreateCommand(sql);
        foreach(var value in values) command.Parameters.AddWithValue(value);
        command.Parameters.AddWithValue(options.Value.MaximumRows+1);
        return await ReadAsync(command,map,budget,ct);
    }

    private async Task<ReportInputDataset<T>> ReadAsync<T>(NpgsqlCommand command,Func<NpgsqlDataReader,T> map,
        CaptureBudget budget,CancellationToken ct,bool requested=true)
    {
        var rows=new List<T>(); long bytes=0;
        await using var reader=await command.ExecuteReaderAsync(ct);
        while(await reader.ReadAsync(ct))
        {
            if(rows.Count>=options.Value.MaximumRows) throw new CapacityExceededException();
            T item=map(reader); int size=Size(JsonSerializer.Serialize(item)); budget.Add(size); bytes+=size; rows.Add(item);
        }
        return new ReportInputDataset<T>(requested,requested,rows.Count,bytes,rows);
    }

    private static ReportAmountValue? Amount(NpgsqlDataReader reader,int index,string storageType) =>
        reader.IsDBNull(index) ? null : ReportAmountValue.From(reader.GetString(index),storageType);
    private static int? NullableInt(NpgsqlDataReader reader,int index) => reader.IsDBNull(index) ? null : reader.GetInt32(index);
    private static string? NullableString(NpgsqlDataReader reader,int index) => reader.IsDBNull(index) ? null : reader.GetString(index);
    private static DateOnly? NullableDateOnly(NpgsqlDataReader reader,int index) => reader.IsDBNull(index) ? null : DateOnly.FromDateTime(reader.GetDateTime(index));
    private static DateTime? NullableDateTime(NpgsqlDataReader reader,int index) => reader.IsDBNull(index) ? null : reader.GetDateTime(index);
    private static bool ValidRequest(ReportInputRequest request,out ReportInputRequest normalized,out string? error)
    {
        normalized=request;error="report_input_request_invalid";
        if(request.ClientId<1||!Enum.IsDefined(request.Kind)||request.UploadId is <=0||request.TemplateId is <=0
            ||request.MetadataOnly && (request.Kind != ReportInputKind.LedgerSource
                || request.CategorySource != ReportAccountCategorySource.Cashflow)
            ||request.IncludeLatestTrialBalance && (!request.MetadataOnly
                || request.Kind != ReportInputKind.LedgerSource
                || request.CategorySource != ReportAccountCategorySource.Cashflow)
            ||request.IncludeVoucherContext&&(request.Kind!=ReportInputKind.LedgerSource
                ||request.CategorySource!=ReportAccountCategorySource.Cashflow
                ||request.PeriodStart is null||request.PeriodEnd is null)
            ||request.UploadId is not null&&request.TemplateId is not null
                && !(request.Kind == ReportInputKind.LedgerSource && request.CategorySource == ReportAccountCategorySource.Cashflow)
            ||request.TrialBalanceUploadId is <=0
            ||!Enum.IsDefined(request.CategorySource)) return false;
        string? start=NormalizeDate(request.PeriodStart),end=NormalizeDate(request.PeriodEnd);
        if(request.PeriodStart is not null&&start is null||request.PeriodEnd is not null&&end is null
            ||start is not null&&end is not null&&string.CompareOrdinal(start,end)>0)return false;
        IReadOnlyList<string>? selected=null;
        if(request.SelectedAccountCodes is not null)
        {
            if(request.SelectedAccountCodes.Count is 0 or >500)return false;
            var codes=request.SelectedAccountCodes.Select(code=>(code??"").Trim()).ToArray();
            if(codes.Any(code=>code.Length is 0 or >120)||codes.Distinct(StringComparer.Ordinal).Count()!=codes.Length)return false;
            selected=codes;
        }
        string? view=request.TreasuryView?.Trim().ToLowerInvariant();
        if(view is not null&&view is not("combined" or "cash" or "bank"))return false;
        if(request.Kind==ReportInputKind.FinancialStatements&&request.TreasuryView is not null)return false;
        normalized=request with {PeriodStart=start,PeriodEnd=end,SelectedAccountCodes=selected,TreasuryView=view};
        error=null;return true;
    }
    private static string? NormalizeDate(string? value)=>value is null?null:DateOnly.TryParseExact(value,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var date)?date.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture):null;
    private static ReportInputDataset<T> Empty<T>()=>new(false,false,0,0,[]);
    private static ReportInputDataset<T> Complete<T>(IReadOnlyList<T> rows,long bytes)=>new(true,true,rows.Count,bytes,rows);
    private static int Size(string? value)=>value is null?0:Encoding.UTF8.GetByteCount(value);
    private static string Hash<T>(T value)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(value)))).ToLowerInvariant();
}
