namespace AuditPlatform.Api.Configuration;

// Activation needs reviewed tenant mapping and cross-backend authorization.
public sealed class ClientManagementOptions
{
    public const string SectionName = "ClientManagement";
    public bool Enabled { get; set; }
    public bool WriteEnabled { get; set; }
}
