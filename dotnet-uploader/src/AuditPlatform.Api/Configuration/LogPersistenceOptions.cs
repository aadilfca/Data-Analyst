using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class LogPersistenceOptions
{
    public const string SectionName = "LogPersistence";

    [Range(1, 10)]
    public int DiagnosticTimeoutSeconds { get; set; } = 2;

    [Range(64, 5000)]
    public int MetadataMaxBytes { get; set; } = 5000;

    [Range(1, 16)]
    public int MetadataMaxDepth { get; set; } = 8;

    [Range(1, 1000)]
    public int AuditDetailsMaxCharacters { get; set; } = 1000;
}
