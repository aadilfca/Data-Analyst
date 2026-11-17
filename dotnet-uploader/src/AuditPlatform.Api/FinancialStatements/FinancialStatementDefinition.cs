namespace AuditPlatform.Api.FinancialStatements;

internal enum FinancialStatementSection { BalanceSheet, IncomeStatement }

internal sealed record FinancialStatementResult(int ClientId, int TrialBalanceUploadId,
    string ContextKey, string ClosingStatus, FinancialStatementView Statement,
    FinancialStatementProfitLink? ProfitLink, FinancialStatementTransferReconciliation? TransferReconciliation,
    FinancialStatementView? Comparative, FinancialStatementOpeningReview? OpeningReview,
    FinancialStatementProfitBridge? ProfitBridge);

internal sealed record FinancialStatementView(string Basis, string Section,
    IReadOnlyList<FinancialStatementCategory> Categories,
    IReadOnlyList<FinancialStatementFormula> Calculations,
    IReadOnlyList<FinancialStatementDiagnostic> Diagnostics,
    IReadOnlyList<FinancialStatementOpeningIncomeItem> OpeningIncomeItems,
    FinancialStatementControls Controls);

internal sealed record FinancialStatementCategory(int Id, string Label, string Section, string RowType,
    int? ParentCategoryId, int SortOrder, bool IsActive, string CategoryKey,
    string? PresentationRole, string? IncomeStatementRole, string? RawNet, string? Amount,
    int AccountCount, string? EffectivePresentationRole, string? EffectiveIncomeStatementRole,
    string? ErpAmount, string? LinkedProfit);

internal sealed record FinancialStatementFormula(int Id, string Label, string Section, string Type,
    int? ParentHeadingId, int SortOrder, string? Amount,
    IReadOnlyList<FinancialStatementFormulaComponent> Components,
    IReadOnlyList<int> SourceCategoryIds,
    IReadOnlyDictionary<int, int> SourceCategoryCoefficients);

internal sealed record FinancialStatementFormulaComponent(int? SourceCategoryId,
    int? SourceCalculationId, string Operator);

internal sealed record FinancialStatementDiagnostic(string Code, string Message, int? Count = null);

internal sealed record FinancialStatementControls(string Basis, string Debit, string Credit, string Difference,
    int LeafCount, string OpeningIncomeStatementNet, string PeriodIncomeStatementNet,
    string UnmappedNet, int IncomeStatementAccountCount, int UnmappedCount,
    int UnmappedNonzeroCount, IReadOnlyList<string> UnmappedAccountCodes,
    string IncomeStatementNet, string AuditAdjustmentNet);

internal sealed record FinancialStatementOpeningIncomeItem(string AccountCode, string Category,
    string OpeningDebit, string OpeningCredit, string EquityEffect);

internal sealed record FinancialStatementProfitLink(bool Configured, int? SourceCalculationId,
    int? TargetCategoryId, string? FinalProfit, string? TrialBalanceProfit, string? Difference,
    bool Reconciled, string ClosingStatus, bool Applied, IReadOnlyList<string> Reasons);

internal sealed record FinancialStatementOpeningReview(string Amount, int AccountCount,
    int? TargetCategoryId, bool Included, IReadOnlyList<FinancialStatementOpeningIncomeItem> Accounts);

internal sealed record FinancialStatementProfitBridge(string Opening, string Movement,
    string ErpPeriod, string Closing, string Difference, bool Reconciled);

internal sealed record FinancialStatementTransferReconciliation(string Status, string Label,
    bool Reconciled, bool EquationBalanced, string SourceImbalance, string? StatementDifference,
    string? UnexplainedDifference, string Tolerance, IReadOnlyList<FinancialStatementTransferRow> Rows,
    IReadOnlyList<string> Issues, IReadOnlyDictionary<string, FinancialStatementTransferTotal?> Totals,
    bool OpeningReviewRequired, FinancialStatementProfitBridge? ProfitBridge = null);

internal sealed record FinancialStatementTransferRow(string Key, string Label, string? TrialBalance,
    string? Statement);

internal sealed record FinancialStatementTransferTotal(int Id, string Label,
    IReadOnlyDictionary<int, int> CategoryCoefficients);

internal sealed class FinancialStatementInputException(string message) : Exception(message);
