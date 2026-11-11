namespace AuditPlatform.Api.Configuration;

// No asset hosting or selected-tenant schema is used unless explicitly enabled.
public sealed class ApplicationShellOptions
{
    public const string SectionName = "ApplicationShell";
    public bool Enabled { get; set; }
}
