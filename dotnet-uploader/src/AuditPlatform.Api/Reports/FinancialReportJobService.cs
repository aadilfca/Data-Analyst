using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Cashflows;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.TrialBalance;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Reports;

public sealed class FinancialReportJobService(IServiceProvider services,ReportInputService inputs,
    IOptions<FinancialReportJobOptions> options,IOptions<ReportInputOptions> reportOptions)
{
    private FinancialReportJobStore Store=>services.GetRequiredService<FinancialReportJobStore>();

    public async Task<FinancialReportJobResult> SubmitAsync(VerifiedSession? actor,
        FinancialReportJobRequest request,CancellationToken ct)
    {
        if(!options.Value.Enabled||!reportOptions.Value.Enabled)return new("unavailable",Code:"financial_report_jobs_disabled");
        if(actor is null)return new("unauthenticated");
        if(request.RequestId==Guid.Empty||request.ClientId<1)return new("invalid",Code:"invalid_job_request");
        var captured=await CaptureAsync(actor,request,ct);
        if(captured.Result.Status!="success"||captured.Result.Snapshot is null)
            return new(captured.Result.Status,Code:captured.Result.Code);
        try{return await Store.CreateAsync(actor,request,captured.Result.Snapshot.Identity.Fingerprint,ct);}
        catch(TrialBalanceStorageRejected error){return new(error.Status,Code:error.Code);}
    }

    public Task<FinancialReportJobResult> GetAsync(VerifiedSession? actor,Guid id,CancellationToken ct)=>
        Store.GetAsync(actor,id,ct);
    public Task<FinancialReportJobResult> CancelAsync(VerifiedSession? actor,Guid id,CancellationToken ct)=>
        Store.CancelAsync(actor,id,ct);
    public Task<FinancialReportJobDownload?> DownloadAsync(VerifiedSession? actor,Guid id,CancellationToken ct)=>
        Store.DownloadAsync(actor,id,ct);

    public async Task<bool> RunOnceAsync(CancellationToken ct)
    {
        if(!options.Value.Enabled||!reportOptions.Value.Enabled)return false;
        var lease=await Store.ClaimAsync(ct);if(lease is null)return false;
        try
        {
            var captured=await CaptureAsync(lease.Actor,lease.Job.Request,ct);
            if(captured.Result.Status!="success"||captured.Result.Snapshot is null)
            {await Store.FailAsync(lease,"report_inputs_unavailable",ct);return true;}
            var snapshot=captured.Result.Snapshot;
            if(!StringComparer.Ordinal.Equals(snapshot.Identity.Fingerprint,lease.Job.InputFingerprint))
            {await Store.FailAsync(lease,"report_inputs_changed",ct);return true;}
            byte[] bytes=BuildWorkbook(snapshot,lease.Job.Request,ct);
            string file=lease.Job.Request.Report+".xlsx";
            await Store.CompleteAsync(lease,bytes,file,ct);
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch(FinancialStatementInputException){await Store.FailAsync(lease,"report_requires_review",ct);}
        catch(InvalidDataException){await Store.FailAsync(lease,"report_artifact_capacity",ct);}
        catch(FormatException){await Store.FailAsync(lease,"report_amount_invalid",ct);}
        catch(Exception error)when(error is Npgsql.NpgsqlException or HttpRequestException or TimeoutException or IOException)
        {await Store.FailAsync(lease,"report_job_unavailable",ct);}
        return true;
    }

    public Task CleanupAsync(CancellationToken ct)=>Store.CleanupAsync(ct);

    private async Task<(ReportInputCaptureResult Result,DateOnly? Start,DateOnly? End)> CaptureAsync(
        VerifiedSession actor,FinancialReportJobRequest request,CancellationToken ct)
    {
        if(request.Report is "balance_sheet" or "income_statement")
        {
            if(request.TrialBalanceUploadId is not >0)return (new("invalid",Code:"trial_balance_upload_required"),null,null);
            return (await inputs.CaptureAsync(actor,new ReportInputRequest
            {ClientId=request.ClientId,Kind=ReportInputKind.FinancialStatements,TrialBalanceUploadId=request.TrialBalanceUploadId},ct),null,null);
        }
        if(request.Report=="cashflows")
        {
            if(!DateOnly.TryParseExact(request.PeriodStart,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var start)
                ||!DateOnly.TryParseExact(request.PeriodEnd,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var end))
                return (new("invalid",Code:"cashflow_period_invalid"),null,null);
            return (await inputs.CaptureAsync(actor,new ReportInputRequest
            {ClientId=request.ClientId,Kind=ReportInputKind.LedgerSource,CategorySource=ReportAccountCategorySource.Cashflow,
                PeriodStart=request.PeriodStart,PeriodEnd=request.PeriodEnd,TreasuryView=request.TreasuryView},ct),start,end);
        }
        return (new("invalid",Code:"unsupported_financial_report"),null,null);
    }

    private byte[] BuildWorkbook(ReportInputSnapshot snapshot,FinancialReportJobRequest request,CancellationToken ct)
    {
        IReadOnlyList<FinancialReportWorkbookSheet> sheets;
        if(request.Report is "balance_sheet" or "income_statement")
        {
            var section=request.Report=="balance_sheet"?FinancialStatementSection.BalanceSheet:FinancialStatementSection.IncomeStatement;
            var result=services.GetRequiredService<FinancialStatementCalculationService>().Calculate(snapshot,section,ct);
            var notes=services.GetRequiredService<FinancialStatementNotesService>().Calculate(snapshot,result,section,ct);
            var rows=new List<IReadOnlyList<string?>>{new string?[]{"Category","Section","Amount","Source balance","Accounts"}};
            foreach(var row in result.Statement.Categories)rows.Add(new string?[]{row.Label,row.Section,row.Amount,row.RawNet,row.AccountCount.ToString(CultureInfo.InvariantCulture)});
            rows.Add(new string?[]{"Calculation","Section","Amount","Type","Lineage"});
            foreach(var row in result.Statement.Calculations)rows.Add(new string?[]{row.Label,row.Section,row.Amount,row.Type,
                string.Join(",",row.SourceCategoryCoefficients.Select(item=>$"{item.Key}:{item.Value}"))});
            rows.Add(new string?[]{"Controls",result.Statement.Controls.Basis,result.Statement.Controls.Debit,result.Statement.Controls.Credit,result.Statement.Controls.Difference});
            var noteRows=new List<IReadOnlyList<string?>>{new string?[]{"Note","Parent","Current","Comparative","Accounts"}};
            void Visit(FinancialStatementNoteNode node)
            {noteRows.Add(new string?[]{node.Label,node.ParentCategoryId?.ToString(CultureInfo.InvariantCulture),node.CurrentAmount,node.ComparativeAmount,node.AccountCount.ToString(CultureInfo.InvariantCulture)});foreach(var child in node.Children)Visit(child);}
            foreach(var root in notes.Roots)Visit(root);
            noteRows.Add(new string?[]{"Coverage","Statement category","Assigned accounts","Unassigned accounts","Current difference"});
            foreach(var row in notes.Coverage)noteRows.Add(new string?[]{"",row.Label,row.AssignedAccountCount.ToString(CultureInfo.InvariantCulture),
                row.UnassignedAccountCount.ToString(CultureInfo.InvariantCulture),row.CurrentDifference});
            sheets=[new FinancialReportWorkbookSheet("Statement",rows),new FinancialReportWorkbookSheet("Notes",noteRows)];
        }
        else
        {
            var (start,end)= (DateOnly.ParseExact(request.PeriodStart!,"yyyy-MM-dd",CultureInfo.InvariantCulture),
                DateOnly.ParseExact(request.PeriodEnd!,"yyyy-MM-dd",CultureInfo.InvariantCulture));
            var result=services.GetRequiredService<CashflowCalculationService>().Calculate(snapshot,start,end,request.GroupBy,request.TreasuryView,ct);
            var rows=new List<IReadOnlyList<string?>>{new string?[]{"Category"}.Concat(result.Periods.Select(p=>p.Label)).Append("Total").ToArray()};
            foreach(var row in result.CategoryTotals)rows.Add(new string?[]{row.Category}.Concat(result.Periods.Select(p=>row.Periods[p.Key])).Append(row.Total).ToArray());
            var reconciliation=services.GetRequiredService<FinancialReportReconciliationService>().Calculate(snapshot,result,ct);
            var proof=new List<IReadOnlyList<string?>>{new string?[]{"Account","Name","Treasury type","Period","Status","Opening","Closing","Ledger movement","Cashflow movement","Calculated closing","Difference"}};
            foreach(var account in reconciliation.Accounts)foreach(var period in account.Periods)
                proof.Add(new string?[]{account.AccountCode,account.AccountName,account.TreasuryType,period.PeriodKey,period.Status,
                    period.LedgerOpening,period.LedgerClosing,period.LedgerMovement,period.CashflowMovement,period.CalculatedClosing,period.Difference});
            proof.Add(new string?[]{"Posting comparison",reconciliation.PostingStatus});
            proof.AddRange(reconciliation.PostingFindings.Select(item=>(IReadOnlyList<string?>)new string?[]{item.AccountCode,item.VoucherId,
                item.Status,item.LedgerLineCount.ToString(CultureInfo.InvariantCulture),item.TransactionLineCount.ToString(CultureInfo.InvariantCulture),
                string.Join(",",item.DifferenceFields)}));
            var movement=new List<IReadOnlyList<string?>>{new string?[]{"Period","Treasury type","Source movement","Category movement",
                "Internal transfer","Mixed voucher movement","Explained movement","Variance","Mixed voucher count","Allocation status","Status"}};
            movement.AddRange(result.TreasuryMovementProof.Select(item=>(IReadOnlyList<string?>)new string?[]{item.PeriodLabel,item.TreasuryType,
                item.SourceMovement,item.CategoryMovement,item.InternalTransferMovement,item.MixedVoucherMovement,item.ExplainedMovement,
                item.Variance,item.MixedVoucherCount.ToString(CultureInfo.InvariantCulture),item.AllocationStatus,item.Status}));
            sheets=[new FinancialReportWorkbookSheet("Cashflows",rows),new FinancialReportWorkbookSheet("Reconciliation",proof),
                new FinancialReportWorkbookSheet("Movement proof",movement)];
        }
        return FinancialReportWorkbookWriter.Write(sheets,reportOptions.Value,ct);
    }
}
