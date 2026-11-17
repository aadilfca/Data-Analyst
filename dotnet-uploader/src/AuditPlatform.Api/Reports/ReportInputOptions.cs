using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Reports;

public sealed class ReportInputOptions
{
    public const string SectionName = "ReportInputs";
    // Reporting remains unavailable until local source parity and acceptance are complete.
    public bool Enabled { get; set; }
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 30;
    [Range(1, 100000)] public int MaximumRows { get; set; } = 100000;
    [Range(1048576, 33554432)] public int MaximumBytes { get; set; } = 32 * 1024 * 1024;
    [Range(1048576, 67108864)] public int MaximumWorkbookBytes { get; set; } = 16777216;
    [Range(1, 100000)] public int MaximumWorkbookRows { get; set; } = 50000;

    public bool Valid() => TimeoutSeconds is >= 1 and <= 60
        && MaximumRows is >= 1 and <= 100000
        && MaximumBytes is >= 1048576 and <= 33554432
        && MaximumWorkbookBytes is >= 1048576 and <= 67108864
        && MaximumWorkbookRows is >= 1 and <= 100000;
}
