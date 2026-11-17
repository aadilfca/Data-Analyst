namespace AuditPlatform.Api.Reports;

internal sealed record FinancialReportPeriodReconciliation(string PeriodKey, string Status,
    string? LedgerOpening, string? LedgerClosing, string? LedgerMovement,
    string? CashflowMovement, string? CalculatedClosing, string? Difference);
internal sealed record FinancialReportAccountReconciliation(string AccountCode, string AccountName,
    string TreasuryType, IReadOnlyList<FinancialReportPeriodReconciliation> Periods);
internal sealed record FinancialReportPostingFinding(string AccountCode, string TreasuryType, string VoucherId,
    string Status, int LedgerLineCount, int TransactionLineCount, IReadOnlyList<string> DifferenceFields)
{
    public double? LedgerMovement { get; init; }
    public double? TransactionMovement { get; init; }
    public double? MovementDifference { get; init; }
    public string Note { get; init; } = "";
    public IReadOnlyList<object> LedgerLines { get; init; } = [];
    public IReadOnlyList<object> TransactionLines { get; init; } = [];
    public IReadOnlyList<object> VoucherLines { get; init; } = [];
}
internal sealed record FinancialReportReconciliationResult(string Status, string EvidenceFingerprint,
    string PostingStatus, IReadOnlyList<FinancialReportAccountReconciliation> Accounts,
    IReadOnlyList<FinancialReportPostingFinding> PostingFindings, IReadOnlyList<string> Diagnostics)
{
    public IReadOnlyList<FinancialReportPresentationAdjustment> PresentationAdjustments { get; init; } = [];
    public IReadOnlyList<FinancialReportVoucherReview> VoucherReviews { get; init; } = [];
    public IReadOnlyList<FinancialReportAdjustmentEvidence> AdjustmentEvidence { get; init; } = [];
    public IReadOnlyDictionary<string,object?> WorkingPaper { get; init; } = new Dictionary<string,object?>();
}
internal sealed record FinancialReportPresentationAdjustment(string TreasuryView,string VoucherId,string PeriodKey,string Reason,
    string Label,string Category,string Receipts,string Payments);
internal sealed record FinancialReportVoucherReview(string TreasuryView,string VoucherId,string PeriodKey,string Reason);
internal sealed record FinancialReportAdjustmentEvidence(string TreasuryView,int SourceRowId,string VoucherId,string Date,
    string PeriodKey,string AccountCode,string AccountName,string Category,string Debit,string Credit,
    bool IsTreasury,bool CategoryOwned,bool IsInPeriod);
