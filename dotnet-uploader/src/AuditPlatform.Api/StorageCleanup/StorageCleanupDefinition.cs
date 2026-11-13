using System.Text.Json;

namespace AuditPlatform.Api.StorageCleanup;

public static class CleanupJson
{
    public static readonly JsonSerializerOptions Options = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
}
public sealed record CleanupRequest(
    [property:System.Text.Json.Serialization.JsonPropertyName("operation_id")] Guid OperationId,
    [property:System.Text.Json.Serialization.JsonPropertyName("preview_token")] string PreviewToken,
    [property:System.Text.Json.Serialization.JsonPropertyName("confirmation")] string Confirmation,
    [property:System.Text.Json.Serialization.JsonPropertyName("backup_token")] string? BackupToken = null);
public sealed record CleanupResult(string Status, object? Value = null, string? Code = null);
public sealed record CleanupPreview(int ClientId, int TemplateId, string Kind, DateTimeOffset GeneratedAt,
    IReadOnlyDictionary<string, long> Counts, IReadOnlyDictionary<string, long> Protected, string Fingerprint,
    string PreviewToken, bool CanApply, IReadOnlyList<string> Blockers, int ArtifactCount);
public sealed record CleanupOperation(Guid OperationId, int ClientId, int TemplateId, string Kind, DateTimeOffset CompletedAt,
    IReadOnlyDictionary<string, long> DeletedCounts, string ArtifactStatus, int PendingArtifacts, int ReviewArtifacts);
internal sealed record CleanupArtifact(string Kind, Guid Key, string LocationHash, long Version = 0);
internal sealed record CleanupCapture(int Client, int Template, string Kind, string Fingerprint,
    Dictionary<string, long> Counts, Dictionary<string, long> Protected, List<string> Blockers, List<CleanupArtifact> Artifacts,
    string[] Tables, int[] Uploads, int[] AuditIds);
internal sealed record CleanupProof(int Version, string Purpose, int Tenant, int User, long Context, int Client, int Template,
    string Kind, string Fingerprint, DateTimeOffset Expires);
internal sealed class CleanupRejected(string status, string code) : Exception(code)
{
    public string Status { get; } = status;
    public string Code { get; } = code;
}
