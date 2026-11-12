using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class UploadCorrectionOptions
{
    public const string SectionName="UploadCorrections";
    public bool Enabled {get;set;}
    [Range(1,8)] public int MaximumConcurrentRequests {get;set;}=2;
    [Range(1,300)] public int TimeoutSeconds {get;set;}=60;
    [Range(1,100000)] public int MaximumRows {get;set;}=100000;
    [Range(4096,67108864)] public long MaximumComparisonBytes {get;set;}=67108864;
    [Range(1,1000)] public int MaximumIssueDetails {get;set;}=100;
    [Range(4096,1048576)] public int MaximumResponseBytes {get;set;}=1048576;
}
