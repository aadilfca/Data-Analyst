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
    private static bool TooLong(string? value,int maximum)=>value is not null&&value.Length>maximum;

    public async Task<FinancialReportJobResult> SubmitAsync(VerifiedSession? actor,
        FinancialReportJobRequest request,CancellationToken ct)
    {
        if(!options.Value.Enabled||!reportOptions.Value.Enabled)return new("unavailable",Code:"financial_report_jobs_disabled");
        if(actor is null)return new("unauthenticated");
        if(request.RequestId==Guid.Empty||request.ClientId<1)return new("invalid",Code:"invalid_job_request");
        if(TooLong(request.Search,200)||TooLong(request.Category,200)||TooLong(request.AccountHead,300)
            ||TooLong(request.TreasuryType,40)||TooLong(request.PeriodKey,80)||TooLong(request.SummaryFilter,200)
            ||TooLong(request.ExpectedFingerprint,64)||TooLong(request.ExpectedIdentity,64))
            return new("invalid",Code:"invalid_job_filter");
        var captured=await CaptureAsync(actor,request,ct);
        if(captured.Result.Status!="success"||captured.Result.Snapshot is null)
            return new(captured.Result.Status,Code:captured.Result.Code);
        if(request.Report=="cashflow_reconciliation")
        {
            try
            {
                var (start,end)=(captured.Start!.Value,captured.End!.Value);
                var cashflows=services.GetRequiredService<CashflowCalculationService>()
                    .Calculate(captured.Result.Snapshot,start,end,request.GroupBy,request.TreasuryView,ct);
                var reconciliation=services.GetRequiredService<FinancialReportReconciliationService>()
                    .Calculate(captured.Result.Snapshot,cashflows,ct);
                if(!StringComparer.Ordinal.Equals(request.ExpectedFingerprint,reconciliation.EvidenceFingerprint)
                    ||!StringComparer.Ordinal.Equals(request.ExpectedIdentity,reconciliation.WorkingPaper.GetValueOrDefault("cache_identity") as string))
                    return new("conflict",Code:"reconciliation_evidence_changed");
            }
            catch(FinancialStatementInputException error){return new("invalid",Code:error.Message);}
        }
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
            await Store.UpdateProgressAsync(lease,"preparing_inputs","capturing_inputs",0,false,ct);
            var captured=await CaptureAsync(lease.Actor,lease.Job.Request,ct);
            if(captured.Result.Status!="success"||captured.Result.Snapshot is null)
            {await Store.FailAsync(lease,"report_inputs_unavailable",captured.Result.Status is "unavailable" or "timeout" or "conflict",ct);return true;}
            var snapshot=captured.Result.Snapshot;
            if(!StringComparer.Ordinal.Equals(snapshot.Identity.Fingerprint,lease.Job.InputFingerprint))
            {await Store.FailAsync(lease,"report_inputs_changed",false,ct);return true;}
            await Store.UpdateProgressAsync(lease,"running","inputs_ready",1,true,ct);
            await Store.UpdateProgressAsync(lease,"running","building_workbook",2,true,ct);
            byte[] bytes=BuildWorkbook(snapshot,lease.Job.Request,ct);
            string file=lease.Job.Request.Report switch
            {
                "cashflow_reconciliation"=>$"Cashflow_reconciliation_{lease.Job.Request.TreasuryView}_{lease.Job.Request.PeriodStart}_to_{lease.Job.Request.PeriodEnd}.xlsx",
                "cashflows"=>$"Cashflows_{lease.Job.Request.TreasuryView}_{lease.Job.Request.PeriodStart}_to_{lease.Job.Request.PeriodEnd}.xlsx",
                "balance_sheet"=>"Balance_Sheet.xlsx",
                "income_statement"=>"Income_Statement.xlsx",
                _=>"Financial_Report.xlsx"
            };
            await Store.CompleteAsync(lease,bytes,file,ct);
        }
        catch(OperationCanceledException)when(ct.IsCancellationRequested){throw;}
        catch(FinancialStatementInputException){await Store.FailAsync(lease,"report_requires_review",false,ct);}
        catch(InvalidDataException){await Store.FailAsync(lease,"report_artifact_capacity",false,ct);}
        catch(FormatException){await Store.FailAsync(lease,"report_amount_invalid",false,ct);}
        catch(Exception error)when(error is Npgsql.NpgsqlException or HttpRequestException or TimeoutException or IOException)
        {await Store.FailAsync(lease,"report_job_unavailable",true,ct);}
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
        if(request.Report is "cashflows" or "cashflow_reconciliation")
        {
            if(!DateOnly.TryParseExact(request.PeriodStart,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var start)
                ||!DateOnly.TryParseExact(request.PeriodEnd,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var end))
                return (new("invalid",Code:"cashflow_period_invalid"),null,null);
            return (await inputs.CaptureAsync(actor,new ReportInputRequest
            {ClientId=request.ClientId,Kind=ReportInputKind.LedgerSource,CategorySource=ReportAccountCategorySource.Cashflow,
                PeriodStart=request.PeriodStart,PeriodEnd=request.PeriodEnd,TreasuryView=request.TreasuryView,
                SelectedAccountCodes=request.SelectedTreasuryAccountCodes,IncludeVoucherContext=true},ct),start,end);
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
            var reconciliation=services.GetRequiredService<FinancialReportReconciliationService>().Calculate(snapshot,result,ct);
            if(request.Report=="cashflow_reconciliation"
                &&(!StringComparer.Ordinal.Equals(request.ExpectedFingerprint,reconciliation.EvidenceFingerprint)
                    ||!StringComparer.Ordinal.Equals(request.ExpectedIdentity,reconciliation.WorkingPaper.GetValueOrDefault("cache_identity") as string)))
                throw new FinancialStatementInputException("Reconciliation evidence changed; reload the working paper before exporting.");
            var filter=new CashflowWorkbookFilter(request.Search,request.Category,request.AccountHead,request.TreasuryType,
                request.PeriodKey,request.SummaryFilter,request.HideZeroRows,result.TreasuryView,request.SelectedTreasuryAccountCodes);
            sheets=FinancialReportWorkbookWriter.CashflowSheets(result,reconciliation,filter,
                includeReconciliation:request.Report=="cashflow_reconciliation",snapshot:snapshot);
        }
        return FinancialReportWorkbookWriter.Write(sheets,reportOptions.Value,ct);
    }
}
