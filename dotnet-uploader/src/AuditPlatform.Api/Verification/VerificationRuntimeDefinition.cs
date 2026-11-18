using System.Text.Json;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Verification;

public sealed record VerificationRuntimeResult(string Status, JsonElement? Value = null, string? Code = null);

public sealed class VerificationSessionStartRequest
{
    [JsonPropertyName("client_id")] public int ClientId { get; set; }
    [JsonPropertyName("cashbook_name")] public string CashbookName { get; set; } = "";
    [JsonPropertyName("period_start")] public string PeriodStart { get; set; } = "";
    [JsonPropertyName("period_end")] public string PeriodEnd { get; set; } = "";
    [JsonPropertyName("created_by")] public string? CreatedBy { get; set; }
    [JsonPropertyName("session_type")] public string SessionType { get; set; } = "cash";
}

public sealed record VerificationPresenceRequest([property: JsonPropertyName("session_id")] int SessionId);

public class VerificationLineSaveRequest
{
    [JsonPropertyName("detail_id")] public int DetailId { get; set; }
    [JsonPropertyName("line_items")] public JsonElement LineItems { get; set; }
    [JsonPropertyName("transaction_type")] public string? TransactionType { get; set; }
    [JsonPropertyName("procedure_data")] public JsonElement? ProcedureData { get; set; }
}

public sealed class VerificationBundleLineRequest : VerificationLineSaveRequest
{
    [JsonPropertyName("transaction_ref")] public string? TransactionRef { get; set; }
}

public sealed class VerificationFormSaveRequest
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
    [JsonPropertyName("entries")] public List<VerificationFormEntryInput> Entries { get; set; } = [];
}
public sealed class VerificationFormEntryInput
{
    [JsonPropertyName("bill_group_index")] public int BillGroupIndex { get; set; }
    [JsonPropertyName("row_scope")] public string RowScope { get; set; } = "header";
    [JsonPropertyName("sort_order")] public int SortOrder { get; set; } = -1;
    [JsonPropertyName("values")] public List<VerificationFormValueInput> Values { get; set; } = [];
}
public sealed class VerificationFormValueInput
{
    [JsonPropertyName("field_id")] public int FieldId { get; set; }
    [JsonPropertyName("value")] public JsonElement Value { get; set; }
}
public sealed class VerificationPhysicalBillSaveRequest
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
    [JsonPropertyName("bills")] public List<VerificationPhysicalBillInput> Bills { get; set; } = [];
}
public sealed class VerificationPhysicalBillInput
{
    [JsonPropertyName("bill_no")] public string? BillNo { get; set; }
    [JsonPropertyName("bill_date")] public string? BillDate { get; set; }
    [JsonPropertyName("vendor_name")] public string? VendorName { get; set; }
    [JsonPropertyName("items")] public List<VerificationPhysicalBillItemInput> Items { get; set; } = [];
}
public sealed class VerificationPhysicalBillItemInput
{
    [JsonPropertyName("item_name")] public string? ItemName { get; set; }
    [JsonPropertyName("quantity")] public JsonElement Quantity { get; set; }
    [JsonPropertyName("rate")] public JsonElement Rate { get; set; }
    [JsonPropertyName("source_document_row_id")] public int? SourceDocumentRowId { get; set; }
    [JsonPropertyName("source_template")] public string? SourceTemplate { get; set; }
}
public sealed record VerificationSourcePreviewRequest([property: JsonPropertyName("source_type")] string SourceType);

public sealed class VerificationVoucherCheckInput
{
    [JsonPropertyName("check_id")] public int CheckId { get; set; }
    [JsonPropertyName("state")] public string State { get; set; } = "untouched";
}

public sealed class VerificationVoucherCheckSaveRequest
{
    [JsonPropertyName("client_id")] public int ClientId { get; set; }
    [JsonPropertyName("transaction_ref")] public string TransactionRef { get; set; } = "";
    [JsonPropertyName("check_id")] public int CheckId { get; set; }
    [JsonPropertyName("state")] public string State { get; set; } = "untouched";
}

public sealed class VerificationVoucherChecksBatchRequest
{
    [JsonPropertyName("client_id")] public int ClientId { get; set; }
    [JsonPropertyName("transaction_ref")] public string TransactionRef { get; set; } = "";
    [JsonPropertyName("checks")] public List<VerificationVoucherCheckInput> Checks { get; set; } = [];
}

public sealed class VerificationProcedureChecksBatchRequest
{
    [JsonPropertyName("checks")] public List<VerificationProcedureCheckInput> Checks { get; set; } = [];
}

public sealed class VerificationProcedureCheckInput
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
    [JsonPropertyName("role_name")] public string RoleName { get; set; } = "";
    [JsonPropertyName("is_checked")] public bool IsChecked { get; set; }
}

public sealed class VerificationObservationInput
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
    [JsonPropertyName("observation_text")] public string? ObservationText { get; set; }
}

public sealed class VerificationAccountHeadNoteInput
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("observation_text")] public string? ObservationText { get; set; }
}

public sealed class VerificationLineCorrectnessInput
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("is_correct")] public bool IsCorrect { get; set; }
}

public sealed class VerificationDocumentAssignRequest
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("client_data_id")] public int ClientDataId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
}

public sealed record VerificationUnassignDocumentRequest([property: JsonPropertyName("assignment_id")] int AssignmentId);
public sealed class VerificationSignatureExceptionRequest
{
    [JsonPropertyName("assignment_id")] public int AssignmentId { get; set; }
    [JsonPropertyName("signature_exception")] public bool SignatureException { get; set; }
}

public sealed class VerificationProcedureCheckRequest
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
    [JsonPropertyName("role_name")] public string RoleName { get; set; } = "";
    [JsonPropertyName("is_checked")] public bool IsChecked { get; set; }
}

public sealed class VerificationObservationSaveRequest
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
    [JsonPropertyName("observation_text")] public string? ObservationText { get; set; }
}

public sealed class VerificationBundleSaveRequest
{
    [JsonPropertyName("session_id")] public int SessionId { get; set; }
    [JsonPropertyName("current_detail_id")] public int CurrentDetailId { get; set; }
    [JsonPropertyName("lines")] public List<VerificationBundleLineRequest> Lines { get; set; } = [];
    [JsonPropertyName("verify")] public bool Verify { get; set; } = true;
    [JsonPropertyName("procedure_checks")] public List<VerificationProcedureCheckInput> ProcedureChecks { get; set; } = [];
    [JsonPropertyName("voucher_checks")] public List<VerificationVoucherCheckInput> VoucherChecks { get; set; } = [];
    [JsonPropertyName("observations")] public List<VerificationObservationInput> Observations { get; set; } = [];
    [JsonPropertyName("account_head_notes")] public List<VerificationAccountHeadNoteInput> AccountHeadNotes { get; set; } = [];
    [JsonPropertyName("voucher_line_correctness")] public List<VerificationLineCorrectnessInput> VoucherLineCorrectness { get; set; } = [];
    [JsonPropertyName("form_entries")] public List<VerificationFormSaveRequest> FormEntries { get; set; } = [];
    [JsonPropertyName("physical_bills")] public List<VerificationPhysicalBillSaveRequest> PhysicalBills { get; set; } = [];
}

public sealed record VerificationSaveAndVerifyRequest(
    [property: JsonPropertyName("session_id")] int SessionId,
    [property: JsonPropertyName("current_detail_id")] int CurrentDetailId,
    [property: JsonPropertyName("detail_id")] int DetailId,
    [property: JsonPropertyName("line_items")] JsonElement LineItems,
    [property: JsonPropertyName("transaction_type")] string? TransactionType = null,
    [property: JsonPropertyName("procedure_data")] JsonElement? ProcedureData = null);

public sealed record VerificationValidateRequest([property: JsonPropertyName("detail_id")] int DetailId,
    [property: JsonPropertyName("inventory_flag")] bool InventoryFlag = false);
public sealed record VerificationCompleteRequest([property: JsonPropertyName("detail_id")] int DetailId,
    [property: JsonPropertyName("verified_by")] string? VerifiedBy = null);
public sealed record VerificationVerifyVoucherRequest([property: JsonPropertyName("session_id")] int SessionId,
    [property: JsonPropertyName("current_detail_id")] int CurrentDetailId);

internal sealed record VerificationReviewContext(int TenantId, int UserId, string Username,
    string TenantRole, int ClientId, int? SessionRulebookVersion, int CurrentRulebookVersion, JsonElement Rulebook);

internal sealed record VerificationLineStatusResult(string ClearedStatus, int? MatrixVersion,
    int? CategoryId, string? CategoryName, string AppliedRulesetHash);

internal sealed record VerificationAuditWorkbookLineInput(int DetailId, int ClientDataId,
    string? TransactionRef, JsonElement RowData, string? ExtractAmount, int? AppliedCategoryId,
    string? AppliedCategoryName);

internal sealed record VerificationAuditWorkbookProcedureProgress(int Done, int Total,
    int? CategoryId, string? CategoryName);

internal sealed record AuditedCashbookExportRow(IReadOnlyList<string?> Cells, decimal? Amount);
