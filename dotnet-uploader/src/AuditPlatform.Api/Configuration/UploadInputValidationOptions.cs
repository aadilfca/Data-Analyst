using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class UploadInputValidationOptions : IValidatableObject
{
    public const string SectionName="UploadInputValidation";
    public bool Enabled {get;set;}
    public int MaximumConcurrentRequests {get;set;}=2;
    public int TimeoutSeconds {get;set;}=60;
    public int MaximumIssueRows {get;set;}=100;
    public int MaximumResponseBytes {get;set;}=1048576;
    public IEnumerable<ValidationResult> Validate(ValidationContext context)
    {
        if(MaximumConcurrentRequests is <1 or >8 || TimeoutSeconds is <1 or >300
            || MaximumIssueRows is <1 or >1000 || MaximumResponseBytes is <4096 or >4194304)
            yield return new("Invalid input-validation limits.");
    }
}
