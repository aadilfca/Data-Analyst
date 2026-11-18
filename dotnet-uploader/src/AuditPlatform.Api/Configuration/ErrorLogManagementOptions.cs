using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class ErrorLogManagementOptions
{
    public const string SectionName = "ErrorLogManagement";
    public bool Enabled { get; set; }
    public bool RouteOwnershipEnabled { get; set; }
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 15;
    [Range(1, 8)] public int MaximumConcurrentRequests { get; set; } = 2;
    [Range(16384, 4194304)] public int MaximumResponseBytes { get; set; } = 2097152;
}
