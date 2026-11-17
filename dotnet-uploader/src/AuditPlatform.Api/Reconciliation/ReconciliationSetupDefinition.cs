using System.Text.Json;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Reconciliation;

public sealed record ReconciliationSetupResult(string Status, JsonElement? Value = null, string? Code = null);

public sealed class AccountGroupUpdateRequest
{
    [JsonPropertyName("group_label")] public string? GroupLabel { get; init; }
    [JsonPropertyName("account_codes")] public IReadOnlyList<string?>? AccountCodes { get; init; } = [];
}

public sealed class ReconciliationBalanceInputRequest
{
    [JsonPropertyName("account_code")] public string? AccountCode { get; init; }
    [JsonPropertyName("period_month")] public string? PeriodMonth { get; init; }
    [JsonRequired, JsonPropertyName("opening_balance")] public double? OpeningBalance { get; init; }
    [JsonRequired, JsonPropertyName("actual_closing_balance")] public double? ActualClosingBalance { get; init; }
}

public sealed class ReconciliationBalanceInputsUpdateRequest
{
    [JsonPropertyName("group_label")] public string? GroupLabel { get; init; }
    [JsonPropertyName("balances")] public IReadOnlyList<ReconciliationBalanceInputRequest?>? Balances { get; init; } = [];
}

public sealed class ReportCategorySetCreateRequest
{
    [JsonPropertyName("purpose_key")] public string? PurposeKey { get; init; }
    [JsonPropertyName("set_name")] public string? SetName { get; init; }
}

public sealed class ReportCategoryDefinitionRequest
{
    [JsonPropertyName("category_key")] public string? CategoryKey { get; init; }
    [JsonPropertyName("label")] public string? Label { get; init; }
    [JsonPropertyName("parent_category_key")] public string? ParentCategoryKey { get; init; }
    [JsonPropertyName("sort_order")] public int SortOrder { get; init; }
}

public sealed class ReportCategoryDefinitionsUpdateRequest
{
    [JsonPropertyName("categories")] public IReadOnlyList<ReportCategoryDefinitionRequest?>? Categories { get; init; } = [];
}

public sealed class ReportAccountCategoryAssignmentRequest
{
    [JsonPropertyName("account_code")] public string? AccountCode { get; init; }
    [JsonPropertyName("category_key")] public string? CategoryKey { get; init; }
}

public sealed class ReportCategoryAssignmentsUpdateRequest
{
    [JsonPropertyName("assignments")] public IReadOnlyList<ReportAccountCategoryAssignmentRequest?>? Assignments { get; init; } = [];
}
