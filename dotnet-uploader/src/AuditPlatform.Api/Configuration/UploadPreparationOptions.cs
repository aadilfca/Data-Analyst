using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class UploadPreparationOptions : IValidatableObject
{
    public const string SectionName="UploadPreparation";
    public bool Enabled {get;set;}
    public int MaximumConcurrentRequests {get;set;}=2;
    public int TimeoutSeconds {get;set;}=60;
    public int MaximumSelectionRows {get;set;}=10000;
    public int MaximumResponseBytes {get;set;}=1048576;
    public int MaximumFullPreviewBytes {get;set;}=64*1024*1024;
    public IEnumerable<ValidationResult> Validate(ValidationContext context)
    {
        if(MaximumConcurrentRequests is <1 or >8 || TimeoutSeconds is <1 or >300
            || MaximumSelectionRows is <1 or >100000 || MaximumResponseBytes is <4096 or >4194304
            || MaximumFullPreviewBytes is <1048576 or >67108864)
            yield return new("Invalid upload preparation limits.");
    }
}
