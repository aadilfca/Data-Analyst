namespace AuditPlatform.Api.Configuration;

// Requires approved tenant mapping and shared-schema execution before live use.
public sealed class SettingsManagementOptions
{
    public const string SectionName = "SettingsManagement";
    public bool Enabled { get; set; }
}
