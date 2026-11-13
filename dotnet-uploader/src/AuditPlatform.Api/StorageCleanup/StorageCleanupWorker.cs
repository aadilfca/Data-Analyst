using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.StorageCleanup;

public sealed class StorageCleanupWorker(StorageArtifactCleanupService cleanup,IOptions<StorageCleanupOptions> options,
    ILogger<StorageCleanupWorker> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if(!options.Value.Enabled||!options.Value.PurgeEnabled||!options.Value.ArtifactWorkerEnabled) return;
        while(!stoppingToken.IsCancellationRequested)
        {
            try
            {
                for(int i=0;i<8&&!stoppingToken.IsCancellationRequested;i++) if(!await cleanup.ProcessOnceAsync(stoppingToken)) break;
                await Task.Delay(TimeSpan.FromSeconds(5),stoppingToken);
            }
            catch(OperationCanceledException) when(stoppingToken.IsCancellationRequested) {break;}
            catch(Exception)
            {
                // No source values, paths, storage URLs or credential-bearing errors.
                logger.LogWarning("Pending cleanup artifacts need another bounded recovery pass.");
                try {await Task.Delay(TimeSpan.FromSeconds(5),stoppingToken);} catch(OperationCanceledException) {break;}
            }
        }
    }
}
