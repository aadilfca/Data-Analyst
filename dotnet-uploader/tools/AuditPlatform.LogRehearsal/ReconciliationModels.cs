using System.Text.Json;
using System.Text.Json.Serialization;

namespace AuditPlatform.LogRehearsal;

public sealed record ReconciliationManifest
{
    public int Version { get; init; } = 1;
    public string SourceKind { get; init; } = "";
    public Guid OriginId { get; init; }
    public Guid SnapshotId { get; init; }
    public Dictionary<string, string> NaiveTimeZones { get; init; } = [];
    public Dictionary<int, int> ClientIds { get; init; } = [];
    public Dictionary<int, CanonicalRowLink> CanonicalRows { get; init; } = [];
    public HashSet<string> ValueFields { get; init; } = [];
    public HashSet<string> PublicTextFields { get; init; } = [];
    public List<LegacyLogRecord> Records { get; init; } = [];
}

public sealed record CanonicalRowLink(int TargetId, int SourceClientId);
public sealed record LegacyLogRecord(string Kind, int SourceId, JsonElement Values, string PayloadMode = "compact");
public sealed record ReconciliationItem(string Kind, int SourceId, int? TargetId, string Status, string Reason, int Redactions = 0);
public sealed record ReconciliationProgress(int Completed, int Total);
public sealed record ReconciliationReport(Guid OriginId, Guid SnapshotId, string Status, IReadOnlyList<ReconciliationItem> Items, int Total)
{
    public int Considered => Items.Count;
    public int Remaining => Total - Considered;
    public int Imported => Items.Count(x => x.Status == "imported");
    public int Matched => Items.Count(x => x.Status is "matched" or "already_applied");
    public int Conflicts => Items.Count(x => x.Status is "conflict" or "quarantined");
    public int Redactions => Items.Sum(x => x.Redactions);
}

public static class ReconciliationJson
{
    public static JsonSerializerOptions Options { get; } = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = false,
        NumberHandling = JsonNumberHandling.Strict,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        MaxDepth = 16
    };

    public static ReconciliationManifest Parse(ReadOnlyMemory<byte> bytes)
    {
        if (bytes.Length > 5 * 1024 * 1024)
            throw new ArgumentException("Rehearsal input exceeds its size limit.");
        if (bytes.Span.StartsWith(new byte[] { 0xef, 0xbb, 0xbf })) bytes = bytes[3..];
        using var document = JsonDocument.Parse(bytes, new() { MaxDepth = 16 });
        UniqueKeys(document.RootElement);
        return document.RootElement.Deserialize<ReconciliationManifest>(Options)
            ?? throw new ArgumentException("Rehearsal input is missing.");
    }

    private static void UniqueKeys(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var keys = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in element.EnumerateObject())
            {
                if (!keys.Add(property.Name)) throw new ArgumentException("Duplicate input property.");
                UniqueKeys(property.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
            foreach (var child in element.EnumerateArray()) UniqueKeys(child);
    }
}
