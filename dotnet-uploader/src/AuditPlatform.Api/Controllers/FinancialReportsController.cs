using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Reports;
using AuditPlatform.Api.Cashflows;
using System.Globalization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using Microsoft.Extensions.DependencyInjection;

namespace AuditPlatform.Api.Controllers;

[Route("api/reconciliation")]
public sealed class FinancialReportsController(ReportInputService inputs,
    IOptions<ReportInputOptions> options) : ControllerBase
{
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

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
            return Ok(result);
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
    }

    [HttpGet("cashflows/reconciliation")]
    public async Task<IActionResult> CashflowReconciliation([FromQuery] int client_id,
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
            SelectedAccountCodes = selected_codes
        }, ct);
        if (captured.Status != "success" || captured.Snapshot is null) return CaptureError(captured);
        try
        {
            var cashflows = HttpContext.RequestServices.GetRequiredService<CashflowCalculationService>()
                .Calculate(captured.Snapshot, start, end, group_by, treasury_view, ct);
            var result = HttpContext.RequestServices.GetRequiredService<FinancialReportReconciliationService>()
                .Calculate(captured.Snapshot, cashflows, ct);
            return Ok(result);
        }
        catch (FinancialStatementInputException error) { return Conflict(new { detail = error.Message }); }
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
                    PeriodStart=period_start,PeriodEnd=period_end,TreasuryView=treasury_view },ct);
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
    {
        var summary=new List<IReadOnlyList<string?>>{new string?[]{"Category"}.Concat(result.Periods.Select(row=>row.Label)).Append("Total").ToArray()};
        foreach(var row in result.CategoryTotals)summary.Add(new string?[]{row.Category}.Concat(result.Periods.Select(period=>row.Periods[period.Key])).Append(row.Total).ToArray());
        summary.Add(new string?[]{"Total receipts"}.Concat(result.Periods.Select(period=>result.PeriodTotals.Single(row=>row.PeriodKey==period.Key).Receipts)).Append(result.TotalReceipts).ToArray());
        summary.Add(new string?[]{"Total payments"}.Concat(result.Periods.Select(period=>result.PeriodTotals.Single(row=>row.PeriodKey==period.Key).Payments)).Append(result.TotalPayments).ToArray());
        var evidence=new List<IReadOnlyList<string?>>{new string?[]{"Account","Name","Treasury type","Period","Status","Opening","Closing","Cashflow movement","Difference"}};
        foreach(var account in reconciliation.Accounts)foreach(var period in account.Periods)
            evidence.Add(new string?[]{account.AccountCode,account.AccountName,account.TreasuryType,period.PeriodKey,period.Status,period.LedgerOpening,period.LedgerClosing,period.CashflowMovement,period.Difference});
        evidence.Add(new string?[]{"Treasury movement proof","Period","Treasury type","Source movement","Category movement",
            "Internal transfer","Mixed voucher movement","Explained movement","Variance","Mixed voucher count","Allocation status","Status"});
        evidence.AddRange(result.TreasuryMovementProof.Select(row=>(IReadOnlyList<string?>)new string?[]{row.PeriodLabel,row.PeriodKey,
            row.TreasuryType,row.SourceMovement,row.CategoryMovement,row.InternalTransferMovement,row.MixedVoucherMovement,
            row.ExplainedMovement,row.Variance,row.MixedVoucherCount.ToString(CultureInfo.InvariantCulture),row.AllocationStatus,row.Status}));
        evidence.Add(new string?[]{"Posting comparison",reconciliation.PostingStatus});
        evidence.AddRange(reconciliation.PostingFindings.Select(row=>(IReadOnlyList<string?>)new string?[]{row.AccountCode,row.VoucherId,row.Status,row.LedgerLineCount.ToString(CultureInfo.InvariantCulture),row.TransactionLineCount.ToString(CultureInfo.InvariantCulture),string.Join(",",row.DifferenceFields)}));
        return [new("Cashflows",summary),new("Reconciliation",evidence)];
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
        if (notesOnly)
            return Ok(new { client_id = clientId,
                upload = new { id = upload.Id, client_id = upload.ClientId, file_name = upload.FileName,
                    as_of_date = upload.AsOfDate, period_start = upload.PeriodStart, period_end = upload.PeriodEnd,
                    period_type = upload.PeriodType, uploaded_at = upload.UploadedAt },
                context_key = result.ContextKey, notes_context_key = snapshot.Identity.ConfigurationFingerprint,
                statement_section = section == FinancialStatementSection.BalanceSheet ? "balance_sheet" : "income_statement",
                categories = result.Statement.Categories, calculations = result.Statement.Calculations,
                comparative = result.Comparative, notes = noteResult });
        var payload = new Dictionary<string, object?>
        {
            ["client_id"] = clientId,
            ["upload"] = new { id = upload.Id, client_id = upload.ClientId, file_name = upload.FileName,
                as_of_date = upload.AsOfDate, period_start = upload.PeriodStart, period_end = upload.PeriodEnd,
                period_type = upload.PeriodType, uploaded_at = upload.UploadedAt },
            ["context_key"] = result.ContextKey,
            ["notes_context_key"] = snapshot.Identity.ConfigurationFingerprint,
            ["closing_status"] = result.ClosingStatus,
            ["statement_section"] = section == FinancialStatementSection.BalanceSheet ? "balance_sheet" : "income_statement",
            ["categories"] = result.Statement.Categories,
            ["calculations"] = result.Statement.Calculations,
            ["diagnostics"] = result.Statement.Diagnostics,
            ["controls"] = result.Statement.Controls,
            ["profit_link"] = result.ProfitLink,
            ["transfer_reconciliation"] = result.TransferReconciliation,
            ["comparative"] = result.Comparative,
            ["opening_review"] = result.OpeningReview,
            ["profit_bridge"] = result.ProfitBridge,
            ["notes"] = noteResult,
            ["input_fingerprint"] = snapshot.Identity.Fingerprint
        };
        if (includeNotes) return Ok(payload);
        payload.Remove("notes");
        return Ok(payload);
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
