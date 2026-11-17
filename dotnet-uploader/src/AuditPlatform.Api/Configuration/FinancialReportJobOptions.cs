using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class FinancialReportJobOptions
{
    public const string SectionName = "FinancialReportJobs";
    public bool Enabled { get; set; }
    [Range(1,4)] public int MaximumWorkers { get; set; }=1;
    [Range(1,32)] public int MaximumOutstandingPerOwner { get; set; }=8;
    [Range(1,3)] public int MaximumAttempts { get; set; }=3;
    [Range(30,300)] public int LeaseSeconds { get; set; }=120;
    [Range(1,30)] public int PollSeconds { get; set; }=2;
    [Range(1,72)] public int RetentionHours { get; set; }=24;
}
