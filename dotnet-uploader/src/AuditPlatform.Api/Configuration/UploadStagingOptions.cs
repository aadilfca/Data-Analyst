using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class UploadStagingOptions : IValidatableObject
{
    public const string SectionName="UploadStaging";
    public bool Enabled { get; set; }
    public string RootDirectory { get; set; }="";
    public long MaximumFileBytes { get; set; }=64L*1024*1024;
    public long MaximumExpandedBytes { get; set; }=256L*1024*1024;
    public int MaximumSheets { get; set; }=100;
    public int MaximumColumns { get; set; }=1024;
    public int MaximumRows { get; set; }=1048576;
    public int MaximumCellCharacters { get; set; }=65536;
    public int LifetimeMinutes { get; set; }=120;
    public int MaximumConcurrentReceipts { get; set; }=2;
    public int ReceiptTimeoutSeconds { get; set; }=120;
    public IEnumerable<ValidationResult> Validate(ValidationContext context)
    {
        if(!Enabled) yield break;
        if(!Path.IsPathFullyQualified(RootDirectory)) yield return new("Staging requires an absolute private local directory.");
        if(MaximumFileBytes is <1 or >268435456 || MaximumExpandedBytes<MaximumFileBytes || MaximumExpandedBytes>1073741824)
            yield return new("Invalid staging byte limits.");
        if(MaximumSheets is <1 or >500 || MaximumColumns is <1 or >16384 || LifetimeMinutes is <1 or >1440
            || MaximumRows is <1 or >1048576 || MaximumCellCharacters is <1 or >1048576
            || MaximumConcurrentReceipts is <1 or >8 || ReceiptTimeoutSeconds is <1 or >600) yield return new("Invalid staging limits.");
    }
}
