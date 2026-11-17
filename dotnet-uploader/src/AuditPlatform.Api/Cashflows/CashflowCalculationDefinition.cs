using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Cashflows;

internal sealed record CashflowPeriod([property: JsonPropertyName("key")] string Key,
    [property: JsonPropertyName("start")] string Start, [property: JsonPropertyName("end")] string End,
    [property: JsonPropertyName("label")] string Label);
internal sealed record CashflowCategoryTotal([property: JsonPropertyName("category")] string Category,
    [property: JsonPropertyName("periods")] IReadOnlyDictionary<string, string> Periods,
    [property: JsonPropertyName("total")] string Total);
internal sealed record CashflowPeriodTotal([property: JsonPropertyName("period_key")] string PeriodKey,
    [property: JsonPropertyName("receipts")] string Receipts, [property: JsonPropertyName("payments")] string Payments,
    [property: JsonPropertyName("net_movement")] string NetMovement);
internal sealed record CashflowVoucherControl([property: JsonPropertyName("voucher_id")] string VoucherId,
    [property: JsonPropertyName("period_key")] string PeriodKey,
    [property: JsonPropertyName("treasury_movement")] string TreasuryMovement,
    [property: JsonPropertyName("counter_movement")] string CounterMovement,
    [property: JsonPropertyName("variance")] string Variance, [property: JsonPropertyName("balanced")] bool Balanced);
internal sealed record CashflowMovementProof([property: JsonPropertyName("period_key")] string PeriodKey,
    [property: JsonPropertyName("period_label")] string PeriodLabel,
    [property: JsonPropertyName("treasury_type")] string TreasuryType,
    [property: JsonPropertyName("movement")] string Movement,
    [property: JsonPropertyName("internal_transfer_movement")] string InternalTransferMovement,
    [property: JsonPropertyName("mixed_voucher_movement")] string MixedVoucherMovement,
    [property: JsonPropertyName("explained_movement")] string ExplainedMovement,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("source_movement")] string SourceMovement,
    [property: JsonPropertyName("category_movement")] string CategoryMovement,
    [property: JsonPropertyName("unallocated_category_movement")] string UnallocatedCategoryMovement,
    [property: JsonPropertyName("variance")] string Variance,
    [property: JsonPropertyName("mixed_voucher_count")] int MixedVoucherCount,
    [property: JsonPropertyName("allocation_status")] string AllocationStatus);
internal sealed record CashflowPostingEvidence([property: JsonPropertyName("source_row_id")] int SourceRowId,
    [property: JsonPropertyName("voucher_id")] string VoucherId, [property: JsonPropertyName("date")] string Date,
    [property: JsonPropertyName("account_code")] string AccountCode,
    [property: JsonPropertyName("debit")] string Debit, [property: JsonPropertyName("credit")] string Credit);
internal sealed record CashflowTransferDetail(string VoucherId, string Date, string PeriodKey,
    string PeriodLabel, string TransferType, string TransferorAccountCode, string TransferorAccountName,
    string TransferorType, string TransfereeAccountCode, string TransfereeAccountName,
    string TransfereeType, string TransferorAmount, string TransfereeAmount, string Amount,
    string Variance, string Classification, string Status, bool Matched, int TransferorLegCount,
    int TransfereeLegCount, int NonTreasuryLineCount, string VoucherDetail, string BankMovement, string CashMovement);
internal sealed record CashflowCustomerPaymentDetail(string VoucherId, string Date, string PeriodKey,
    string PeriodLabel, string CustomerAccountCode, string CustomerAccountName, string BankAccountCode,
    string BankAccountName, string BankAccountType, string Method, string ConfiguredPresentation,
    string CustomerGrossAmount, string CustomerReceipts, string CustomerReversals, string CashflowAmount,
    string BankStatementAmount, string BankCharges, string WithholdingTax, string OtherDeductions,
    string DeductionsTotal, string TechnicalBankOffset, string? NetBankSettlement,
    string ExpectedNetSettlement, string? Variance, string Status, string VoucherDetail);
internal sealed record CashflowTreasuryCategoryTotal(string Category, string PeriodKey,
    string BankAmount, string CashAmount, string MixedAmount, string TotalAmount);
internal sealed record CashflowReviewLine(string VoucherId, string Date, string PeriodKey,
    string PeriodLabel, string AccountCode, string AccountName, string Debit, string Credit,
    string SignedAmount, string Reason);
internal sealed record CashflowSelectionReview(string VoucherId,string Date,string PeriodKey,string PeriodLabel,
    string Reason,string Receipts,string Payments);
internal sealed record CashflowVendorPaymentDetail(string VoucherId, string Date, string PeriodKey,
    string PeriodLabel, string AccountCode, string AccountName, string Category, string Payments,
    string Reversals, string NetPayments, string CashflowAmount, string VoucherDetail);
internal sealed record CashflowVendorPaymentTotal(string AccountCode, string AccountName, string Category,
    string Payments, string Reversals, string NetPayments, string CashflowAmount, int VoucherCount,
    string? PeriodKey = null, string? PeriodLabel = null);
internal sealed record CashflowCustomerPaymentOption(string AccountCode, string AccountName);
internal sealed record CashflowDeductionDetail(string Category, string Month, string PeriodKey, string PeriodLabel,
    string Amount, string AccountCode, string AccountName, string Date, string Presentation);
internal sealed record CashflowPaymentSummary(string CustomerAccountCode, string CustomerAccountName,
    string BankAccountType, int VoucherCount, int MatchedVoucherCount, int ReviewVoucherCount,
    string CustomerGrossAmount, string CustomerReceipts, string CustomerReversals, string CashflowAmount,
    string BankCharges, string WithholdingTax, string OtherDeductions, string DeductionsTotal,
    string TechnicalBankOffset, string ExpectedNetSettlement, string NetBankSettlement,
    string StatementEligibleActualSettlement, string Variance, string Status,
    string? PeriodKey = null, string? PeriodLabel = null);
internal sealed record CashflowPaymentPeriodTotal(string PeriodKey, string PeriodLabel, string PeriodStart,
    string PeriodEnd, int VoucherCount, int MatchedVoucherCount, int ReviewVoucherCount,
    string CustomerGrossAmount, string CustomerReceipts, string CustomerReversals, string CashflowAmount,
    string BankCharges, string WithholdingTax, string OtherDeductions, string DeductionsTotal,
    string TechnicalBankOffset, string ExpectedNetSettlement, string NetBankSettlement,
    string StatementEligibleActualSettlement, string MainReceivedFromCustomersTotal,
    string Difference, string Status);
internal sealed record CashflowTransferReconciliation(int CandidateVoucherCount, int ConfirmedCount,
    int MixedCount, int AmbiguousCount, string TransferorTotal, string TransfereeTotal,
    string VarianceTotal, string Status);
internal sealed record CashflowCustomerPaymentReconciliation(string StatementEligibleCustomerTotal,
    string MainReceivedFromCustomersTotal, string Difference, string Status, int CustomerCount,
    int MatchedVoucherCount, int ReviewVoucherCount, string Tolerance);
internal sealed record CashflowTreasuryReconciliation(string Status, string MaxAbsVariance,
    string MixedAmount, int MixedRowCount, string AllocationPolicy, string SourceStatus,
    string SourceMaxAbsVariance, string AllocationStatus);
internal sealed record CashflowPeriodMetric(string PeriodKey, int SourceLineCount,
    int SelectedVoucherCount, int InternalTransferCount, int CategoryReviewCount, int DeductionCount);
internal sealed record CashflowLedgerRollup(
    [property: JsonPropertyName("category")] string Category,
    [property: JsonPropertyName("period_key")] string PeriodKey,
    [property: JsonPropertyName("period_label")] string PeriodLabel,
    [property: JsonPropertyName("account_code")] string AccountCode,
    [property: JsonPropertyName("account_name")] string AccountName,
    [property: JsonPropertyName("treasury_type")] string TreasuryType,
    [property: JsonPropertyName("amount")] string Amount,
    [property: JsonPropertyName("line_count")] int LineCount);
internal sealed record CashflowReconciliationLine(int SourceRowId,string VoucherId,string Date,string PeriodKey,
    string AccountCode,string AccountName,string Category,string Debit,string Credit,bool IsTreasury,bool CategoryOwned,
    bool IsInPeriod,string CategoryTreasuryType);
internal sealed record CashflowCalculationResult(
    [property: JsonPropertyName("client_id")] int ClientId,
    [property: JsonPropertyName("input_fingerprint")] string InputFingerprint,
    [property: JsonPropertyName("group_by")] string GroupBy,
    [property: JsonPropertyName("treasury_view")] string TreasuryView,
    [property: JsonPropertyName("receipt_presentation")] string ReceiptPresentation,
    [property: JsonPropertyName("periods")] IReadOnlyList<CashflowPeriod> Periods,
    [property: JsonPropertyName("period_totals")] IReadOnlyList<CashflowPeriodTotal> PeriodTotals,
    [property: JsonPropertyName("category_totals")] IReadOnlyList<CashflowCategoryTotal> CategoryTotals,
    [property: JsonPropertyName("voucher_controls")] IReadOnlyList<CashflowVoucherControl> VoucherControls,
    [property: JsonPropertyName("treasury_movement_proof")] IReadOnlyList<CashflowMovementProof> TreasuryMovementProof,
    [property: JsonPropertyName("diagnostics")] IReadOnlyList<string> Diagnostics,
    [property: JsonPropertyName("total_receipts")] string TotalReceipts,
    [property: JsonPropertyName("total_payments")] string TotalPayments,
    [property: JsonPropertyName("treasury_postings")] IReadOnlyList<CashflowPostingEvidence> TreasuryPostings)
{
    [JsonPropertyName("transfer_details")] public IReadOnlyList<CashflowTransferDetail> TransferDetails { get; init; } = [];
    [JsonPropertyName("customer_payment_details")] public IReadOnlyList<CashflowCustomerPaymentDetail> CustomerPaymentDetails { get; init; } = [];
    [JsonPropertyName("treasury_category_totals")] public IReadOnlyList<CashflowTreasuryCategoryTotal> TreasuryCategoryTotals { get; init; } = [];
    [JsonPropertyName("categories_needing_review")] public IReadOnlyList<CashflowReviewLine> CategoriesNeedingReview { get; init; } = [];
    [JsonPropertyName("selection_review_details")] public IReadOnlyList<CashflowSelectionReview> SelectionReviewDetails { get; init; } = [];
    [JsonPropertyName("vendor_payment_details")] public IReadOnlyList<CashflowVendorPaymentDetail> VendorPaymentDetails { get; init; } = [];
    [JsonPropertyName("vendor_payment_vendor_totals")] public IReadOnlyList<CashflowVendorPaymentTotal> VendorPaymentVendorTotals { get; init; } = [];
    [JsonPropertyName("vendor_payment_period_totals")] public IReadOnlyList<CashflowVendorPaymentTotal> VendorPaymentPeriodTotals { get; init; } = [];
    [JsonPropertyName("customer_payment_customer_options")] public IReadOnlyList<CashflowCustomerPaymentOption> CustomerPaymentCustomerOptions { get; init; } = [];
    [JsonPropertyName("deduction_details")] public IReadOnlyList<CashflowDeductionDetail> DeductionDetails { get; init; } = [];
    [JsonPropertyName("customer_payment_customer_totals")] public IReadOnlyList<CashflowPaymentSummary> CustomerPaymentCustomerTotals { get; init; } = [];
    [JsonPropertyName("customer_payment_account_period_totals")] public IReadOnlyList<CashflowPaymentSummary> CustomerPaymentAccountPeriodTotals { get; init; } = [];
    [JsonPropertyName("customer_payment_period_totals")] public IReadOnlyList<CashflowPaymentPeriodTotal> CustomerPaymentPeriodTotals { get; init; } = [];
    [JsonPropertyName("customer_payment_reconciliation")] public CashflowCustomerPaymentReconciliation? CustomerPaymentReconciliation { get; init; }
    [JsonPropertyName("transfer_reconciliation")] public CashflowTransferReconciliation? TransferReconciliation { get; init; }
    [JsonPropertyName("treasury_reconciliation")] public CashflowTreasuryReconciliation? TreasuryReconciliation { get; init; }
    [JsonPropertyName("period_metrics")] public IReadOnlyDictionary<string, CashflowPeriodMetric> PeriodMetrics { get; init; } = new Dictionary<string, CashflowPeriodMetric>();
    [JsonPropertyName("ledger_rollups")] public IReadOnlyList<CashflowLedgerRollup> LedgerRollups { get; init; } = [];
    [JsonPropertyName("selected_voucher_count")] public int SelectedVoucherCount { get; init; }
    [JsonPropertyName("source_line_count")] public int SourceLineCount { get; init; }
    [JsonPropertyName("internal_transfer_count")] public int InternalTransferCount { get; init; }
    [JsonPropertyName("category_review_count")] public int CategoryReviewCount { get; init; }
    [JsonPropertyName("deduction_count")] public int DeductionCount { get; init; }
    [JsonIgnore] internal IReadOnlyList<CashflowReconciliationLine> ReconciliationLines { get; init; } = [];
}
