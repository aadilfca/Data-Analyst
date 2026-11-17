using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class ReconciliationSetupOptions
{
    public const string SectionName = "ReconciliationSetup";
    public bool Enabled { get; set; }
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 30;
    [Range(1, 100000)] public int MaximumAccounts { get; set; } = 100000;
    [Range(1, 100000)] public int MaximumBalanceRows { get; set; } = 100000;
    [Range(1, 5000)] public int MaximumCategories { get; set; } = 5000;
    [Range(1, 100000)] public int MaximumAssignments { get; set; } = 100000;

    public bool Valid() => TimeoutSeconds is >= 1 and <= 60
        && MaximumAccounts is >= 1 and <= 100000
        && MaximumBalanceRows is >= 1 and <= 100000
        && MaximumCategories is >= 1 and <= 5000
        && MaximumAssignments is >= 1 and <= 100000;
}
