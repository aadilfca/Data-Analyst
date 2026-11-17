using System.Text.Json;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Verification;

public sealed record VerificationSetupResult(string Status, JsonElement? Value = null, string? Code = null);

public sealed class WorkstreamAssignmentRequest
{
    [JsonPropertyName("client_id")] public int ClientId { get; init; }
    [JsonPropertyName("trial_balance_upload_id")] public int? TrialBalanceUploadId { get; init; }
    [JsonPropertyName("account_code")] public string? AccountCode { get; init; }
    [JsonRequired, JsonPropertyName("verification_workstream")] public string? VerificationWorkstream { get; init; }
}

public sealed class WorkstreamBulkAssignmentRequest
{
    [JsonPropertyName("client_id")] public int ClientId { get; init; }
    [JsonPropertyName("trial_balance_upload_id")] public int? TrialBalanceUploadId { get; init; }
    [JsonPropertyName("parent_account_code")] public string? ParentAccountCode { get; init; }
    [JsonRequired, JsonPropertyName("verification_workstream")] public string? VerificationWorkstream { get; init; }
}

public sealed record AccountSetSelectorRequest(
    [property: JsonPropertyName("selector_type")] string? SelectorType,
    [property: JsonPropertyName("account_code")] string? AccountCode,
    [property: JsonPropertyName("sort_order")] int? SortOrder = null);

public sealed class AccountSetCreateRequest
{
    [JsonPropertyName("client_id")] public int ClientId { get; init; }
    [JsonPropertyName("set_key")] public string? SetKey { get; init; }
    [JsonPropertyName("display_name")] public string? DisplayName { get; init; }
    [JsonPropertyName("description")] public string? Description { get; init; }
    [JsonPropertyName("selectors")] public IReadOnlyList<AccountSetSelectorRequest>? Selectors { get; init; }
}

public sealed class AccountSetDraftRequest
{
    [JsonPropertyName("source_upload_id")] public int? SourceUploadId { get; init; }
    [JsonPropertyName("display_name")] public string? DisplayName { get; init; }
    [JsonPropertyName("description")] public string? Description { get; init; }
    [JsonPropertyName("selectors")] public IReadOnlyList<AccountSetSelectorRequest>? Selectors { get; init; }
}
