using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.ConfigurationBackup;
using AuditPlatform.Api.Tenancy;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/backup")]
[RequestSizeLimit(8388608)]
public sealed class ConfigurationBackupController(ConfigurationBackupService service,IOptions<ConfigurationBackupOptions> options) : ControllerBase
{
    public sealed record RestoreRequest(ConfigurationBackupDocument? Backup,string? Revision,bool Confirmed);
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

    [HttpGet("export")]
    public async Task<IActionResult> Export(CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        try
        {
            var backup=await service.ExportAsync(Session,ct);
            return backup is null ? StatusCode(403,new {detail="Backup access denied"}) : Ok(backup);
        }
        catch(BackupReviewException review) { return Conflict(new {detail=review.Message}); }
    }

    [HttpPost("preview")]
    public async Task<IActionResult> Preview([FromBody] ConfigurationBackupDocument? backup,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid) return BadRequest(new {detail="Invalid backup."});
        var result=await service.PreviewAsync(Session,backup,ct);
        return result.Status=="success" ? Ok(new {revision=result.Revision,warning=result.Detail,
            columns=backup!.Columns.Select(c=>new {c.Id,c.Name}), templates=backup.Templates.Select(t=>new {t.Id,t.Name}),
            mappings=backup.Mappings.Select(m=>new {m.ClientId,m.TemplateId,entry_count=m.Entries.Count}),
            holidays=backup.Holidays.Select(h=>new {h.Id,h.Label}),settings=backup.Settings.Keys.Order()}) : Error(result);
    }

    [HttpPost("import")]
    public async Task<IActionResult> Restore([FromBody] RestoreRequest? input,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid || input is null) return BadRequest(new {detail="Invalid restore request."});
        var result=await service.RestoreAsync(Session,input.Backup,input.Revision,input.Confirmed,ct);
        return result.Status=="success" ? Ok(new {message=result.Detail}) : Error(result);
    }

    private IActionResult? Guard()
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {detail="Configuration backup is unavailable"});
        if(Session is null) return Unauthorized(new {detail="Not authenticated"});
        if(Session.TenantContextRequired && (Request.Headers["X-App-Context"].Count!=1
            || Request.Headers["X-App-Context"].ToString()!=TenantSessionService.ContextKey(Session)))
        {
            Response.Headers["X-Context-Changed"]="true";
            return Conflict(new {detail="Workspace context changed. Select your workspace again."});
        }
        return null;
    }
    private IActionResult Error(BackupResult result) => StatusCode(result.Status switch {
        "forbidden"=>403,"review" or "conflict"=>409,_=>400},new {detail=result.Detail??"Backup access denied"});
}
