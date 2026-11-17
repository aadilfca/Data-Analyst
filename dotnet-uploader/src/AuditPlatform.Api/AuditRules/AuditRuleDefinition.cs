using System.Text.Json;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.AuditRules;

public static class AuditRuleKinds
{
    public const int SnapshotSchemaVersion = 2;
    public const int EvaluatorVersion = 1;
    public static readonly string[] All = ["documents", "cross-checks", "categories", "assertions", "voucher-checks"];
    public static readonly string[] ComparisonTypes = ["All equal", "Sequential increasing", "Sequential decreasing",
        "Within tolerance %", "Must be equal", "May be less, never more"];
    public static readonly string[] FormFieldTypes = ["text", "date", "numeric", "arithmetic_multiply"];
    public static readonly string[] FormFieldScopes = ["header", "item"];
    public static bool Valid(string? value) => value is not null && All.Contains(value, StringComparer.Ordinal);
    public static string Singular(string value) => value switch
    {
        "documents" => "Document", "cross-checks" => "Cross-check", "categories" => "Category",
        "assertions" => "Assertion", "voucher-checks" => "VoucherCheck", _ => "AuditRule"
    };
}

public sealed record AuditRuleResult(string Status, JsonElement? Value = null, string? Code = null,
    int? EntityId = null, long? Version = null, int? Total = null);

public sealed record AuditRuleEntity(int Id, string Kind, JsonElement Definition, bool IsActive,
    long Revision, DateTime UpdatedAt);

public sealed record AuditRuleSnapshot(int TenantId, long ConfigVersion, int SchemaVersion,
    int EvaluatorVersion, string Fingerprint, JsonElement Payload, DateTime PublishedAt);

public sealed record SignatureSpecimenInput(
    [property: JsonPropertyName("client_id")] int ClientId,
    [property: JsonPropertyName("document_id")] int DocumentId,
    [property: JsonPropertyName("role_name")] string RoleName,
    [property: JsonPropertyName("expected_signatory_name")] string ExpectedSignatoryName,
    [property: JsonPropertyName("authority")] string? Authority = null);
