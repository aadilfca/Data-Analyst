using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public class DashboardOptions
{
    public const string SectionName = "Dashboard";
    public bool Enabled { get; set; }
    [Range(1, 60)] public int TimeoutSeconds { get; set; } = 30;
    [Range(1, 8)] public int MaximumConcurrentRequests { get; set; } = 2;
    [Range(4096, 2097152)] public int MaximumResponseBytes { get; set; } = 262144;
    [Range(1, 10000)] public int MaximumOverdueRows { get; set; } = 1000;
}
