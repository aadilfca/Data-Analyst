namespace AuditPlatform.Api.Configuration;

public sealed class TemplateManagementOptions
{
    public const string SectionName = "TemplateManagement";
    public bool Enabled { get; set; }
    public bool WriteEnabled { get; set; }
}
