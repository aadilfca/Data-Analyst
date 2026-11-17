using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Cashflows;

internal sealed record CashflowPeriod(string Key, string Start, string End, string Label);
internal sealed record CashflowCategoryTotal(string Category,
    IReadOnlyDictionary<string, string> Periods, string Total);
internal sealed record CashflowPeriodTotal(string PeriodKey, string Receipts, string Payments, string NetMovement);
internal sealed record CashflowVoucherControl(string VoucherId, string PeriodKey,
    string TreasuryMovement, string CounterMovement, string Variance, bool Balanced);
internal sealed record CashflowMovementProof(string PeriodKey, string PeriodLabel,
    string TreasuryType, string Movement, string InternalTransferMovement,
    string MixedVoucherMovement, string ExplainedMovement, string Status,
    string SourceMovement, string CategoryMovement, string Variance,
    int MixedVoucherCount, string AllocationStatus);
internal sealed record CashflowPostingEvidence(int SourceRowId, string VoucherId, string Date,
    string AccountCode, string Debit, string Credit);
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
    [property: JsonPropertyName("treasury_postings")] IReadOnlyList<CashflowPostingEvidence> TreasuryPostings);
