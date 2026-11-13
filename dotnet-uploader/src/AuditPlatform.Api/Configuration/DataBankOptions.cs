using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class DataBankOptions
{
    public const string SectionName = "DataBank";
    public bool Enabled { get; set; }
    [Range(1, 8)] public int MaximumConcurrentRequests { get; set; } = 2;
    [Range(1, 120)] public int TimeoutSeconds { get; set; } = 30;
    [Range(4096, 8388608)] public int MaximumResponseBytes { get; set; } = 1048576;
    [Range(1, 2000)] public int MaximumTemplates { get; set; } = 500;
    [Range(1, 1024)] public int MaximumColumns { get; set; } = 1024;
    [Range(1, 10000)] public int MaximumLedgerAccounts { get; set; } = 10000;
}
