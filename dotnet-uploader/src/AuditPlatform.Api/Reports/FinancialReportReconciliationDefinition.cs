namespace AuditPlatform.Api.Reports;

internal sealed record FinancialReportPeriodReconciliation(string PeriodKey, string Status,
    string? LedgerOpening, string? LedgerClosing, string? LedgerMovement,
    string? CashflowMovement, string? CalculatedClosing, string? Difference);
internal sealed record FinancialReportAccountReconciliation(string AccountCode, string AccountName,
    string TreasuryType, IReadOnlyList<FinancialReportPeriodReconciliation> Periods);
internal sealed record FinancialReportPostingFinding(string AccountCode, string VoucherId,
    string Status, int LedgerLineCount, int TransactionLineCount, IReadOnlyList<string> DifferenceFields);
internal sealed record FinancialReportReconciliationResult(string Status, string EvidenceFingerprint,
    string PostingStatus, IReadOnlyList<FinancialReportAccountReconciliation> Accounts,
    IReadOnlyList<FinancialReportPostingFinding> PostingFindings, IReadOnlyList<string> Diagnostics);
