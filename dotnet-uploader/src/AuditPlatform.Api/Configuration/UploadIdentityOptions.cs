using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class UploadIdentityOptions : IValidatableObject
{
    public const string SectionName="UploadIdentity";
    public bool Enabled {get;set;}
    public int MaximumConcurrentRequests {get;set;}=2;
    public int TimeoutSeconds {get;set;}=60;
    public int MaximumRows {get;set;}=100000;
    public int MaximumComparisonBytes {get;set;}=67108864;
    public int MaximumIssueDetails {get;set;}=100;
    public int MaximumResponseBytes {get;set;}=1048576;
    public IEnumerable<ValidationResult> Validate(ValidationContext context)
    {
        if(MaximumConcurrentRequests is <1 or >8||TimeoutSeconds is <1 or >300||MaximumRows is <1 or >1048576
            ||MaximumComparisonBytes is <4096 or >268435456||MaximumIssueDetails is <1 or >1000||MaximumResponseBytes is <4096 or >4194304)
            yield return new("Identity processing limits are outside supported bounds.");
    }
}
