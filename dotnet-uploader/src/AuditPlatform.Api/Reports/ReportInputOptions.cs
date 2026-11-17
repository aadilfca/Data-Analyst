using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Reports;

public sealed class ReportInputOptions
{
    public const string SectionName = "ReportInputs";
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 30;
    [Range(1, 100000)] public int MaximumRows { get; set; } = 100000;
    [Range(1048576, 33554432)] public int MaximumBytes { get; set; } = 32 * 1024 * 1024;

    public bool Valid() => TimeoutSeconds is >= 1 and <= 60
        && MaximumRows is >= 1 and <= 100000
        && MaximumBytes is >= 1048576 and <= 33554432;
}
