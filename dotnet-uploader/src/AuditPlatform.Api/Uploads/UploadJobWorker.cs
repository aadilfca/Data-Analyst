using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
using Npgsql;
using AuditPlatform.Api.Data;
using System.Text.Json;

namespace AuditPlatform.Api.Uploads;

public sealed class UploadJobWorker(UploadJobService jobs,IOptions<UploadJobOptions> options,ILogger<UploadJobWorker> logger):BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if(!options.Value.Enabled) return;
        // Independent loops share durable database limits, including across hosts.
        var loops=Enumerable.Range(0,options.Value.MaximumWorkers).Select(_=>Loop(stoppingToken)).ToList();
        loops.Add(CleanupLoop(stoppingToken));await Task.WhenAll(loops);
    }
    private async Task Loop(CancellationToken ct)
    {
        while(!ct.IsCancellationRequested)
        {
            try
            {
                bool worked=await jobs.RunOnceAsync(ct);
                if(!worked) await Task.Delay(TimeSpan.FromSeconds(options.Value.PollSeconds),ct);
            }
            catch(OperationCanceledException) when(ct.IsCancellationRequested) {return;}
            catch(Exception e) when(e is NpgsqlException or HttpRequestException or IOException or PostgresCommitOutcomeUnknownException or TimeoutException or UploadJobRejected or JsonException)
            {
                // Do not log provider responses, source paths, tokens or input.
                logger.LogWarning("Upload job worker temporarily unavailable");
                await Task.Delay(TimeSpan.FromSeconds(options.Value.PollSeconds),ct);
            }
        }
    }
    private async Task CleanupLoop(CancellationToken ct)
    {
        // Retention progresses even while the execution queue remains busy.
        while(!ct.IsCancellationRequested)
        {
            try {await jobs.CleanupAsync(ct);}
            catch(OperationCanceledException) when(ct.IsCancellationRequested) {return;}
            catch(Exception e) when(e is NpgsqlException or HttpRequestException or IOException or PostgresCommitOutcomeUnknownException or TimeoutException or UploadJobRejected or JsonException)
            {logger.LogWarning("Upload job source cleanup temporarily unavailable");}
            await Task.Delay(TimeSpan.FromSeconds(options.Value.PollSeconds),ct);
        }
    }
}
