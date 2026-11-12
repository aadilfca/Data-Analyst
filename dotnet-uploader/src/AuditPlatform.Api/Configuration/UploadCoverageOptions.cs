using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class UploadCoverageOptions : IValidatableObject
{
    public const string SectionName="UploadCoverage";
    public bool Enabled {get;set;}
    public int MaximumConcurrentRequests {get;set;}=2;
    public int TimeoutSeconds {get;set;}=60;
    public int MaximumIssueDetails {get;set;}=100;
    public int MaximumResponseBytes {get;set;}=1048576;
    public IEnumerable<ValidationResult> Validate(ValidationContext context)
    {
        if(MaximumConcurrentRequests is <1 or >8 || TimeoutSeconds is <1 or >300
            || MaximumIssueDetails is <1 or >1000 || MaximumResponseBytes is <4096 or >4194304)
            yield return new("Coverage processing limits are outside the supported bounds.");
    }
}
