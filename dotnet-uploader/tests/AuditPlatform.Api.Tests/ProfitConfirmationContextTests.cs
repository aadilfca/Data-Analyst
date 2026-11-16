using AuditPlatform.Api.FinancialStatements;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ProfitConfirmationContextTests
{
    [Fact]
    public void ExactToleranceWithCurrentConfirmationAllowsOneCapitalLink()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with
        {
            Calculations=[new(7,"total","99.99",[1,2])],
            IncomeCategories=[new(1,"category","100"),new(2,"category","-0.01")],
            TrialBalanceProfit="100"
        });

        Assert.True(result.Available);
        Assert.True(result.Reconciled);
        Assert.True(result.Applied);
        Assert.Equal("99.99",result.FinalProfit);
        Assert.Equal("100",result.TrialBalanceProfit);
        Assert.Equal("-0.01",result.Difference);
        Assert.Empty(result.Reasons);
    }

    [Fact]
    public void DifferenceAboveToleranceBlocksLink()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with
        {
            Calculations=[new(7,"total","99.98",[1])],
            IncomeCategories=[new(1,"category","100")],
            TrialBalanceProfit="100"
        });

        Assert.False(result.Reconciled);
        Assert.False(result.Applied);
        Assert.Contains("The final Income Statement result does not reconcile to mapped TB P&L accounts.",result.Reasons);
    }

    [Fact]
    public void NonzeroCategoryMustBeCoveredByFinalTotal()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with
        {
            Calculations=[new(7,"total","100",[1])],
            IncomeCategories=[new(1,"category","100"),new(2,"category","5")]
        });

        Assert.False(result.Applied);
        Assert.Contains("The final profit total omits nonzero Income Statement categories.",result.Reasons);
    }

    [Fact]
    public void InvalidDestinationAndUnmappedAccountsBlockLink()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with
        {
            TargetIsActiveEquityCategory=false,
            UnmappedNonzeroCount=1
        });

        Assert.False(result.Applied);
        Assert.Contains("Select an active Balance Sheet equity category in ACC.",result.Reasons);
        Assert.Contains("Resolve nonzero unmapped Trial Balance accounts before linking profit.",result.Reasons);
    }

    [Fact]
    public void MissingLinkConfigurationCannotApply()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with {Configured=false});

        Assert.False(result.Applied);
        Assert.Contains("Configure a profit-to-capital link in ACC.",result.Reasons);
    }

    [Fact]
    public void StaleContextRequiresCurrentClosingConfirmation()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with {ConfirmedContextKey="old"});

        Assert.Equal("not_confirmed",result.ClosingStatus);
        Assert.False(result.Applied);
        Assert.Contains("Confirm this TB closing status for the current source and configuration.",result.Reasons);
    }

    [Fact]
    public void AlreadyInCapitalDoesNotLinkProfitAgain()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with {ClosingStatus="already_in_capital"});

        Assert.False(result.Applied);
        Assert.Contains("Profit is already included in ERP capital; no additional amount is linked.",result.Reasons);
    }

    [Fact]
    public void MissingFinalTotalBlocksLink()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with {Calculations=[]});

        Assert.False(result.Applied);
        Assert.Contains("Select a complete final Profit after taxation total in ACC.",result.Reasons);
    }

    [Fact]
    public void InvalidAmountsReturnUnavailableWithoutApplying()
    {
        var result=ProfitConfirmationContext.Evaluate(Valid() with
        {
            Calculations=[new(7,"total","NaN",[1])]
        });

        Assert.False(result.Available);
        Assert.False(result.Applied);
        Assert.Contains("Profit confirmation amounts cannot be evaluated safely.",result.Reasons);
    }

    private static ProfitConfirmationInput Valid()=>new(
        true,7,12,true,
        [new(7,"total","100",[1])],
        [new(1,"category","100")],
        "100",0,"not_in_capital","context-v1","context-v1");
}
