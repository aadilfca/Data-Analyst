using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class StorageCleanupOptions
{
    public const string SectionName = "StorageCleanup";
    public bool Enabled { get; set; }
    public bool PurgeEnabled { get; set; }
    public bool ArtifactWorkerEnabled { get; set; }
    public string SigningKey { get; set; } = "";
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 60;
    [Range(1, 100000)] public int MaximumRows { get; set; } = 100000;
    [Range(4096, 33554432)] public int MaximumInputBytes { get; set; } = 33554432;
    [Range(4096, 262144)] public int MaximumResponseBytes { get; set; } = 131072;
    [Range(1, 256)] public int MaximumArtifacts { get; set; } = 256;
    [Range(1, 1024)] public int MaximumOperationsPerScope { get; set; } = 1024;
    [Range(1, 10)] public int ProofMinutes { get; set; } = 5;
    public bool ValidKey()
    {
        if (!Enabled) return true;
        try { return Convert.FromBase64String(SigningKey).Length is >= 32 and <= 64; }
        catch (FormatException) { return false; }
    }
}
