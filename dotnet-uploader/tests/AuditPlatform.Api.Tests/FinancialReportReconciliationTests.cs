using AuditPlatform.Api.Cashflows;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Reports;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class FinancialReportReconciliationTests
{
    [Fact]
    public void MissingIndependentLedgerEvidenceRemainsUnavailable()
    {
        var snapshot = Snapshot();
        var cashflows = new CashflowCalculationResult(7,"fingerprint","month","combined","gross",
            [new CashflowPeriod("2026-01","2026-01-01","2026-01-31","Jan-26")],
            [new CashflowPeriodTotal("2026-01","100","0","100")],[],[],[],[],"100","0",[]);

        var result = new FinancialReportReconciliationService().Calculate(snapshot,cashflows,
            TestContext.Current.CancellationToken);

        Assert.Equal("Unavailable",result.Status);
        Assert.Equal("Unavailable",Assert.Single(Assert.Single(result.Accounts).Periods).Status);
        Assert.Contains(result.Diagnostics,message=>message.Contains("No imported independent ledger",StringComparison.Ordinal));
    }

    [Fact]
    public void IncompleteIndependentEvidenceSnapshotIsRejected()
    {
        var snapshot=Snapshot() with
        { CashflowLedgerEvidenceRows=new ReportInputDataset<ReportCashflowLedgerEvidenceRow>(true,false,0,0,[]) };
        var cashflows=new CashflowCalculationResult(7,"fingerprint","month","combined","gross",[],[],[],[],[],[],"0","0",[]);

        var error=Assert.Throws<FinancialStatementInputException>(()=>
            new FinancialReportReconciliationService().Calculate(snapshot,cashflows,TestContext.Current.CancellationToken));

        Assert.Contains("complete independent ledger evidence",error.Message);
    }

    private static ReportInputSnapshot Snapshot() => new(
        new ReportInputScope(7,ReportInputKind.LedgerSource,null,3,[30],null,"2026-01-01","2026-01-31",
            ReportAccountCategorySource.Cashflow,null,"combined"),
        new ReportInputIdentity("test","fingerprint","configuration",DateTime.UtcNow),
        Dataset<ReportLedgerUpload>([]),Dataset<ReportClientDataRow>([]),Dataset<ReportTrialBalanceUpload>([]),
        Dataset<ReportTrialBalanceAccount>([]),Dataset<ReportTrialBalanceAdjustment>([]),
        Dataset<ReportAccountAssignment>([]),Dataset<ReportCategoryMapping>([]),Dataset<ReportCategory>([]),
        Dataset<ReportStatementCalculation>([]),Dataset<ReportStatementComponent>([]),Dataset<ReportProfitLink>([]),
        Dataset<ReportProfitStatus>([]),Dataset<ReportCashflowSettings>([]),
        Dataset<ReportCashflowAccountAssignment>([]),Dataset<ReportCashflowRoleCategory>([]),
        Dataset([new ReportCashflowTreasuryAccount("B1","bank")]),Dataset<ReportCashflowColumnMapping>([]))
        { CashflowLedgerEvidenceUploads=Dataset<ReportCashflowLedgerEvidenceUpload>([]),
          CashflowLedgerEvidenceRows=Dataset<ReportCashflowLedgerEvidenceRow>([]) };

    private static ReportInputDataset<T> Dataset<T>(IReadOnlyList<T> rows)=>new(true,true,rows.Count,0,rows);
}
