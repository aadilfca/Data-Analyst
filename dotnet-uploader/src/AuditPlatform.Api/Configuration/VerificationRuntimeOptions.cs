using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class VerificationRuntimeOptions
{
    public const string SectionName = "VerificationRuntime";
    public bool Enabled { get; set; }
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 30;
    [Range(1, 100000)] public int MaximumSourceRows { get; set; } = 100000;
    [Range(1048576, 268435456)] public int MaximumSourceBytes { get; set; } = 67108864;

    public bool Valid() => TimeoutSeconds is >= 1 and <= 60
        && MaximumSourceRows is >= 1 and <= 100000
        && MaximumSourceBytes is >= 1048576 and <= 268435456;
}
