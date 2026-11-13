using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class DataIntegrityOptions
{
    public const string SectionName = "DataIntegrity";
    public bool Enabled { get; set; }
    [Range(1, 1)] public int MaximumConcurrentRequests { get; set; } = 1;
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 60;
    [Range(14, 4096)] public int MaximumCatalogEntries { get; set; } = 1024;
    [Range(16384, 4194304)] public int MaximumCatalogBytes { get; set; } = 2097152;
    [Range(4096, 262144)] public int MaximumResponseBytes { get; set; } = 131072;
}
