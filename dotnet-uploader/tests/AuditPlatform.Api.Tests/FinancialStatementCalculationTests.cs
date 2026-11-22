using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Reports;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class FinancialStatementCalculationTests
{
    [Fact]
    public void BalanceSheetPreservesSignsProfitLinkComparativeAndTransferProof()
    {
        var snapshot=BalancedSnapshot();

        var result=new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.BalanceSheet,
            TestContext.Current.CancellationToken);

        Assert.Equal("not_in_capital",result.ClosingStatus);
        Assert.True(result.ProfitLink!.Applied);
        Assert.Equal("20",result.ProfitLink.FinalProfit);
        Assert.Equal("90",Category(result.Statement,1).Amount);
        Assert.Equal("70",Category(result.Statement,3).Amount);
        Assert.Equal("80",Category(result.Comparative!,1).Amount);
        Assert.Equal("40",Category(result.Comparative!,3).Amount);
        Assert.Equal("20",result.ProfitBridge!.Movement);
        Assert.True(result.ProfitBridge.Reconciled);
        Assert.Equal("reconciled",result.TransferReconciliation!.Status);
        Assert.Equal("-50",result.TransferReconciliation.Rows.Single(row=>row.Key=="equity").Statement);
        Assert.Equal("-20",result.TransferReconciliation.Rows.Single(row=>row.Key=="profit").Statement);
    }

    [Fact]
    public void BalanceSheetMatchesLegacyGoldenAmountsForTheControlledTrialBalance()
    {
        // Golden values were captured from Python's build_balance_sheet using this same synthetic source.
        // Keep them here as data so the C# runtime and tests never call Python.
        var result=new FinancialStatementCalculationService().Calculate(BalancedSnapshot(),
            FinancialStatementSection.BalanceSheet,TestContext.Current.CancellationToken);

        Assert.Equal(("90","90"),(Category(result.Statement,1).RawNet,Category(result.Statement,1).Amount));
        Assert.Equal(("-20","20"),(Category(result.Statement,2).RawNet,Category(result.Statement,2).Amount));
        Assert.Equal(("-50","70"),(Category(result.Statement,3).RawNet,Category(result.Statement,3).Amount));
        Assert.Equal("90",result.Statement.Calculations.Single(row=>row.Id==10).Amount);
        Assert.Equal("90",result.Statement.Calculations.Single(row=>row.Id==11).Amount);
        Assert.Equal("80",Category(result.Comparative!,1).Amount);
        Assert.Equal("20",Category(result.Comparative!,2).Amount);
        Assert.Equal("40",Category(result.Comparative!,3).Amount);
        Assert.Equal("0",result.ProfitLink!.Difference);
        Assert.True(result.ProfitLink.Reconciled);
        Assert.Equal("reconciled",result.TransferReconciliation!.Status);
        Assert.Equal("0",result.TransferReconciliation.UnexplainedDifference);
    }

    [Fact]
    public void IncomeStatementUsesCreditPositiveIncomeAndDebitNegativeExpense()
    {
        var result=new FinancialStatementCalculationService().Calculate(BalancedSnapshot(),
            FinancialStatementSection.IncomeStatement,TestContext.Current.CancellationToken);

        Assert.Equal("30",Category(result.Statement,4).Amount);
        Assert.Equal("-10",Category(result.Statement,5).Amount);
        Assert.Equal("20",result.ProfitLink!.FinalProfit);
        Assert.DoesNotContain(result.Statement.Diagnostics,row=>row.Code=="unclosed_profit_loss");
    }

    [Fact]
    public void StaleConfirmationDoesNotApplyProfitToEquity()
    {
        var snapshot=BalancedSnapshot();
        snapshot=snapshot with { ProfitStatuses=Dataset([new ReportProfitStatus(1,7,"not_in_capital","stale")]) };

        var result=new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.BalanceSheet,
            TestContext.Current.CancellationToken);

        Assert.False(result.ProfitLink!.Applied);
        Assert.Contains("not_confirmed",result.ProfitLink.ClosingStatus);
        Assert.Equal("50",Category(result.Statement,3).Amount);
        Assert.Contains(result.Statement.Diagnostics,row=>row.Code=="profit_link");
    }

    [Fact]
    public void CircularFormulaSourceIsRejected()
    {
        var snapshot=BalancedSnapshot();
        var calculations=snapshot.StatementCalculations.Items.ToArray();
        calculations[2]=calculations[2] with { CalculationType="total" };
        var components=snapshot.StatementComponents.Items.Concat([
            new ReportStatementComponent(20,null,21,"add",0),
            new ReportStatementComponent(21,null,20,"add",0)
        ]).ToArray();
        snapshot=snapshot with
        {
            StatementCalculations=Dataset(calculations.Concat([new ReportStatementCalculation(21,7,"income_statement","total","Cycle",null,2)]).ToArray()),
            StatementComponents=Dataset(components),
            ProfitStatuses=Dataset<ReportProfitStatus>(Array.Empty<ReportProfitStatus>())
        };

        var error=Assert.Throws<FinancialStatementInputException>(()=>
            new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.IncomeStatement,
                TestContext.Current.CancellationToken));

        Assert.Contains("circular source",error.Message);
    }

    [Fact]
    public void IncompleteSnapshotIsRejectedWithoutPartialCalculation()
    {
        var snapshot=BalancedSnapshot() with
        { TrialBalanceAdjustments=new ReportInputDataset<ReportTrialBalanceAdjustment>(true,false,0,0,[]) };

        var error=Assert.Throws<FinancialStatementInputException>(()=>
            new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.IncomeStatement,
                TestContext.Current.CancellationToken));

        Assert.Contains("complete Trial Balance adjustments snapshot",error.Message);
    }

    [Fact]
    public void OverlappingFormulaLineageIsRejected()
    {
        var snapshot=BalancedSnapshot();
        snapshot=snapshot with { StatementComponents=Dataset(snapshot.StatementComponents.Items.Concat([
            new ReportStatementComponent(10,1,null,"add",1)
        ]).ToArray()) };

        var error=Assert.Throws<FinancialStatementInputException>(()=>
            new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.BalanceSheet,
                TestContext.Current.CancellationToken));

        Assert.Contains("more than once",error.Message);
    }

    [Fact]
    public void MissingPresentationRoleProducesNullAmountsAndDiagnostic()
    {
        var snapshot=BalancedSnapshot();
        var categories=snapshot.Categories.Items.ToArray();
        categories[0]=categories[0] with { PresentationRole=null };
        snapshot=snapshot with { Categories=Dataset(categories) };

        var result=new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.BalanceSheet,
            TestContext.Current.CancellationToken);

        Assert.Null(Category(result.Statement,1).Amount);
        Assert.Contains(result.Statement.Diagnostics,row=>row.Code=="missing_presentation");
        Assert.Equal("not_reconciled",result.TransferReconciliation!.Status);
    }

    [Fact]
    public void UnmappedAndInactiveLeafAccountsRemainVisibleForReview()
    {
        var snapshot=BalancedSnapshot();
        snapshot=snapshot with { AccountAssignments=Dataset(snapshot.AccountAssignments.Items.Skip(1).ToArray()) };
        var unmapped=new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.BalanceSheet,
            TestContext.Current.CancellationToken);
        Assert.Equal(1,unmapped.Statement.Controls.UnmappedCount);
        Assert.Equal("90",unmapped.Statement.Controls.UnmappedNet);
        Assert.Contains(unmapped.Statement.Diagnostics,row=>row.Code=="unmapped_accounts");

        snapshot=BalancedSnapshot();
        var categories=snapshot.Categories.Items.ToArray();
        categories[0]=categories[0] with { IsActive=false };
        snapshot=snapshot with { Categories=Dataset(categories) };
        var inactive=new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.BalanceSheet,
            TestContext.Current.CancellationToken);
        Assert.Equal("90",Category(inactive.Statement,1).Amount);
        Assert.Contains(inactive.Statement.Diagnostics,row=>row.Code=="inactive_assignments");
    }

    [Fact]
    public void NonFiniteAndOutOfRangeAmountsAreRejectedSafely()
    {
        foreach(var invalid in new[]{"NaN","1e1001"})
        {
            var snapshot=BalancedSnapshot();
            var accounts=snapshot.TrialBalanceAccounts.Items.ToArray();
            accounts[0]=accounts[0] with { ClosingDebit=new ReportAmountValue(invalid,"numeric",invalid) };
            snapshot=snapshot with { TrialBalanceAccounts=Dataset(accounts) };

            var error=Assert.Throws<FinancialStatementInputException>(()=>
                new FinancialStatementCalculationService().Calculate(snapshot,FinancialStatementSection.IncomeStatement,
                    TestContext.Current.CancellationToken));

            Assert.Contains("cannot be evaluated safely",error.Message);
        }
    }

    [Fact]
    public void PythonCompatibleContextKeyMatchesGoldenVector()
    {
        var snapshot=BalancedSnapshot();
        var key=FinancialStatementCalculationService.FinancialStatementContextKey(
            snapshot.TrialBalanceAccounts.Items,snapshot.AccountAssignments.Items,snapshot.Categories.Items,
            snapshot.StatementCalculations.Items,snapshot.StatementComponents.Items,snapshot.ProfitLinks.Items.Single());

        Assert.Equal("4c28e06f752e4622341aeb203eda5435abef4f6ff84c6b7a9026c8de439fae68",key);
    }

    [Fact]
    public void PythonContextHashPreservesFloatNotationAndUnicode()
    {
        var snapshot=BalancedSnapshot();
        var accounts=snapshot.TrialBalanceAccounts.Items.ToArray();
        accounts[0]=accounts[0] with
        {
            AccountCode="Å-01",
            OpeningDebit=new ReportAmountValue("1e-07","real","0.0000001")
        };
        var assignments=snapshot.AccountAssignments.Items.ToArray();
        assignments[0]=assignments[0] with { AccountCode="Å-01" };
        var categories=snapshot.Categories.Items.ToArray();
        categories[0]=categories[0] with { Label="Café" };

        var key=FinancialStatementCalculationService.FinancialStatementContextKey(accounts,assignments,categories,
            snapshot.StatementCalculations.Items,snapshot.StatementComponents.Items,snapshot.ProfitLinks.Items.Single());

        Assert.Equal("57ae555f5db022282c24141aba838573f44e66af866279752746c3e43647c58d",key);
    }

    private static FinancialStatementCategory Category(FinancialStatementView view,int id)
        =>view.Categories.Single(row=>row.Id==id);

    private static ReportInputSnapshot BalancedSnapshot()
    {
        var accounts=new[]
        {
            Account(1,"1000",90,0,80,0,10,0),
            Account(2,"2000",0,20,0,20,0,0),
            Account(3,"3000",0,50,0,40,0,10),
            Account(4,"4000",0,30,0,0,0,30),
            Account(5,"5000",10,0,0,0,10,0)
        };
        var categories=new[]
        {
            CategoryRow(1,"Cash","balance_sheet","asset"),
            CategoryRow(2,"Payables","balance_sheet","liability"),
            CategoryRow(3,"Capital","balance_sheet","equity"),
            CategoryRow(4,"Revenue","income_statement",null,"income"),
            CategoryRow(5,"Expenses","income_statement",null,"expense")
        };
        var assignments=accounts.Select((row,index)=>new ReportAccountAssignment(7,row.AccountCode,null,index+1,null)).ToArray();
        var calculations=new[]
        {
            new ReportStatementCalculation(10,7,"balance_sheet","total","Assets",null,0),
            new ReportStatementCalculation(11,7,"balance_sheet","total","Equity and Liabilities",null,1),
            new ReportStatementCalculation(20,7,"income_statement","total","Profit after taxation",null,0)
        };
        var components=new[]
        {
            new ReportStatementComponent(10,1,null,"add",0),
            new ReportStatementComponent(11,2,null,"add",0),new ReportStatementComponent(11,3,null,"add",1),
            new ReportStatementComponent(20,4,null,"add",0),new ReportStatementComponent(20,5,null,"add",1)
        };
        var profitLink=new ReportProfitLink(7,20,3);
        string context=FinancialStatementCalculationService.FinancialStatementContextKey(accounts,assignments,
            categories,calculations,components,profitLink);
        var scope=new ReportInputScope(7,ReportInputKind.FinancialStatements,null,null,[],1,null,null,null,null,null);
        var identity=new ReportInputIdentity("test","fingerprint","configuration",DateTime.UnixEpoch);
        return new(scope,identity,Dataset<ReportLedgerUpload>([]),Dataset<ReportClientDataRow>([]),
            Dataset([new ReportTrialBalanceUpload(1,7,new DateOnly(2026,6,30),"tb.xlsx",null,null,null,null,"{}")]),
            Dataset(accounts),Dataset<ReportTrialBalanceAdjustment>([]),Dataset(assignments),
            Dataset<ReportCategoryMapping>([]),Dataset(categories),Dataset(calculations),Dataset(components),
            Dataset([profitLink]),Dataset([new ReportProfitStatus(1,7,"not_in_capital",context)]),
            Dataset<ReportCashflowSettings>([]),Dataset<ReportCashflowAccountAssignment>([]),
            Dataset<ReportCashflowRoleCategory>([]),Dataset<ReportCashflowTreasuryAccount>([]),
            Dataset<ReportCashflowColumnMapping>([]));
    }

    private static ReportCategory CategoryRow(int id,string label,string section,string? presentation,string? income=null)
        =>new(id,7,"financial_statement",label.ToLowerInvariant().Replace(' ','_'),label,section,presentation,
            income,"category",null,null,id,true);

    private static ReportTrialBalanceAccount Account(int id,string code,int debit,int credit,
        int openingDebit,int openingCredit,int periodDebit,int periodCredit)
        =>new(id,1,code,code,code,null,0,"leaf",false,id,null,Value(debit),Value(credit),
            Value(openingDebit),Value(openingCredit),Value(periodDebit),Value(periodCredit),Value(debit),Value(credit));

    private static ReportAmountValue Value(int amount)=>ReportAmountValue.From(amount==0?"0":amount.ToString(),"real");
    private static ReportInputDataset<T> Dataset<T>(IReadOnlyList<T> rows)
        =>new(true,true,rows.Count,0,rows);
}
