using System.Text.Json.Serialization;

namespace AuditPlatform.Api.LocalDatabaseBackup;

public sealed record LocalBackupResult(string Status, LocalBackupDocument? Backup = null,
    IReadOnlyList<string>? Requirements = null, string? Detail = null, bool RetrySame = false,
    string? Code = null);

public sealed record LocalBackupDocument(
    [property: JsonPropertyName("operation_id")] string OperationId,
    [property: JsonPropertyName("created_at")] DateTimeOffset CreatedAt,
    [property: JsonPropertyName("verified_at")] DateTimeOffset? VerifiedAt,
    string State,
    [property: JsonPropertyName("file_name")] string FileName,
    long? Bytes,
    [property: JsonPropertyName("sha256")] string? Sha256,
    string? Error,
    [property: JsonPropertyName("error_code")] string? ErrorCode,
    [property: JsonPropertyName("local_path")] string LocalPath,
    bool Running,
    LocalBackupProgress Progress);

public sealed record LocalBackupProgress(string Stage,
    [property: JsonPropertyName("completed_steps")] int CompletedSteps,
    string Label,
    [property: JsonPropertyName("local_bytes")] long LocalBytes,
    [property: JsonPropertyName("elapsed_seconds")] long ElapsedSeconds,
    string Note);
