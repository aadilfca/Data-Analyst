namespace AuditPlatform.Api.Configuration;

// Client creation and broader client administration have separate activation gates.
public sealed class ClientManagementOptions
{
    public const string SectionName = "ClientManagement";
    public bool Enabled { get; set; }
    public bool CreateEnabled { get; set; }
    public bool WriteEnabled { get; set; }
}
