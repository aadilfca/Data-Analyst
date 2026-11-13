using System.Globalization;
using System.Text.Json;

namespace AuditPlatform.Api.DataIntegrity;

public static class IntegrityJson
{
    public static readonly JsonSerializerOptions Options = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
}

public sealed record DataIntegrityResult(string Status, IntegrityOverview? Value = null, string? Code = null);
public sealed record IntegrityOverview(int ClientId, string ClientName, DateTimeOffset ScannedAt, string OverallStatus,
    IReadOnlyList<IntegrityModule> Modules, IntegritySafeguards Safeguards, IntegrityDatabaseContext? DatabaseContext,
    string SizeNote, string ScopeNote);
public sealed record IntegrityDatabaseContext(long DatabaseBytes, string DatabaseSize, long SharedIndexBytes, string SharedIndexSize);
public sealed record IntegrityModule(string Module, long? RowCount, long? AssociatedRecordCount, long? DataBytes,
    long? AssociatedDataBytes, long? DuplicateGroups, long? DuplicateExtraRows, long? UnidentifiedRows,
    long? UnmeasuredSourceFiles, string DuplicateBasis, long? InvalidUploadLinks = 0)
{
    public long? TotalDataBytes => DataBytes + AssociatedDataBytes;
    public string DataSize => ByteLabel(DataBytes);
    public string AssociatedDataSize => ByteLabel(AssociatedDataBytes);
    public string TotalDataSize => ByteLabel(TotalDataBytes);
    public string SizeStatus => TotalDataBytes is null || UnmeasuredSourceFiles is null or > 0 || InvalidUploadLinks is null or > 0 ? "Incomplete" : "Estimated";
    public string DuplicateStatus => DuplicateGroups > 0 ? "Review" : DuplicateGroups is null || UnidentifiedRows is null or > 0 ? "Not fully checked" : "Clear";
    public static string ByteLabel(long? bytes)
    {
        if (bytes is null) return "Unavailable";
        double size = Math.Max(0, bytes.Value);
        foreach (var unit in new[] { "B", "KB", "MB", "GB", "TB" })
        {
            if (size < 1024 || unit == "TB") return size.ToString(unit == "B" ? "0" : "0.0", CultureInfo.InvariantCulture) + " " + unit;
            size /= 1024;
        }
        return "Unavailable";
    }
}
public sealed record IntegrityCheck(string Label, string Status, string Detail);
public sealed record IntegritySafeguardModule(string Module, long? RowCount, string Status, IReadOnlyList<IntegrityCheck> Checks, bool Incomplete);
public sealed record IntegritySafeguards(string Status, bool Incomplete, IReadOnlyList<IntegritySafeguardModule> Modules,
    string ScopeNote, string LimitNote);
public sealed record IntegrityIndexRequirement(string Module, string Label, string Table, string[] Columns, string Kind, string? Predicate = null);
public sealed record IntegrityIndex(string TableName, string IndexName, string AccessMethod, bool IsValid, bool IsReady,
    bool IsLive, bool IsUnique, bool IsPrimary, bool IsImmediate, bool StandardKeys, string? Predicate, string?[] KeyColumns);

// Fixed public labels/requirements are independent of deployed index names.
public static class IntegrityBaseline
{
    public const string ActivePredicate = "is_deleted = FALSE AND change_detection_status <> 'deleted' AND source_identity IS NOT NULL AND source_identity <> ''";
    public static readonly string[] Modules = ["Client Data", "Trial Balance", "Reconciliation"];
    public static readonly IntegrityIndexRequirement[] Requirements = [
        new("Client Data", "Row identification", "client_data", ["id"], "primary"),
        new("Client Data", "Duplicate prevention", "client_data", ["client_id", "template_id", "source_identity"], "unique", ActivePredicate),
        new("Client Data", "Template identity lookup", "client_data", ["client_id", "template_id", "is_deleted", "source_identity"], "lookup"),
        new("Client Data", "Stored-date lookup", "client_data", ["client_id", "is_deleted", "extract_date"], "lookup"),
        new("Client Data", "Report-date lookup", "client_data", ["client_id", "is_deleted", "derived_transaction_date"], "lookup"),
        new("Client Data", "Cash/Bank classification lookup", "client_data", ["client_id", "is_deleted", "derived_treasury_type", "derived_transaction_date"], "lookup"),
        new("Trial Balance", "Upload identification", "trial_balance_uploads", ["id"], "primary"),
        new("Trial Balance", "Client upload lookup", "trial_balance_uploads", ["client_id"], "lookup"),
        new("Trial Balance", "Account identification", "trial_balance_accounts", ["id"], "primary"),
        new("Trial Balance", "Duplicate prevention", "trial_balance_accounts", ["upload_id", "account_code"], "unique"),
        new("Trial Balance", "Account hierarchy lookup", "trial_balance_accounts", ["upload_id", "parent_account_code"], "lookup"),
        new("Reconciliation", "Row identification", "report_balance_inputs", ["id"], "primary"),
        new("Reconciliation", "Duplicate prevention", "report_balance_inputs", ["client_id", "group_label", "account_code", "period_month"], "unique"),
        new("Reconciliation", "Period lookup", "report_balance_inputs", ["client_id", "group_label", "period_month"], "lookup")
    ];
    public static string Status(IEnumerable<string> statuses)
    {
        var values = statuses.ToArray();
        return values.Contains("Needs attention") ? "Needs attention" : values.Length == 0 || values.Contains("Not checked") ? "Not checked" : "Passed";
    }
}
