using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class LocalDatabaseBackupOptions
{
    public const string SectionName = "LocalDatabaseBackup";
    public bool Enabled { get; set; }
    public string ArchiveDirectory { get; set; } = "";
    public string ToolsDirectory { get; set; } = "";
    public string ConnectionString { get; set; } = "";
    [Range(60, 1800)] public int TimeoutSeconds { get; set; } = 900;
    [Range(1, 8)] public int MaximumConcurrentRequests { get; set; } = 1;
    [Range(1073741824, long.MaxValue)] public long MinimumFreeBytes { get; set; } = 1073741824;
}
