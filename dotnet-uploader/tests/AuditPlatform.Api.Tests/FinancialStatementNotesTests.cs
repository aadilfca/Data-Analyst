using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Reports;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class FinancialStatementNotesTests
{
    [Fact]
    public void BalanceSheetNotesRollUpLeafAndComparativeBalancesAndShowUnassignedDifference()
    {
        var snapshot = Snapshot();
        var statement = Statement();

        var result = new FinancialStatementNotesService().Calculate(snapshot, statement,
            FinancialStatementSection.BalanceSheet, TestContext.Current.CancellationToken);

        var root = Assert.Single(result.Roots);
        Assert.Equal("100", root.CurrentAmount);
        Assert.Equal("80", root.ComparativeAmount);
        Assert.Equal("60", Assert.Single(root.Children, row => row.Id == 11).CurrentAmount);
        var pending = Assert.Single(root.Children, row => row.IsUnallocatedSummary);
        Assert.Equal("40", pending.CurrentAmount);
        Assert.Equal("30", pending.ComparativeAmount);
        var coverage = Assert.Single(result.Coverage);
        Assert.Equal(1, coverage.AssignedAccountCount);
        Assert.Equal(1, coverage.UnassignedAccountCount);
        Assert.Equal("40", coverage.CurrentDifference);
        Assert.Equal("30", coverage.ComparativeDifference);
    }

    [Fact]
    public void IncomeNotesUseExpenseDebitSignAndDoNotExposeComparatives()
    {
        var snapshot = Snapshot();
        var incomeCategory = new FinancialStatementCategory(1, "Expenses", "income_statement", "category",
            null, 1, true, "expenses", null, "expense", "-60", "-60", 1, null, "expense", null, null);
        var incomeView = View("income_statement", [incomeCategory]);
        var result = new FinancialStatementNotesService().Calculate(snapshot,
            Statement(incomeView, null), FinancialStatementSection.IncomeStatement,
            TestContext.Current.CancellationToken);

        var root = Assert.Single(result.Roots);
        Assert.Equal("60", root.CurrentAmount);
        Assert.Null(root.ComparativeAmount);
    }

    private static FinancialStatementResult Statement(FinancialStatementView? view = null,
        FinancialStatementView? comparative = null)
    {
        view ??= View("balance_sheet", [new FinancialStatementCategory(1, "Cash", "balance_sheet", "category",
            null, 1, true, "cash", "asset", null, "100", "100", 2, "asset", null, null, null)]);
        comparative ??= View("balance_sheet", [new FinancialStatementCategory(1, "Cash", "balance_sheet", "category",
            null, 1, true, "cash", "asset", null, "80", "80", 2, "asset", null, null, null)]);
        return new FinancialStatementResult(7, 5, "context", "not_confirmed", view, null, null,
            comparative, null, null);
    }

    private static FinancialStatementView View(string section, IReadOnlyList<FinancialStatementCategory> categories) =>
        new(section == "balance_sheet" ? "closing" : "closing", section, categories, [], [], [],
            new FinancialStatementControls("closing", "0", "0", "0", 2, "0", "0", "0", 0, 0,
                0, [], "0", "0"));

    private static ReportInputSnapshot Snapshot()
    {
        var accounts = new[]
        {
            Account(1, "100", "60", "50", true), Account(2, "200", "40", "30", true)
        };
        var categories = new[]
        {
            new ReportCategory(1, 7, "financial_statement", "cash", "Cash", "balance_sheet", "asset", null,
                "category", null, null, 1, true),
            new ReportCategory(10, 7, "note", "cash", "Cash note", null, null, null,
                "category", null, 1, 1, true),
            new ReportCategory(11, 7, "note", "cash-detail", "Cash detail", null, null, null,
                "category", 10, null, 1, true)
        };
        return new ReportInputSnapshot(
            new ReportInputScope(7, ReportInputKind.FinancialStatements, null, null, [], 5, null, null,
                null, null, null),
            new ReportInputIdentity("test", "fingerprint", "config", DateTime.UtcNow),
            Dataset<ReportLedgerUpload>([]), Dataset<ReportClientDataRow>([]),
            Dataset([new ReportTrialBalanceUpload(5, 7, new DateOnly(2026, 6, 30), "TB.xlsx", null, null,
                null, null, "{}")]), Dataset(accounts), Dataset<ReportTrialBalanceAdjustment>([]),
            Dataset([new ReportAccountAssignment(7, "100", null, 1, 11)]),
            Dataset<ReportCategoryMapping>([]), Dataset(categories), Dataset<ReportStatementCalculation>([]),
            Dataset<ReportStatementComponent>([]), Dataset<ReportProfitLink>([]), Dataset<ReportProfitStatus>([]),
            Dataset<ReportCashflowSettings>([]), Dataset<ReportCashflowAccountAssignment>([]),
            Dataset<ReportCashflowRoleCategory>([]), Dataset<ReportCashflowTreasuryAccount>([]),
            Dataset<ReportCashflowColumnMapping>([]));
    }

    private static ReportTrialBalanceAccount Account(int id, string code, string closingDebit, string openingDebit, bool leaf) =>
        new(id, 5, code, null, "Account", null, 1, leaf ? "leaf" : "group", true,
            id, null, null, null, Amount(openingDebit), Amount("0"), Amount("0"), Amount("0"),
            Amount(closingDebit), Amount("0"));

    private static ReportAmountValue Amount(string value) => ReportAmountValue.From(value, "numeric");
    private static ReportInputDataset<T> Dataset<T>(IReadOnlyList<T> items) => new(true, true, items.Count, 0, items);
}
