namespace AuditPlatform.Api.Configuration;

// Native column labels are not legacy JSON keys. Activation requires compatible
// template/import/rule/report consumers and reviewed legacy binding inventory.
public sealed class ColumnManagementOptions
{
    public const string SectionName = "ColumnManagement";
    public bool Enabled { get; set; }
}
