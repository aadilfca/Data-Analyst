using AuditPlatform.Api.Cashflows;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Reports;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class CashflowCalculationTests
{
    [Fact]
    public void SummaryUsesConfiguredSourceSelectionAndMonthlyPeriods()
    {
        var result = new CashflowCalculationService().Calculate(Snapshot(), new DateOnly(2026, 1, 1),
            new DateOnly(2026, 1, 31), "month", "combined", TestContext.Current.CancellationToken);

        Assert.Equal("2026-01", Assert.Single(result.Periods).Key);
        var category = Assert.Single(result.CategoryTotals);
        Assert.Equal("Sales", category.Category);
        Assert.Equal("100", category.Total);
        Assert.Equal("100", result.TotalReceipts);
        Assert.Equal("0", result.TotalPayments);
        Assert.True(Assert.Single(result.VoucherControls).Balanced);
    }

    [Fact]
    public void UnbalancedVoucherAndIncompleteSourceAreRejectedWithoutPartialTotals()
    {
        var bad = Snapshot(amount: "99");
        var balanceError = Assert.Throws<FinancialStatementInputException>(() =>
            new CashflowCalculationService().Calculate(bad, new DateOnly(2026, 1, 1),
                new DateOnly(2026, 1, 31), cancellationToken: TestContext.Current.CancellationToken));
        Assert.Contains("unbalanced vouchers", balanceError.Message);

        var incomplete = Snapshot() with
        { ClientData = new ReportInputDataset<ReportClientDataRow>(true, false, 0, 0, []) };
        var inputError = Assert.Throws<FinancialStatementInputException>(() =>
            new CashflowCalculationService().Calculate(incomplete, new DateOnly(2026, 1, 1),
                new DateOnly(2026, 1, 31), cancellationToken: TestContext.Current.CancellationToken));
        Assert.Contains("complete Cashflows transaction rows", inputError.Message);
    }

    [Fact]
    public void MixedCashAndBankVouchersRemainUnderReview()
    {
        var snapshot=Snapshot() with
        {
            ClientData=Dataset([Row(1,"Bank","B1","100","0"),Row(2,"Cash","C1","100","0"),
                Row(3,"Revenue","R1","0","200")]),
            CashflowTreasuryAccounts=Dataset([new ReportCashflowTreasuryAccount("B1","bank"),
                new ReportCashflowTreasuryAccount("C1","cash")])
        };

        var result=new CashflowCalculationService().Calculate(snapshot,new DateOnly(2026,1,1),
            new DateOnly(2026,1,31),"month","combined",TestContext.Current.CancellationToken);

        var bank=Assert.Single(result.TreasuryMovementProof,row=>row.TreasuryType=="Bank");
        var combined=Assert.Single(result.TreasuryMovementProof,row=>row.TreasuryType=="combined");
        Assert.Equal("Review required",bank.Status);
        Assert.Equal("Review required",bank.AllocationStatus);
        Assert.Equal("100",bank.MixedVoucherMovement);
        Assert.Equal(1,bank.MixedVoucherCount);
        Assert.Equal("Review required",combined.Status);
        Assert.Equal("Review required",combined.AllocationStatus);
    }

    private static ReportInputSnapshot Snapshot(string amount = "100")
    {
        var rows = new[]
        {
            Row(1, "Bank", "B1", "100", "0"), Row(2, "Revenue", "R1", "0", amount)
        };
        var uploads = Dataset([new ReportLedgerUpload(30, 7, 3, "cashflow.xlsx", null)]);
        var categories = Dataset([new ReportCategory(1, 7, "cashflow", "sales", "Sales", null, null, null,
            "category", null, null, 1, true)]);
        var mappings = Dataset([new ReportCategoryMapping(7, "R1", "cashflow", 1, null, "sales", "Sales")]);
        var columnMappings = Dataset(new[]
        {
            Map("voucher_id", "Voucher ID"), Map("date", "Date"), Map("account_code", "Account Code"),
            Map("account_name", "Account Name"), Map("debit", "Debit"), Map("credit", "Credit")
        });
        return new ReportInputSnapshot(
            new ReportInputScope(7, ReportInputKind.LedgerSource, null, 3, [30], null,
                "2026-01-01", "2026-01-31", ReportAccountCategorySource.Cashflow, null, "combined"),
            new ReportInputIdentity("test", "fingerprint", "configuration", DateTime.UtcNow),
            uploads, Dataset(rows), Dataset<ReportTrialBalanceUpload>([]),
            Dataset<ReportTrialBalanceAccount>([]), Dataset<ReportTrialBalanceAdjustment>([]),
            Dataset<ReportAccountAssignment>([]), mappings, categories,
            Dataset<ReportStatementCalculation>([]), Dataset<ReportStatementComponent>([]),
            Dataset<ReportProfitLink>([]), Dataset<ReportProfitStatus>([]),
            Dataset([new ReportCashflowSettings(7, false, "gross", null, 3)]),
            Dataset<ReportCashflowAccountAssignment>([]), Dataset<ReportCashflowRoleCategory>([]),
            Dataset([new ReportCashflowTreasuryAccount("B1", "bank")]), columnMappings);
    }

    private static ReportClientDataRow Row(int id, string voucher, string code, string debit, string credit) =>
        new(id, 3, 30, 1, "source", new DateOnly(2026, 1, 10), null,
            $$"""{"Voucher ID":"V1","Date":"2026-01-10","Account Code":"{{code}}","Account Name":"{{voucher}}","Debit":"{{debit}}","Credit":"{{credit}}"}""");

    private static ReportCashflowColumnMapping Map(string role, string column) => new(3, role, column);
    private static ReportInputDataset<T> Dataset<T>(IReadOnlyList<T> rows) => new(true, true, rows.Count, 0, rows);
}
