namespace AuditPlatform.Api.Configuration;

public sealed class ConfigurationBackupOptions
{
    public const string SectionName = "ConfigurationBackup";
    public bool Enabled { get; set; }
}
