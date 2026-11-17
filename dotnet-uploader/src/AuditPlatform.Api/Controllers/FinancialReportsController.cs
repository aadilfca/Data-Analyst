using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Reports;
using AuditPlatform.Api.Cashflows;
using System.Globalization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using Microsoft.Extensions.DependencyInjection;
using System.Text.Json;
using AuditPlatform.Api.Uploads;
using AuditPlatform.Api.Configuration;

namespace AuditPlatform.Api.Controllers;

[Route("api/reconciliation")]
public sealed class FinancialReportsController(ReportInputService inputs,
    IOptions<ReportInputOptions> options, FinancialReportJobService reportJobs,
    IOptions<FinancialReportJobOptions> jobOptions) : ControllerBase
{
    private static readonly JsonSerializerOptions CashflowJson = new(JsonSerializerDefaults.Web)
    { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

    [HttpPost("cashflows/reconciliation/exports")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> SubmitCashflowReconciliationExport([FromBody] FinancialReportJobRequest request,
        CancellationToken ct)
    {
        if (!jobOptions.Value.Enabled || !options.Value.Enabled) return StatusCode(503, new { detail = "Financial report jobs are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (request.Report != "cashflow_reconciliation") return BadRequest(new { detail = "Unsupported report job." });
        return JobResult(await reportJobs.SubmitAsync(Session, request, ct), accepted: true);
    }

    [HttpGet("cashflows/reconciliation/exports")]
    public async Task<IActionResult> GetCashflowReconciliationExport([FromQuery] Guid id, CancellationToken ct)
    {
        if (!jobOptions.Value.Enabled || !options.Value.Enabled) return StatusCode(503, new { detail = "Financial report jobs are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        return JobResult(await reportJobs.GetAsync(Session, id, ct));
    }

    [HttpDelete("cashflows/reconciliation/exports")]
    public async Task<IActionResult> CancelCashflowReconciliationExport([FromQuery] Guid id, CancellationToken ct)
    {
        if (!jobOptions.Value.Enabled || !options.Value.Enabled) return StatusCode(503, new { detail = "Financial report jobs are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        return JobResult(await reportJobs.CancelAsync(Session, id, ct));
    }

    [HttpGet("cashflows/reconciliation/exports/download")]
    public async Task<IActionResult> DownloadCashflowReconciliationExport([FromQuery] Guid id, CancellationToken ct)
    {
        if (!jobOptions.Value.Enabled || !options.Value.Enabled) return StatusCode(503, new { detail = "Financial report jobs are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        var result = await reportJobs.DownloadAsync(Session, id, ct);
        return result is null ? NotFound(new { detail = "Report job artifact is unavailable" })
            : File(result.Bytes, result.ContentType, result.FileName);
    }

    private IActionResult JobResult(FinancialReportJobResult result, bool accepted = false) => result.Status switch
    {
        "success" => accepted ? Accepted(result.Job) : Ok(result.Job),
        "invalid" => BadRequest(new { detail = result.Code }),
        "unauthenticated" => Unauthorized(new { detail = "Not authenticated" }),
        "forbidden" => Forbid(),
        "not_found" => NotFound(new { detail = result.Code }),
        "unavailable" => StatusCode(503, new { detail = result.Code }),
        "conflict" => Conflict(new { detail = result.Code }),
        _ => StatusCode(503, new { detail = "Financial report job request could not be completed" })
    };

    [HttpGet("cashflows/account-options")]
    public async Task<IActionResult> CashflowAccountOptions([FromQuery] int client_id, CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (client_id < 1) return BadRequest(new { detail = "Choose a client." });
        var capture = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = client_id, Kind = ReportInputKind.LedgerSource,
            CategorySource = ReportAccountCategorySource.Cashflow, MetadataOnly = true,
            IncludeLatestTrialBalance = true
        }, ct);
        if (capture.Status != "success" || capture.Snapshot is null) return CaptureError(capture);
        var snapshot = capture.Snapshot;
        var names = snapshot.TrialBalanceAccounts.Items.GroupBy(row => row.AccountCode, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.Last().AccountName ?? group.Key, StringComparer.Ordinal);
        var accounts = snapshot.CashflowTreasuryAccounts.Items
            .GroupBy(row => row.AccountCode, StringComparer.Ordinal).Select(group => group.Last())
            .OrderBy(row => row.TreasuryType, StringComparer.OrdinalIgnoreCase)
            .ThenBy(row => row.AccountCode, StringComparer.Ordinal)
            .Select(row => new { account_code = row.AccountCode,
                account_name = names.GetValueOrDefault(row.AccountCode, row.AccountCode),
                treasury_type = row.TreasuryType.ToLowerInvariant() }).ToArray();
        return Ok(new { client_id, configuration_key = snapshot.Identity.ConfigurationFingerprint, accounts });
    }

    [HttpGet("cashflows/revision")]
    public async Task<IActionResult> CashflowRevision([FromQuery] int client_id, CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (client_id < 1) return BadRequest(new { detail = "Choose a client." });
        var capture = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = client_id, Kind = ReportInputKind.LedgerSource,
            CategorySource = ReportAccountCategorySource.Cashflow, MetadataOnly = true
        }, ct);
        if (capture.Status != "success" || capture.Snapshot is null) return CaptureError(capture);
        var snapshot = capture.Snapshot;
        return Ok(new
        {
            client_id,
            cashflow_report_revision = snapshot.CashflowReportRevision ?? 1,
            cashflow_report_source_key = CashflowSourceKey(snapshot),
            cashflow_configuration_key = snapshot.Identity.ConfigurationFingerprint
        });
    }

    [HttpGet("financial-statements/balance-sheet")]
    public Task<IActionResult> BalanceSheet([FromQuery] int client_id,
        [FromQuery] int trial_balance_upload_id, [FromQuery] bool include_notes = true,
        CancellationToken ct = default) => Statement(client_id, trial_balance_upload_id,
        FinancialStatementSection.BalanceSheet, include_notes, ct);

    [HttpGet("financial-statements/income-statement")]
    public Task<IActionResult> IncomeStatement([FromQuery] int client_id,
        [FromQuery] int trial_balance_upload_id, [FromQuery] bool include_notes = true,
        CancellationToken ct = default) => Statement(client_id, trial_balance_upload_id,
        FinancialStatementSection.IncomeStatement, include_notes, ct);

    [HttpGet("financial-statements/notes")]
    public Task<IActionResult> Notes([FromQuery] int client_id, [FromQuery] int trial_balance_upload_id,
        [FromQuery] string statement_section, CancellationToken ct)
    {
        if (statement_section is not ("balance_sheet" or "income_statement"))
            return Task.FromResult<IActionResult>(BadRequest(new { detail = "Choose Balance Sheet or Income Statement" }));
        return Statement(client_id, trial_balance_upload_id,
            statement_section == "balance_sheet" ? FinancialStatementSection.BalanceSheet : FinancialStatementSection.IncomeStatement,
            true, ct, notesOnly: true);
    }

    [HttpGet("cashflows/preview")]
    public async Task<IActionResult> Cashflows([FromQuery] int client_id,
        [FromQuery] string period_start, [FromQuery] string period_end,
        [FromQuery] string group_by = "month", [FromQuery] string treasury_view = "combined",
        [FromQuery(Name = "selected_treasury_account_codes")] string[]? selected_codes = null,
        CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (client_id < 1 || !DateOnly.TryParseExact(period_start, "yyyy-MM-dd", out var start)
            || !DateOnly.TryParseExact(period_end, "yyyy-MM-dd", out var end))
            return BadRequest(new { detail = "Choose a valid Cashflows reporting period." });
        var captured = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = client_id,
            Kind = ReportInputKind.LedgerSource,
            CategorySource = ReportAccountCategorySource.Cashflow,
            PeriodStart = period_start,
            PeriodEnd = period_end,
            TreasuryView = treasury_view,
            SelectedAccountCodes = selected_codes
        }, ct);
        if (captured.Status != "success" || captured.Snapshot is null) return CaptureError(captured);
        try
        {
            var result = HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(captured.Snapshot, start, end, group_by, treasury_view, ct);
            var snapshot = captured.Snapshot;
            var settings = snapshot.CashflowSettings.Items.Single();
            var reconciliation = HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>()
                .Calculate(snapshot, result, ct);
            var preview = CashflowPreview(result, reconciliation);
            string sourceKey = CashflowSourceKey(snapshot);
            long reportRevision = snapshot.CashflowReportRevision ?? 1;
            return new JsonResult(new
            {
                client_id,
                period_start,
                period_end,
                group_by = result.GroupBy,
                treasury_view = result.TreasuryView,
                cashflow_report_revision = reportRevision,
                cashflow_report_source_key = sourceKey,
                cashflow_configuration_key = snapshot.Identity.ConfigurationFingerprint,
                source = new
                {
                    ready = result.Periods.Count > 0,
                    reconciliation_ready = false,
                    source_upload_id = settings.SourceUploadId,
                    source_template_id = settings.SourceTemplateId,
                    source_key = sourceKey
                },
                configuration = new { source_upload_id = settings.SourceUploadId, source_template_id = settings.SourceTemplateId },
                account_selection = snapshot.Scope.SelectedAccountCodes is { } codes
                    ? new { account_codes = codes } : null,
                ledger_context = CashflowLedgerContext(snapshot, start, end, result.GroupBy,
                    result.TreasuryView, reportRevision, sourceKey),
                preview,
                blocking_reasons = Array.Empty<string>()
            }, CashflowJson);
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
    }

    [HttpGet("cashflows/reconciliation")]
    public async Task<IActionResult> CashflowReconciliation([FromQuery] int client_id,
        [FromQuery] string period_start, [FromQuery] string period_end,
        [FromQuery] string group_by = "month", [FromQuery] string treasury_view = "combined",
        [FromQuery] string support_key = "", [FromQuery] string period_key = "",
        [FromQuery] string expected_fingerprint = "", [FromQuery] bool posting_differences = false,
        [FromQuery] string comparison_view = "", [FromQuery] bool differences_only = true,
        [FromQuery] int page = 1, [FromQuery] int limit = 50,
        [FromQuery(Name = "selected_treasury_account_codes")] string[]? selected_codes = null,
        CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (client_id < 1 || !DateOnly.TryParseExact(period_start, "yyyy-MM-dd", out var start)
            || !DateOnly.TryParseExact(period_end, "yyyy-MM-dd", out var end))
            return BadRequest(new { detail = "Choose a valid reconciliation period." });
        var captured = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = client_id, Kind = ReportInputKind.LedgerSource,
            CategorySource = ReportAccountCategorySource.Cashflow,
            PeriodStart = period_start, PeriodEnd = period_end, TreasuryView = treasury_view,
            SelectedAccountCodes = selected_codes,IncludeVoucherContext=true
        }, ct);
        if (captured.Status != "success" || captured.Snapshot is null) return CaptureError(captured);
        try
        {
            var cashflows = HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(captured.Snapshot, start, end, group_by, treasury_view, ct);
            var result = HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>()
                .Calculate(captured.Snapshot, cashflows, ct);
            if (page < 1 || limit is < 1 or > 200 || support_key.Length > 64 || period_key.Length > 40
                || expected_fingerprint.Length > 64 || comparison_view.Length > 16)
                return BadRequest(new { detail = "Choose a valid reconciliation detail scope." });
            if (!string.IsNullOrWhiteSpace(expected_fingerprint)
                && !StringComparer.OrdinalIgnoreCase.Equals(expected_fingerprint, result.EvidenceFingerprint))
                return Conflict(new { detail = "Reconciliation evidence changed; reload the working paper." });
            if (!string.IsNullOrWhiteSpace(period_key)
                && !cashflows.Periods.Any(period => period.Key == period_key))
                return BadRequest(new { detail = "Unknown reconciliation period." });
            if (posting_differences && (!string.IsNullOrWhiteSpace(support_key) || !string.IsNullOrWhiteSpace(period_key)))
                return BadRequest(new { detail = "Posting differences cover the whole selected range." });
            var value = new Dictionary<string, object?>(result.WorkingPaper, StringComparer.Ordinal);
            if (posting_differences)
            {
                string selectedView = string.IsNullOrWhiteSpace(comparison_view)
                    ? (treasury_view == "split" ? "combined" : treasury_view) : comparison_view;
                if (!value.TryGetValue("views", out var viewsValue)
                    || viewsValue is not IReadOnlyDictionary<string, object?> views
                    || !views.ContainsKey(selectedView))
                    return BadRequest(new { detail = "Unknown reconciliation comparison view." });
                var findings = result.PostingFindings.Where(row => row.TreasuryType.Equals("Cash", StringComparison.OrdinalIgnoreCase)
                        ? selectedView is "combined" or "cash" : selectedView is "combined" or "bank")
                    .ToArray();
                var filtered = findings.Where(row => !differences_only || row.Status != "matched").ToArray();
                int pageCount = Math.Max(1, (int)Math.Ceiling(filtered.Length / (double)limit));
                int currentPage = Math.Min(page, pageCount);
                value["posting_differences"] = new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["posting_status"] = result.PostingStatus,
                    ["counts"] = findings.GroupBy(row => row.Status).ToDictionary(group => group.Key, group => group.Count(), StringComparer.Ordinal),
                    ["total_groups"] = findings.Length, ["total_rows"] = filtered.Length,
                    ["page"] = currentPage, ["page_count"] = pageCount, ["limit"] = limit,
                    ["rows"] = filtered.Skip((currentPage - 1) * limit).Take(limit),
                    ["scope_note"] = "Only current stored postings are compared. Transaction Report extraction completeness is not independently certified.",
                    ["timestamp_note"] = "Upload timestamps are import metadata, not certified ERP extraction times."
                };
            }
            return new JsonResult(value, CashflowJson);
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
    }

    [HttpGet("cashflows/reconciliation/cache-validation")]
    public async Task<IActionResult> CashflowReconciliationCacheValidation([FromQuery] int client_id,
        [FromQuery] string period_start, [FromQuery] string period_end,
        [FromQuery] string group_by = "month", [FromQuery] string treasury_view = "combined",
        [FromQuery(Name = "selected_treasury_account_codes")] string[]? selected_codes = null,
        CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (client_id < 1 || !DateOnly.TryParseExact(period_start, "yyyy-MM-dd", out var start)
            || !DateOnly.TryParseExact(period_end, "yyyy-MM-dd", out var end))
            return BadRequest(new { detail = "Choose a valid reconciliation period." });
        var captured = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = client_id, Kind = ReportInputKind.LedgerSource,
            CategorySource = ReportAccountCategorySource.Cashflow,
            PeriodStart = period_start, PeriodEnd = period_end, TreasuryView = treasury_view,
            SelectedAccountCodes = selected_codes, IncludeVoucherContext = true
        }, ct);
        if (captured.Status != "success" || captured.Snapshot is null) return CaptureError(captured);
        try
        {
            var cashflows = HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(captured.Snapshot, start, end, group_by, treasury_view, ct);
            var result = HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>()
                .Calculate(captured.Snapshot, cashflows, ct);
            var scope = result.WorkingPaper.Where(pair => pair.Key is "cache_schema" or "cache_identity" or "client_id"
                    or "template_id" or "period_start" or "period_end" or "group_by" or "treasury_view"
                    or "configuration_key" or "report_revision" or "account_selection")
                .ToDictionary(pair => pair.Key, pair => pair.Value, StringComparer.Ordinal);
            return new JsonResult(scope, CashflowJson);
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
    }

    [HttpGet("cashflows/reconciliation/adjustment-transactions")]
    public async Task<IActionResult> CashflowAdjustmentTransactions([FromQuery] int client_id,
        [FromQuery] string period_start,[FromQuery] string period_end,[FromQuery] string support_key,
        [FromQuery] string side,[FromQuery] string expected_identity,[FromQuery] string source_book="ledger",
        [FromQuery] string search="",[FromQuery] string group_by="month",[FromQuery] string treasury_view="combined",
        [FromQuery] int page=1,[FromQuery] int limit=50,
        [FromQuery(Name="selected_treasury_account_codes")] string[]? selected_codes=null,CancellationToken ct=default)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled)return StatusCode(503,new {detail="Financial reports are unavailable"});
        if(Session is null)return Unauthorized(new {detail="Not authenticated"});
        if(client_id<1||!DateOnly.TryParseExact(period_start,"yyyy-MM-dd",out var start)
            ||!DateOnly.TryParseExact(period_end,"yyyy-MM-dd",out var end)||page<1||limit is <1 or >200
            ||support_key.Length!=24||expected_identity.Length!=64||search.Length>200)
            return BadRequest(new {detail="Choose a valid adjustment transaction scope."});
        var captured=await inputs.CaptureAsync(Session,new ReportInputRequest
        {ClientId=client_id,Kind=ReportInputKind.LedgerSource,CategorySource=ReportAccountCategorySource.Cashflow,
            PeriodStart=period_start,PeriodEnd=period_end,TreasuryView=treasury_view,SelectedAccountCodes=selected_codes,IncludeVoucherContext=true},ct);
        if(captured.Status!="success"||captured.Snapshot is null)return CaptureError(captured);
        try
        {
            var cashflows=HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(captured.Snapshot,start,end,group_by,treasury_view,ct);
            var service=HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>();
            var reconciliation=service.Calculate(captured.Snapshot,cashflows,ct);
            if(!StringComparer.Ordinal.Equals(expected_identity,reconciliation.WorkingPaper.GetValueOrDefault("cache_identity") as string))
                return Conflict(new {detail="Reconciliation evidence changed; reload the working paper."});
            var value=new Dictionary<string,object?>(reconciliation.WorkingPaper,StringComparer.Ordinal);
            foreach(var pair in FinancialReportReconciliationService.AdjustmentTransactions(captured.Snapshot,cashflows,
                reconciliation,support_key,side,source_book,search,page,limit))value[pair.Key]=pair.Value;
            return new JsonResult(value,CashflowJson);
        }
        catch(FinancialStatementInputException error){return Conflict(new {detail=error.Message});}
    }

    [HttpGet("cashflows/reconciliation/adjustment-bundle")]
    public async Task<IActionResult> CashflowAdjustmentBundle([FromQuery] int client_id,
        [FromQuery] string period_start,[FromQuery] string period_end,[FromQuery] string proof,
        [FromQuery] string expected_identity,[FromQuery] string source_book="ledger",
        [FromQuery] string support_key="",[FromQuery] string side="",
        [FromQuery] string group_by="month",[FromQuery] string treasury_view="combined",
        [FromQuery(Name="selected_treasury_account_codes")] string[]? selected_codes=null,CancellationToken ct=default)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled)return StatusCode(503,new {detail="Financial reports are unavailable"});
        if(Session is null)return Unauthorized(new {detail="Not authenticated"});
        if(client_id<1||!DateOnly.TryParseExact(period_start,"yyyy-MM-dd",out var start)
            ||!DateOnly.TryParseExact(period_end,"yyyy-MM-dd",out var end)||expected_identity.Length!=64
            ||(support_key.Length!=0&&(support_key.Length!=24||side is not ("receipts" or "payments")))
            ||(support_key.Length==0&&side.Length!=0))
            return BadRequest(new {detail="Choose a valid adjustment bundle scope."});
        var captured=await inputs.CaptureAsync(Session,new ReportInputRequest
        {ClientId=client_id,Kind=ReportInputKind.LedgerSource,CategorySource=ReportAccountCategorySource.Cashflow,
            PeriodStart=period_start,PeriodEnd=period_end,TreasuryView=treasury_view,SelectedAccountCodes=selected_codes,IncludeVoucherContext=true},ct);
        if(captured.Status!="success"||captured.Snapshot is null)return CaptureError(captured);
        try
        {
            var cashflows=HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(captured.Snapshot,start,end,group_by,treasury_view,ct);
            var service=HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>();
            var reconciliation=service.Calculate(captured.Snapshot,cashflows,ct);
            if(!StringComparer.Ordinal.Equals(expected_identity,reconciliation.WorkingPaper.GetValueOrDefault("cache_identity") as string))
                return Conflict(new {detail="Reconciliation evidence changed; reload the working paper."});
            var response=new Dictionary<string,object?>(reconciliation.WorkingPaper,StringComparer.Ordinal);
            foreach(var pair in FinancialReportReconciliationService.AdjustmentBundle(captured.Snapshot,cashflows,
                reconciliation,proof,source_book,support_key,side))response[pair.Key]=pair.Value;
            if(support_key.Length>0){response["support_key"]=support_key;response["side"]=side;}
            return new JsonResult(response,CashflowJson);
        }
        catch(FinancialStatementInputException error){return Conflict(new {detail=error.Message});}
    }

    [HttpGet("cashflows/drilldown")]
    public async Task<IActionResult> CashflowDrilldown([FromQuery] int client_id,
        [FromQuery] string period_start, [FromQuery] string period_end,
        [FromQuery] string group_by = "month", [FromQuery] string treasury_view = "combined",
        [FromQuery] string search = "", [FromQuery] string category = "",
        [FromQuery] string account_head = "", [FromQuery] string treasury_type = "",
        [FromQuery] string period_key = "", [FromQuery] int page = 1, [FromQuery] int limit = 50,
        [FromQuery(Name = "selected_treasury_account_codes")] string[]? selected_codes = null,
        CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (client_id < 1 || !DateOnly.TryParseExact(period_start, "yyyy-MM-dd", out var start)
            || !DateOnly.TryParseExact(period_end, "yyyy-MM-dd", out var end) || page < 1 || limit is < 1 or > 200)
            return BadRequest(new { detail = "Choose a valid Cashflows drilldown scope." });
        var capture = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = client_id, Kind = ReportInputKind.LedgerSource,
            CategorySource = ReportAccountCategorySource.Cashflow, PeriodStart = period_start,
            PeriodEnd = period_end, TreasuryView = treasury_view, SelectedAccountCodes = selected_codes
        }, ct);
        if (capture.Status != "success" || capture.Snapshot is null) return CaptureError(capture);
        try
        {
            var result = HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(capture.Snapshot, start, end, group_by, treasury_view, ct);
            var snapshot = capture.Snapshot;
            long reportRevision = snapshot.CashflowReportRevision ?? 1;
            string sourceKey = CashflowSourceKey(snapshot);
            string normalizedGroup = result.GroupBy;
            string normalizedView = result.TreasuryView;
            string requestedTreasuryType = treasury_type;
            if (string.IsNullOrWhiteSpace(treasury_type) && normalizedView is "bank" or "cash")
                treasury_type = normalizedView;
            IEnumerable<CashflowLedgerRollup> rows = result.LedgerRollups;
            if (!string.IsNullOrWhiteSpace(category)) rows = rows.Where(row => row.Category.Equals(category.Trim(), StringComparison.OrdinalIgnoreCase));
            if (!string.IsNullOrWhiteSpace(account_head)) rows = rows.Where(row => row.AccountName.Equals(account_head.Trim(), StringComparison.OrdinalIgnoreCase)
                || row.AccountCode.Equals(account_head.Trim(), StringComparison.OrdinalIgnoreCase));
            if (!string.IsNullOrWhiteSpace(treasury_type)) rows = rows.Where(row => row.TreasuryType.Equals(treasury_type.Trim(), StringComparison.OrdinalIgnoreCase));
            if (!string.IsNullOrWhiteSpace(period_key)) rows = rows.Where(row => row.PeriodKey.Equals(period_key.Trim(), StringComparison.OrdinalIgnoreCase));
            if (!string.IsNullOrWhiteSpace(search)) rows = rows.Where(row => row.Category.Contains(search.Trim(), StringComparison.OrdinalIgnoreCase)
                || row.AccountCode.Contains(search.Trim(), StringComparison.OrdinalIgnoreCase)
                || row.AccountName.Contains(search.Trim(), StringComparison.OrdinalIgnoreCase));
            var filtered = rows.ToArray();
            int pageCount = Math.Max(1, (int)Math.Ceiling(filtered.Length / (double)limit));
            int currentPage = Math.Min(page, pageCount);
            var pageRows = filtered.Skip((currentPage - 1) * limit).Take(limit).ToArray();
            bool baseRequest = string.IsNullOrWhiteSpace(search) && string.IsNullOrWhiteSpace(category)
                && string.IsNullOrWhiteSpace(account_head) && string.IsNullOrWhiteSpace(requestedTreasuryType)
                && string.IsNullOrWhiteSpace(period_key);
            CashflowLedgerRollup[]? baseRows = null;
            if (baseRequest && result.LedgerRollups.Count <= 2000)
            {
                var candidate = result.LedgerRollups.ToArray();
                if (JsonSerializer.SerializeToUtf8Bytes(candidate, CashflowJson).Length <= 2_000_000)
                    baseRows = candidate;
            }
            return Ok(new
            {
                client_id, period_start, period_end, group_by = normalizedGroup,
                treasury_view = normalizedView, rows = pageRows, base_rows = baseRows,
                ledger_context = CashflowLedgerContext(snapshot, start, end, normalizedGroup,
                    normalizedView, reportRevision, sourceKey),
                cashflow_report_revision = reportRevision,
                cashflow_report_source_key = sourceKey,
                cashflow_configuration_key = snapshot.Identity.ConfigurationFingerprint,
                account_head_options = result.LedgerRollups.Where(row =>
                    (string.IsNullOrWhiteSpace(treasury_type) || row.TreasuryType.Equals(treasury_type, StringComparison.OrdinalIgnoreCase))
                    && (string.IsNullOrWhiteSpace(period_key) || row.PeriodKey.Equals(period_key, StringComparison.OrdinalIgnoreCase)))
                    .Select(row => new { category = row.Category,
                    value = row.AccountName.Length > 0 ? row.AccountName : row.AccountCode })
                    .Distinct().OrderBy(row => row.category).ThenBy(row => row.value).ToArray(),
                total_rows = filtered.Length, page = currentPage, limit, page_count = pageCount,
                controls = new { category_controls = Array.Empty<object>(), cash_bank_split = new { status = "Not loaded" } },
                source = new { ready = false, source_key = sourceKey },
                database_totals = (object?)null, blocking_reasons = Array.Empty<string>(),
                account_selection = selected_codes is null ? null : new { account_codes = selected_codes }
            });
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
    }

    [HttpGet("cashflows/export")]
    public async Task<IActionResult> CashflowExport([FromQuery] int client_id,
        [FromQuery] string period_start, [FromQuery] string period_end,
        [FromQuery] string group_by = "month", [FromQuery] string treasury_view = "combined",
        [FromQuery] string search = "", [FromQuery] string category = "",
        [FromQuery] string account_head = "", [FromQuery] string treasury_type = "",
        [FromQuery] string period_key = "", [FromQuery] string summary_filter = "",
        [FromQuery] bool hide_zero_rows = false,
        [FromQuery(Name = "selected_treasury_account_codes")] string[]? selected_codes = null,
        CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (client_id < 1 || !DateOnly.TryParseExact(period_start, "yyyy-MM-dd", out var start)
            || !DateOnly.TryParseExact(period_end, "yyyy-MM-dd", out var end)
            || search.Length > 200 || category.Length > 200 || account_head.Length > 300
            || treasury_type.Length > 40 || period_key.Length > 80 || summary_filter.Length > 200)
            return BadRequest(new { detail = "Choose a valid Cashflows export scope." });
        var capture = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = client_id, Kind = ReportInputKind.LedgerSource,
            CategorySource = ReportAccountCategorySource.Cashflow, PeriodStart = period_start,
            PeriodEnd = period_end, TreasuryView = treasury_view, SelectedAccountCodes = selected_codes,
            IncludeVoucherContext=true
        }, ct);
        if (capture.Status != "success" || capture.Snapshot is null) return CaptureError(capture);
        try
        {
            var result = HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(capture.Snapshot, start, end, group_by, treasury_view, ct);
            var reconciliation = HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>()
                .Calculate(capture.Snapshot, result, ct);
            var filter = new CashflowWorkbookFilter(search, category, account_head, treasury_type,
                period_key, summary_filter, hide_zero_rows, result.TreasuryView,selected_codes);
            var sheets = FinancialReportWorkbookWriter.CashflowSheets(result, reconciliation, filter);
            byte[] bytes = FinancialReportWorkbookWriter.Write(sheets, options.Value, ct);
            string selectedSuffix = selected_codes is { Length: > 0 } ? "_selected" : "";
            string viewSuffix = result.TreasuryView == "combined" ? "" : "_" + result.TreasuryView;
            string fileName = $"Cashflows{selectedSuffix}{viewSuffix}_{result.GroupBy}_{period_start}_to_{period_end}.xlsx";
            return File(bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", fileName);
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
        catch (InvalidDataException error) { return StatusCode(413, new { detail = error.Message }); }
    }

    [HttpGet("reports/download")]
    public async Task<IActionResult> Download([FromQuery] int client_id, [FromQuery] string report,
        [FromQuery] int? trial_balance_upload_id, [FromQuery] string? period_start,
        [FromQuery] string? period_end, [FromQuery] string group_by = "month",
        [FromQuery] string treasury_view = "combined", CancellationToken ct = default)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        try
        {
            IReadOnlyList<FinancialReportWorkbookSheet> sheets;
            string fileName;
            if (report is "balance_sheet" or "income_statement")
            {
                if (trial_balance_upload_id is not > 0) return BadRequest(new { detail = "Choose a Trial Balance upload." });
                var capture = await inputs.CaptureAsync(Session, new ReportInputRequest
                { ClientId = client_id, Kind = ReportInputKind.FinancialStatements, TrialBalanceUploadId = trial_balance_upload_id }, ct);
                if (capture.Status != "success" || capture.Snapshot is null) return CaptureError(capture);
                var section = report == "balance_sheet" ? FinancialStatementSection.BalanceSheet : FinancialStatementSection.IncomeStatement;
                var result = HttpContext.RequestServices.GetRequiredService<FinancialStatementCalculationService>().Calculate(capture.Snapshot,section,ct);
                var noteResult = HttpContext.RequestServices.GetRequiredService<FinancialStatementNotesService>().Calculate(capture.Snapshot,result,section,ct);
                sheets = StatementSheets(result,noteResult);
                fileName = report + ".xlsx";
            }
            else if (report == "cashflows")
            {
                if (!DateOnly.TryParseExact(period_start,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var start)
                    ||!DateOnly.TryParseExact(period_end,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var end))
                    return BadRequest(new { detail = "Choose a Cashflows reporting period." });
                var capture = await inputs.CaptureAsync(Session,new ReportInputRequest
                { ClientId=client_id,Kind=ReportInputKind.LedgerSource,CategorySource=ReportAccountCategorySource.Cashflow,
                    PeriodStart=period_start,PeriodEnd=period_end,TreasuryView=treasury_view,IncludeVoucherContext=true },ct);
                if(capture.Status!="success"||capture.Snapshot is null)return CaptureError(capture);
                var cashflows=HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                    .Calculate(capture.Snapshot,start,end,group_by,treasury_view,ct);
                var reconciliation=HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>()
                    .Calculate(capture.Snapshot,cashflows,ct);
                sheets=CashflowSheets(cashflows,reconciliation);
                fileName="cashflows.xlsx";
            }
            else return BadRequest(new { detail = "Choose a supported financial report." });

            byte[] bytes=FinancialReportWorkbookWriter.Write(sheets,options.Value,ct);
            return File(bytes,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",fileName);
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
        catch (InvalidDataException error) { return StatusCode(413,new { detail=error.Message }); }
    }

    private static IReadOnlyList<FinancialReportWorkbookSheet> StatementSheets(
        FinancialStatementResult statement,FinancialStatementNotesResult notes)
    {
        var main=new List<IReadOnlyList<string?>>{new string?[]{"Category","Statement","Amount","Raw Trial Balance","Account count"}};
        foreach(var row in statement.Statement.Categories)
            main.Add(new string?[]{row.Label,row.Section,row.Amount,row.RawNet,row.AccountCount.ToString(CultureInfo.InvariantCulture)});
        main.Add(new string?[]{"Calculation","Section","Amount","Type","Lineage"});
        foreach(var row in statement.Statement.Calculations)
            main.Add(new string?[]{row.Label,row.Section,row.Amount,row.Type,string.Join(",",row.SourceCategoryCoefficients.Select(item=>$"{item.Key}:{item.Value}"))});
        var noteRows=new List<IReadOnlyList<string?>>{new string?[]{"Note","Parent","Current","Comparative","Accounts"}};
        void Visit(FinancialStatementNoteNode node)
        {noteRows.Add(new string?[]{node.Label,node.ParentCategoryId?.ToString(CultureInfo.InvariantCulture),node.CurrentAmount,node.ComparativeAmount,node.AccountCount.ToString(CultureInfo.InvariantCulture)});foreach(var child in node.Children)Visit(child);}
        foreach(var root in notes.Roots)Visit(root);
        noteRows.Add(new string?[]{"Coverage","Statement category","Assigned accounts","Unassigned accounts","Current difference"});
        foreach(var row in notes.Coverage)noteRows.Add(new string?[]{"",row.Label,row.AssignedAccountCount.ToString(CultureInfo.InvariantCulture),row.UnassignedAccountCount.ToString(CultureInfo.InvariantCulture),row.CurrentDifference});
        return [new("Statement",main),new("Notes",noteRows)];
    }

    private static IReadOnlyList<FinancialReportWorkbookSheet> CashflowSheets(
        CashflowCalculationResult result,FinancialReportReconciliationResult reconciliation)
        => FinancialReportWorkbookWriter.CashflowSheets(result,reconciliation);

    private static string CashflowSourceKey(ReportInputSnapshot snapshot)
        => snapshot.Scope.TemplateId is { } template
            ? $"client-data-v1:{snapshot.Scope.ClientId}:{template}"
            : $"client-data-v1:{snapshot.Scope.ClientId}:unconfigured";

    private static object CashflowLedgerContext(ReportInputSnapshot snapshot, DateOnly start, DateOnly end,
        string groupBy, string treasuryView, long revision, string sourceKey) => new
    {
        client_id = snapshot.Scope.ClientId,
        template_id = snapshot.Scope.TemplateId,
        period_start = start.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
        period_end = end.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
        group_by = groupBy,
        treasury_view = treasuryView,
        report_revision = revision,
        source_key = sourceKey,
        configuration_key = snapshot.Identity.ConfigurationFingerprint,
        account_selection = snapshot.Scope.SelectedAccountCodes is { } selected
            ? new { account_codes = selected }
            : null
    };

    private static IReadOnlyDictionary<string, object?> CashflowPreview(CashflowCalculationResult result,
        FinancialReportReconciliationResult reconciliation)
    {
        var categoryRows = result.CategoryTotals.Select(row =>
        {
            var item = new Dictionary<string, object?>(StringComparer.Ordinal)
            { ["category"] = row.Category, ["Total"] = row.Total };
            foreach (var period in result.Periods) item[period.Key] = row.Periods.GetValueOrDefault(period.Key, "0");
            return item;
        }).ToArray();
        var categories = categoryRows.Select(row => (string)row["category"]!).Distinct(StringComparer.Ordinal).ToArray();
        var treasuryRows = result.TreasuryCategoryTotals.ToArray();
        IReadOnlyList<object> viewTotals;
        if (result.TreasuryView == "split")
            viewTotals = treasuryRows.Cast<object>().ToArray();
        else if (result.TreasuryView is "bank" or "cash")
        {
            string field = result.TreasuryView == "bank" ? "BankAmount" : "CashAmount";
            viewTotals = categories.Select(category =>
            {
                var item = new Dictionary<string, object?>(StringComparer.Ordinal) { ["category"] = category };
                foreach (var period in result.Periods)
                {
                    var rows = treasuryRows.Where(row => row.Category == category && row.PeriodKey == period.Key);
                    item[period.Key] = rows.Aggregate(UploadExactAmount.Zero,
                        (sum, row) => sum + UploadExactAmount.Parse(field == "BankAmount" ? row.BankAmount : row.CashAmount)).ToFixedString();
                }
                item["Total"] = result.Periods.Aggregate(UploadExactAmount.Zero, (sum, period) =>
                {
                    var value = (string)item[period.Key]!;
                    return sum + UploadExactAmount.Parse(value);
                }).ToFixedString();
                return item;
            }).Cast<object>().ToArray();
        }
        else viewTotals = categoryRows.Cast<object>().ToArray();

        var periodMetrics = result.PeriodMetrics.ToDictionary(pair => pair.Key, pair => (object)pair.Value, StringComparer.Ordinal);
        var detailTotals = result.LedgerRollups.GroupBy(row => (row.Category, row.PeriodKey))
            .ToDictionary(group => group.Key,
                group => group.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + UploadExactAmount.Parse(row.Amount)));
        var categoryControls = new List<object>();
        bool detailMatched = detailTotals.Keys.All(key => result.CategoryTotals.Any(row => row.Category == key.Category));
        foreach (var category in result.CategoryTotals)
        foreach (var period in result.Periods)
        {
            var summary = UploadExactAmount.Parse(category.Periods.GetValueOrDefault(period.Key, "0"));
            var detail = detailTotals.GetValueOrDefault((category.Category, period.Key), UploadExactAmount.Zero);
            var difference = detail - summary;
            bool matched = difference.Abs().CompareTo(UploadExactAmount.Parse("0.01")) <= 0;
            detailMatched &= matched;
            categoryControls.Add(new
            {
                category = category.Category, period_key = period.Key,
                summary_total = summary.ToFixedString(), detail_total = detail.ToFixedString(),
                difference = difference.ToFixedString(), status = matched ? "Matched" : "Variance"
            });
        }
        var bankRows = result.LedgerRollups.Where(row => row.TreasuryType == "Bank").ToArray();
        var cashRows = result.LedgerRollups.Where(row => row.TreasuryType == "Cash").ToArray();
        var mixedRows = result.LedgerRollups.Except(bankRows).Except(cashRows).ToArray();
        UploadExactAmount Sum(IEnumerable<CashflowLedgerRollup> rows) => rows.Aggregate(UploadExactAmount.Zero,
            (sum, row) => sum + UploadExactAmount.Parse(row.Amount));
        int Count(IEnumerable<CashflowLedgerRollup> rows) => rows.Sum(row => row.LineCount);
        var splitRows = new
        {
            bank_amount = Sum(bankRows).ToFixedString(), cash_amount = Sum(cashRows).ToFixedString(),
            mixed_or_unresolved_amount = Sum(mixedRows).ToFixedString(),
            bank_line_count = Count(bankRows), cash_line_count = Count(cashRows),
            mixed_or_unresolved_line_count = Count(mixedRows),
            detail_total = Sum(result.LedgerRollups).ToFixedString(),
            status = mixedRows.Length == 0 ? "Matched" : "Review required"
        };
        var controls = new { category_controls = categoryControls, cash_bank_split = splitRows };
        var payload = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["input_fingerprint"] = result.InputFingerprint,
            ["group_by"] = result.GroupBy,
            ["treasury_view"] = result.TreasuryView,
            ["receipt_presentation"] = result.ReceiptPresentation,
            ["periods"] = result.Periods,
            ["months"] = result.Periods.Select(period => period.Key).ToArray(),
            ["category_totals"] = categoryRows,
            ["treasury_category_totals"] = treasuryRows,
            ["treasury_reconciliation"] = result.TreasuryReconciliation,
            ["treasury_view_reconciliation"] = result.TreasuryReconciliation,
            ["treasury_movement_proof"] = result.TreasuryMovementProof,
            ["treasury_view_totals"] = viewTotals,
            ["treasury_view_ready"] = true,
            ["period_totals"] = result.PeriodTotals,
            ["period_metrics"] = periodMetrics,
            ["ledger_rollups"] = result.LedgerRollups,
            ["drilldown_controls"] = controls,
            ["drilldown_details_prepared"] = detailMatched,
            ["balance_reconciliation"] = BalanceReconciliation(result, reconciliation),
            ["transfer_details"] = result.TransferDetails,
            ["transfer_reconciliation"] = result.TransferReconciliation,
            ["customer_payment_details"] = result.CustomerPaymentDetails,
            ["customer_payment_customer_totals"] = result.CustomerPaymentCustomerTotals,
            ["customer_payment_account_period_totals"] = result.CustomerPaymentAccountPeriodTotals,
            ["customer_payment_period_totals"] = result.CustomerPaymentPeriodTotals,
            ["customer_payment_reconciliation"] = result.CustomerPaymentReconciliation,
            ["customer_payment_customer_options"] = result.CustomerPaymentCustomerOptions,
            ["vendor_payment_details"] = result.VendorPaymentDetails,
            ["vendor_payment_vendor_totals"] = result.VendorPaymentVendorTotals,
            ["vendor_payment_period_totals"] = result.VendorPaymentPeriodTotals,
            ["deduction_details"] = result.DeductionDetails,
            ["categories_needing_review"] = result.CategoriesNeedingReview,
            ["selected_voucher_count"] = result.SelectedVoucherCount,
            ["source_line_count"] = result.SourceLineCount,
            ["internal_transfer_count"] = result.InternalTransferCount,
            ["category_review_count"] = result.CategoryReviewCount,
            ["deduction_count"] = result.DeductionCount,
            ["diagnostics"] = result.Diagnostics
        };
        return payload;
    }

    private static IReadOnlyDictionary<string, object?> BalanceReconciliation(
        CashflowCalculationResult cashflows, FinancialReportReconciliationResult reconciliation)
    {
        decimal? Number(string? value) => decimal.TryParse(value, NumberStyles.Number,
            CultureInfo.InvariantCulture, out var parsed) ? parsed : null;
        UploadExactAmount? Exact(decimal? value) => value is { } amount
            ? UploadExactAmount.Parse(amount.ToString(CultureInfo.InvariantCulture)) : null;
        decimal? Decimal(UploadExactAmount? value) => value is { } amount ? Number(amount.ToFixedString()) : null;
        var periods = new List<Dictionary<string, object?>>();
        UploadExactAmount? calculatedOpening = null;
        foreach (var period in cashflows.Periods)
        {
            var accounts = reconciliation.Accounts.Select(account =>
            {
                var evidence = account.Periods.FirstOrDefault(row => row.PeriodKey == period.Key);
                return new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["account_code"] = account.AccountCode,
                    ["account_name"] = account.AccountName,
                    ["treasury_type"] = account.TreasuryType.ToLowerInvariant(),
                    ["status"] = evidence is not null && evidence.LedgerOpening is not null
                        && evidence.LedgerClosing is not null && evidence.LedgerMovement is not null ? "Ready" : "Unavailable",
                    ["opening_balance"] = Number(evidence?.LedgerOpening),
                    ["closing_balance"] = Number(evidence?.LedgerClosing),
                    ["ledger_movement"] = Number(evidence?.LedgerMovement),
                    ["cashflow_movement"] = Number(evidence?.CashflowMovement)
                };
            }).ToArray();
            var relevant = reconciliation.Accounts.Select(account => account.Periods.FirstOrDefault(row => row.PeriodKey == period.Key))
                .Where(row => row is not null).ToArray();
            bool complete = reconciliation.Accounts.Count > 0 && relevant.Length == reconciliation.Accounts.Count
                && relevant.All(row => row!.LedgerOpening is not null && row.LedgerClosing is not null && row.LedgerMovement is not null);
            UploadExactAmount? Sum(Func<FinancialReportPeriodReconciliation, string?> value)
            {
                if (!complete || relevant.Any(row => value(row!) is null)) return null;
                return relevant.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + UploadExactAmount.Parse(value(row!)!));
            }
            var opening = Sum(row => row.LedgerOpening);
            var closing = Sum(row => row.LedgerClosing);
            var ledgerMovement = Sum(row => row.LedgerMovement);
            if (calculatedOpening is null && periods.Count == 0) calculatedOpening = opening;
            var flow = Number(cashflows.PeriodTotals.Single(row => row.PeriodKey == period.Key).NetMovement);
            var calculatedClosing = calculatedOpening is { } open && Exact(flow) is { } movement
                ? open + movement : (UploadExactAmount?)null;
            var difference = closing is { } closingValue && calculatedClosing is { } calculatedValue
                ? closingValue - calculatedValue : (UploadExactAmount?)null;
            periods.Add(new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["key"] = period.Key, ["start"] = period.Start, ["end"] = period.End,
                ["complete"] = complete, ["accounts"] = accounts,
                ["opening_total"] = Decimal(opening), ["closing_total"] = Decimal(closing),
                ["ledger_movement_total"] = Decimal(ledgerMovement),
                ["calculated_opening"] = Decimal(calculatedOpening), ["calculated_closing"] = Decimal(calculatedClosing),
                ["cashflow_net_movement"] = flow,
                ["variance"] = Decimal(difference),
                ["status"] = difference is null ? "Unavailable"
                    : difference.Value.Abs().CompareTo(UploadExactAmount.Parse("0.01")) <= 0 ? "Matched" : "Variance"
            });
            calculatedOpening = calculatedClosing;
        }
        decimal? Total(string field) => periods.Count > 0 && periods.All(row => row.GetValueOrDefault(field) is decimal)
            ? periods.Sum(row => (decimal)row[field]!) : null;
        var overall = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["opening_total"] = periods.FirstOrDefault()?.GetValueOrDefault("opening_total"),
            ["closing_total"] = periods.LastOrDefault()?.GetValueOrDefault("closing_total"),
            ["calculated_opening"] = periods.FirstOrDefault()?.GetValueOrDefault("calculated_opening"),
            ["calculated_closing"] = periods.LastOrDefault()?.GetValueOrDefault("calculated_closing"),
            ["cashflow_net_movement"] = Total("cashflow_net_movement"),
            ["variance"] = periods.LastOrDefault()?.GetValueOrDefault("variance"),
            ["complete"] = periods.Count > 0 && periods.All(row => row.GetValueOrDefault("complete") is true)
        };
        var breakdown = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (string type in new[] { "bank", "cash" })
        {
            var applicable = reconciliation.Accounts.Where(account => account.TreasuryType.Equals(type, StringComparison.OrdinalIgnoreCase)).ToArray();
            UploadExactAmount? partCalculatedOpening = null;
            var rows = new List<Dictionary<string, object?>>();
            foreach (var period in periods)
            {
                var accounts = (IReadOnlyList<Dictionary<string, object?>>)period["accounts"]!;
                var selected = accounts.Where(row => (string)row["treasury_type"]! == type).ToArray();
                bool ready = applicable.Length > 0 && selected.Length == applicable.Length
                    && selected.All(row => row["opening_balance"] is decimal && row["closing_balance"] is decimal && row["ledger_movement"] is decimal);
                decimal? opening = ready ? selected.Sum(row => (decimal)row["opening_balance"]!) : null;
                decimal? closing = ready ? selected.Sum(row => (decimal)row["closing_balance"]!) : null;
                decimal? movement = ready ? selected.Sum(row => (decimal)row["cashflow_movement"]!) : null;
                if (partCalculatedOpening is null && rows.Count == 0 && ready) partCalculatedOpening = Exact(opening);
                var calculated = partCalculatedOpening is { } prior && Exact(movement) is { } net ? prior + net : (UploadExactAmount?)null;
                var variance = Exact(closing) is { } close && calculated is { } expected ? close - expected : (UploadExactAmount?)null;
                var item = new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["key"] = period["key"], ["start"] = period["start"], ["end"] = period["end"],
                    ["status"] = applicable.Length == 0 ? "Not applicable" : variance is null ? "Unavailable"
                        : variance.Value.Abs().CompareTo(UploadExactAmount.Parse("0.01")) <= 0 ? "Matched" : "Variance",
                    ["opening_total"] = opening, ["closing_total"] = closing,
                    ["calculated_opening"] = Decimal(partCalculatedOpening),
                    ["calculated_closing"] = Decimal(calculated),
                    ["cashflow_net_movement"] = movement,
                    ["variance"] = Decimal(variance)
                };
                rows.Add(item);
                partCalculatedOpening = calculated;
            }
            var first = rows.FirstOrDefault(); var last = rows.LastOrDefault();
            var flow = rows.Select(row => row.GetValueOrDefault("cashflow_net_movement")).OfType<decimal>().ToArray();
            breakdown[type] = new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["status"] = applicable.Length == 0 ? "Not applicable"
                    : rows.Any(row => (string)row["status"]! == "Unavailable") ? "Unavailable"
                    : rows.Any(row => (string)row["status"]! == "Variance") ? "Variance" : "Matched",
                ["periods"] = rows,
                ["overall"] = new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["opening_total"] = first?.GetValueOrDefault("opening_total"),
                    ["closing_total"] = last?.GetValueOrDefault("closing_total"),
                    ["calculated_opening"] = first?.GetValueOrDefault("calculated_opening"),
                    ["calculated_closing"] = last?.GetValueOrDefault("calculated_closing"),
                    ["cashflow_net_movement"] = flow.Length == rows.Count ? flow.Sum() : null,
                    ["variance"] = last?.GetValueOrDefault("variance")
                }
            };
        }
        var unresolved = cashflows.TreasuryMovementProof.Where(row => row.TreasuryType == "combined")
            .ToDictionary(row => row.PeriodKey, row => Number(row.UnallocatedCategoryMovement) ?? 0m, StringComparer.Ordinal);
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["evidence_fingerprint"] = reconciliation.EvidenceFingerprint,
            ["coverage_complete"] = periods.Count > 0 && periods.All(row => (bool)row["complete"]!),
            ["periods"] = periods, ["overall"] = overall, ["treasury_breakdown"] = breakdown,
            ["unresolved_movements"] = unresolved,
            ["diagnostics"] = reconciliation.Diagnostics.Select(message => new { message }).ToArray()
        };
    }

    private async Task<IActionResult> Statement(int clientId, int uploadId,
        FinancialStatementSection section, bool includeNotes, CancellationToken ct, bool notesOnly = false)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Financial reports are unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        if (clientId < 1 || uploadId < 1) return BadRequest(new { detail = "Invalid report scope" });

        var captured = await inputs.CaptureAsync(Session, new ReportInputRequest
        {
            ClientId = clientId,
            Kind = ReportInputKind.FinancialStatements,
            TrialBalanceUploadId = uploadId
        }, ct);
        if (captured.Status != "success" || captured.Snapshot is null) return CaptureError(captured);
        var snapshot = captured.Snapshot;
        FinancialStatementResult result;
        FinancialStatementNotesResult? noteResult = null;
        try
        {
            result = HttpContext.RequestServices.GetRequiredService<FinancialStatementCalculationService>()
                .Calculate(snapshot, section, ct);
            if (includeNotes) noteResult = HttpContext.RequestServices.GetRequiredService<FinancialStatementNotesService>()
                .Calculate(snapshot, result, section, ct);
        }
        catch (FinancialStatementInputException error)
        {
            return Conflict(new { detail = error.Message });
        }

        var upload = snapshot.TrialBalanceUploads.Items.Single();
        string sectionKey = section == FinancialStatementSection.BalanceSheet ? "balance_sheet" : "income_statement";
        var noteRoots = snapshot.Categories.Items.Where(row => row.CategoryType == "note"
                && row.ParentCategoryId is null && row.SourceStatementCategoryId is { } sourceCategory
                && result.Statement.Categories.Any(category => category.Id == sourceCategory
                    && category.RowType == "category" && category.Section == sectionKey))
            .OrderBy(row => row.SortOrder).ThenBy(row => row.Id)
            .Select(row => new { id = row.Id, source_statement_category_id = row.SourceStatementCategoryId,
                sort_order = row.SortOrder }).ToArray();
        object? comparative = result.Comparative is { } compare ? new
        {
            basis = compare.Basis, categories = compare.Categories, calculations = compare.Calculations,
            opening_review = result.OpeningReview
        } : null;
        if (notesOnly)
            return new JsonResult(new { client_id = clientId,
                upload = new { id = upload.Id, client_id = upload.ClientId, file_name = upload.FileName,
                    as_of_date = upload.AsOfDate, period_start = upload.PeriodStart, period_end = upload.PeriodEnd,
                    period_type = upload.PeriodType, uploaded_at = upload.UploadedAt },
                context_key = result.ContextKey, notes_context_key = snapshot.Identity.ConfigurationFingerprint,
                statement_section = sectionKey,
                categories = result.Statement.Categories, calculations = result.Statement.Calculations,
                comparative, notes = noteResult }, CashflowJson);
        var payload = new Dictionary<string, object?>
        {
            ["client_id"] = clientId,
            ["upload"] = new { id = upload.Id, client_id = upload.ClientId, file_name = upload.FileName,
                as_of_date = upload.AsOfDate, period_start = upload.PeriodStart, period_end = upload.PeriodEnd,
                period_type = upload.PeriodType, uploaded_at = upload.UploadedAt },
            ["context_key"] = result.ContextKey,
            ["notes_context_key"] = snapshot.Identity.ConfigurationFingerprint,
            ["closing_status"] = result.ClosingStatus,
            ["basis"] = result.Statement.Basis,
            ["statement_section"] = sectionKey,
            ["categories"] = result.Statement.Categories,
            ["calculations"] = result.Statement.Calculations,
            ["diagnostics"] = result.Statement.Diagnostics,
            ["controls"] = result.Statement.Controls,
            ["profit_link"] = result.ProfitLink,
            ["transfer_reconciliation"] = result.TransferReconciliation,
            ["comparative"] = comparative,
            ["opening_review"] = result.OpeningReview,
            ["profit_bridge"] = result.ProfitBridge,
            ["notes"] = noteResult,
            ["input_fingerprint"] = snapshot.Identity.Fingerprint
        };
        if (includeNotes) return new JsonResult(payload, CashflowJson);
        payload["note_references"] = new { statement_section = sectionKey, roots = noteRoots };
        payload.Remove("notes");
        return new JsonResult(payload, CashflowJson);
    }

    private IActionResult CaptureError(ReportInputCaptureResult result) => result.Status switch
    {
        "invalid" => BadRequest(new { detail = result.Code }),
        "not_found" => NotFound(new { detail = result.Code }),
        "unavailable" => StatusCode(503, new { detail = result.Code }),
        "too_large" => StatusCode(413, new { detail = "Report source exceeds safe limits; no partial report was produced." }),
        "timeout" => StatusCode(503, new { detail = "Report source was not captured within the configured time." }),
        _ => Conflict(new { detail = "Report input could not be captured safely." })
    };
}
