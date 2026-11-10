using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

/// <summary>Non-secret settings for the service foundation.</summary>
public sealed class PlatformOptions
{
    public const string SectionName = "Platform";

    [Required]
    [StringLength(100)]
    public string ApplicationName { get; init; } = string.Empty;

    // Decimal bytes match the accepted 500 MB policy. P04/P24-P29 will
    // implement measured capacity enforcement; this setting performs no I/O.
    [Range(typeof(long), "1", "500000000")]
    public long DatabaseCapacityLimitBytes { get; init; } = 500_000_000;
}
