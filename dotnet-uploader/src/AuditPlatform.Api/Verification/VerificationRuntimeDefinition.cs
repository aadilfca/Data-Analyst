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

public sealed class VerificationVoucherCheckInput
{
    [JsonPropertyName("check_id")] public int CheckId { get; set; }
    [JsonPropertyName("state")] public string State { get; set; } = "untouched";
}

public sealed class VerificationProcedureCheckInput
{
    [JsonPropertyName("voucher_line_id")] public int VoucherLineId { get; set; }
    [JsonPropertyName("document_id")] public int DocumentId { get; set; }
    [JsonPropertyName("role_name")] public string RoleName { get; set; } = "";
    [JsonPropertyName("is_checked")] public bool IsChecked { get; set; }
}

public sealed class VerificationBundleSaveRequest
{
    [JsonPropertyName("session_id")] public int SessionId { get; set; }
    [JsonPropertyName("current_detail_id")] public int CurrentDetailId { get; set; }
    [JsonPropertyName("lines")] public List<VerificationBundleLineRequest> Lines { get; set; } = [];
    [JsonPropertyName("verify")] public bool Verify { get; set; } = true;
    [JsonPropertyName("procedure_checks")] public List<VerificationProcedureCheckInput> ProcedureChecks { get; set; } = [];
    [JsonPropertyName("voucher_checks")] public List<VerificationVoucherCheckInput> VoucherChecks { get; set; } = [];
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
