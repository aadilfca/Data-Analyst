using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class VerificationSetupOptions
{
    public const string SectionName = "VerificationSetup";
    public bool Enabled { get; set; }
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 30;
    [Range(1, 100000)] public int MaximumAccounts { get; set; } = 100000;
    [Range(1, 10000)] public int MaximumSelectors { get; set; } = 10000;

    public bool Valid() => TimeoutSeconds is >= 1 and <= 60
        && MaximumAccounts is >= 1 and <= 100000
        && MaximumSelectors is >= 1 and <= 10000;
}
