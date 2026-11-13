namespace AuditPlatform.Api.Configuration;

// UI activation is separate from API rehearsals; it grants no upload permission.
public sealed class UploadUiOptions
{
    public const string SectionName = "UploadUi";
    public bool Enabled { get; set; }
}
