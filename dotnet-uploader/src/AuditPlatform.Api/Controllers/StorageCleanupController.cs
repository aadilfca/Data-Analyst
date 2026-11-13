using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.DataBank;
using AuditPlatform.Api.StorageCleanup;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Route("api/storage-cleanup")]
public sealed class StorageCleanupController(StorageCleanupService evidence,StoragePurgeService purge,
    PostgresReadSnapshot snapshots,IOptions<StorageCleanupOptions> options) : ControllerBase
{
    private VerifiedSession? Session=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    [HttpGet("preview")]
    public async Task<IActionResult> Preview([FromQuery] int client_id,[FromQuery] int template_id,[FromQuery] string kind="plans",CancellationToken ct=default)=>
        Result(await evidence.PreviewAsync(Session,client_id,template_id,kind,ct));
    [HttpPost("apply")]
    [RequestSizeLimit(16384)]
    public async Task<IActionResult> Apply([FromQuery] int client_id,[FromQuery] int template_id,[FromQuery] string kind,[FromBody] CleanupRequest request,CancellationToken ct)=>
        Result(await purge.ApplyAsync(Session,client_id,template_id,kind,request,ct));
    [HttpGet("operations/{id:guid}")]
    public async Task<IActionResult> Status(Guid id,[FromQuery] int client_id,[FromQuery] int template_id,CancellationToken ct)=>
        Result(await purge.StatusAsync(Session,client_id,template_id,id,ct));
    [HttpGet("operations")]
    public async Task<IActionResult> Recent([FromQuery] int client_id,[FromQuery] int template_id,CancellationToken ct)=>
        Result(await purge.RecentAsync(Session,client_id,template_id,ct));
    [HttpGet("scopes")]
    public async Task<IActionResult> Scopes([FromQuery] int client_id,CancellationToken ct)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503);
        if(Session is not {} actor) return Unauthorized();
        if(client_id<1) return BadRequest();
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var rows=await snapshots.ExecuteAsync(async(db,token)=>
            {
                await DataBankService.Authorize(db,actor,client_id,token,admin:true);
                await using var command=db.CreateCommand("SELECT t.id,left(n.display_name,201) FROM public.platform_tenant_templates n JOIN public.templates t ON t.id=n.template_id WHERE n.tenant_id=$1 AND n.binding_origin='native' AND t.is_active IS TRUE ORDER BY n.display_name,t.id LIMIT 501");
                command.Parameters.AddWithValue(actor.TenantId!.Value);var list=new List<object>();
                await using var reader=await command.ExecuteReaderAsync(token);
                while(await reader.ReadAsync(token))
                {if(list.Count==500||reader.GetString(1).Length>200) throw new CleanupRejected("too_large","cleanup_scope_capacity");list.Add(new {id=reader.GetInt32(0),name=reader.GetString(1)});}
                return list;
            },deadline.Token);
            await snapshots.ExecuteAsync(async(db,token)=>{await DataBankService.Authorize(db,actor,client_id,token,admin:true);return true;},deadline.Token);
            var body=new {client_id,templates=rows};
            if(System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(body,CleanupJson.Options).Length>options.Value.MaximumResponseBytes)
                return Result(new("too_large",Code:"cleanup_response_capacity"));
            return new JsonResult(body,CleanupJson.Options);
        }
        catch(Exception error) when(StorageCleanupService.Map(error,ct,deadline) is not null) {return Result(StorageCleanupService.Map(error,ct,deadline)!);}
    }
    [Route("{**remaining}")]
    public IActionResult Unported() {Response.Headers.CacheControl="no-store";return StatusCode(503,new {code="database_wide_maintenance_not_available"});}
    private IActionResult Result(CleanupResult result)
    {
        Response.Headers.CacheControl="no-store";
        return result.Status=="success"?new JsonResult(result.Value,CleanupJson.Options):StatusCode(result.Status switch {
            "unauthenticated"=>401,"forbidden"=>403,"not_found"=>404,"conflict" or "review"=>409,"too_large"=>413,"busy"=>429,"timeout"=>504,"unavailable"=>503,_=>400
        },new {code=result.Code});
    }
}
