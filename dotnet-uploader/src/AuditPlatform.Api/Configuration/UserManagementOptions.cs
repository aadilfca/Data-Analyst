namespace AuditPlatform.Api.Configuration;

// Disabled until the reviewed schema, identity mapping and routing gates pass.
public sealed class UserManagementOptions
{
    public const string SectionName = "UserManagement";
    public bool Enabled { get; set; }
}
