using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class DataBankExportOptions
{
    public const string SectionName = "DataBankExports";
    public bool Enabled { get; set; }
    [Range(1, 2)] public int MaximumConcurrentRequests { get; set; } = 1;
    [Range(1, 120)] public int TimeoutSeconds { get; set; } = 60;
    [Range(1, 100000)] public int MaximumRows { get; set; } = 100000;
    [Range(4096, 67108864)] public int MaximumInputBytes { get; set; } = 33554432;
    [Range(4096, 16777216)] public int MaximumWorkbookBytes { get; set; } = 8388608;
    [Range(4096, 134217728)] public int MaximumExpandedBytes { get; set; } = 67108864;
}
