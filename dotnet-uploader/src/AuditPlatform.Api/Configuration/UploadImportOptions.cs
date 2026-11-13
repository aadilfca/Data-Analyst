using System.ComponentModel.DataAnnotations;
namespace AuditPlatform.Api.Configuration;

public sealed class UploadImportOptions
{
    public const string SectionName="UploadImports";
    public bool Enabled {get;set;}
    [Range(1,8)] public int MaximumConcurrentRequests {get;set;}=2;
    [Range(1,300)] public int TimeoutSeconds {get;set;}=60;
    [Range(4096,524288)] public int MaximumReceiptBytes {get;set;}=131072;
    [Range(4096,16777216)] public int MaximumWorkbookBytes {get;set;}=8388608;
    [Range(1,100000)] public int MaximumWorkbookRows {get;set;}=10000;
    [Range(1,120)] public int EvidenceMinutes {get;set;}=120;
    [Range(1,128)] public int MaximumNoChangeReceiptsPerFile {get;set;}=128;
    [Range(1,1000)] public int BatchSize {get;set;}=250;
}
