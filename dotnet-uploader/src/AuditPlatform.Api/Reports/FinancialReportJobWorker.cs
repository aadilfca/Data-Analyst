using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Reports;

public sealed class FinancialReportJobWorker(FinancialReportJobService jobs,
    IOptions<FinancialReportJobOptions> options,ILogger<FinancialReportJobWorker> logger):BackgroundService
{
    protected override Task ExecuteAsync(CancellationToken stoppingToken)=>Task.WhenAll(
        Enumerable.Range(0,options.Value.MaximumWorkers).Select(_=>WorkAsync(stoppingToken)));

    private async Task WorkAsync(CancellationToken ct)
    {
        while(!ct.IsCancellationRequested)
        {
            if(!options.Value.Enabled){await Task.Delay(TimeSpan.FromSeconds(options.Value.PollSeconds),ct);continue;}
            try
            {
                bool worked=await jobs.RunOnceAsync(ct);
                await jobs.CleanupAsync(ct);
                if(!worked)await Task.Delay(TimeSpan.FromSeconds(options.Value.PollSeconds),ct);
            }
            catch(OperationCanceledException)when(ct.IsCancellationRequested){return;}
            catch(Exception error)when(error is NpgsqlException or IOException or TimeoutException
                or PostgresCommitOutcomeUnknownException or InvalidOperationException)
            {
                logger.LogWarning("Financial report worker temporarily unavailable");
                await Task.Delay(TimeSpan.FromSeconds(options.Value.PollSeconds),ct);
            }
        }
    }
}
