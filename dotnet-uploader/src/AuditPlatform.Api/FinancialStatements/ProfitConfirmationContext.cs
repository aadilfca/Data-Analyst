using System.Runtime.CompilerServices;
using Amount=AuditPlatform.Api.Uploads.UploadExactAmount;

[assembly: InternalsVisibleTo("AuditPlatform.Api.Tests")]

namespace AuditPlatform.Api.FinancialStatements;

internal sealed record ProfitConfirmationCalculation(
    int Id,
    string CalculationType,
    string? Amount,
    IReadOnlyList<int> SourceCategoryIds);

internal sealed record ProfitConfirmationCategory(
    int Id,
    string RowType,
    string? RawNet);

internal sealed record ProfitConfirmationInput(
    bool Configured,
    int? SourceCalculationId,
    int? TargetCategoryId,
    bool TargetIsActiveEquityCategory,
    IReadOnlyList<ProfitConfirmationCalculation> Calculations,
    IReadOnlyList<ProfitConfirmationCategory> IncomeCategories,
    string? TrialBalanceProfit,
    int UnmappedNonzeroCount,
    string? ClosingStatus,
    string? ContextKey,
    string? ConfirmedContextKey);

internal sealed record ProfitConfirmationResult(
    bool Available,
    bool Configured,
    int? SourceCalculationId,
    int? TargetCategoryId,
    string? FinalProfit,
    string? TrialBalanceProfit,
    string? Difference,
    bool Reconciled,
    string ClosingStatus,
    bool Applied,
    IReadOnlyList<string> Reasons);

/// <summary>Evaluates whether the configured current-period profit can be linked once to equity.</summary>
internal static class ProfitConfirmationContext
{
    private static readonly Amount ReconciliationTolerance=Amount.Parse("0.01");

    internal static ProfitConfirmationResult Evaluate(ProfitConfirmationInput input)
    {
        ArgumentNullException.ThrowIfNull(input);
        if(input.UnmappedNonzeroCount<0)throw new ArgumentOutOfRangeException(nameof(input));

        Amount? final=null;
        Amount independent;
        Amount? difference=null;
        HashSet<int> required;
        HashSet<int> covered;
        ProfitConfirmationCalculation? sourceCalculation;
        string status;
        var reasons=new List<string>();
        bool confirmed=!string.IsNullOrEmpty(input.ContextKey)
            &&StringComparer.Ordinal.Equals(input.ContextKey,input.ConfirmedContextKey);
        status=confirmed?input.ClosingStatus??string.Empty:"not_confirmed";

        try
        {
            sourceCalculation=input.Calculations.FirstOrDefault(row=>row.Id==input.SourceCalculationId&&row.CalculationType=="total");
            if(sourceCalculation?.Amount is not null)final=Amount.Parse(sourceCalculation.Amount);
            independent=input.TrialBalanceProfit is null?Amount.Zero:Amount.Parse(input.TrialBalanceProfit);
            if(final is not null)difference=final.Value-independent;
            required=input.IncomeCategories
                .Where(row=>row.RowType=="category"&&ParseNullable(row.RawNet).CompareTo(Amount.Zero)!=0)
                .Select(row=>row.Id)
                .ToHashSet();
            covered=sourceCalculation?.SourceCategoryIds.ToHashSet()??[];
        }
        catch(FormatException)
        {
            return new(false,input.Configured,input.SourceCalculationId,input.TargetCategoryId,
                null,null,null,false,status,false,["Profit confirmation amounts cannot be evaluated safely."]);
        }

        if(sourceCalculation is null||final is null)
            reasons.Add("Select a complete final Profit after taxation total in ACC.");
        if(!input.Configured)
            reasons.Add("Configure a profit-to-capital link in ACC.");
        if(input.TargetCategoryId is null||!input.TargetIsActiveEquityCategory)
            reasons.Add("Select an active Balance Sheet equity category in ACC.");

        if(!required.IsSubsetOf(covered))
            reasons.Add("The final profit total omits nonzero Income Statement categories.");
        if(difference is not null&&difference.Value.Abs().CompareTo(ReconciliationTolerance)>0)
            reasons.Add("The final Income Statement result does not reconcile to mapped TB P&L accounts.");
        if(input.UnmappedNonzeroCount>0)
            reasons.Add("Resolve nonzero unmapped Trial Balance accounts before linking profit.");
        if(status=="not_confirmed")
            reasons.Add("Confirm this TB closing status for the current source and configuration.");
        else if(status=="already_in_capital")
            reasons.Add("Profit is already included in ERP capital; no additional amount is linked.");
        else if(status!="not_in_capital")
            reasons.Add("The TB closing status is invalid.");

        return new(true,input.Configured,input.SourceCalculationId,input.TargetCategoryId,
            final?.ToFixedString(),independent.ToFixedString(),difference?.ToFixedString(),
            difference is not null&&difference.Value.Abs().CompareTo(ReconciliationTolerance)<=0,
            status,reasons.Count==0,reasons);
    }

    private static Amount ParseNullable(string? value)
        =>value is null?Amount.Zero:Amount.Parse(value);
}
