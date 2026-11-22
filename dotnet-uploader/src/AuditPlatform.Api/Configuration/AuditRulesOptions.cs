using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class AuditRulesOptions
{
    public const string SectionName = "AuditRules";
    public bool Enabled { get; set; }
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 30;
    [Range(1, 200)] public int MaximumPageSize { get; set; } = 200;
    [Range(1024, 1048576)] public int MaximumDefinitionBytes { get; set; } = 262144;

    public bool Valid() => TimeoutSeconds is >= 1 and <= 60
        && MaximumPageSize is >= 1 and <= 200
        && MaximumDefinitionBytes is >= 1024 and <= 1048576;
}
