using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Logging;
using Microsoft.AspNetCore.Mvc;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Controllers;

[Route("api/error-logs")]
public sealed class ErrorLogsController(DiagnosticErrorManagementService service):ControllerBase
{
    public sealed record Update([property:JsonPropertyName("is_resolved")] bool? Resolved,
        [property:JsonPropertyName("resolution_notes")] string? Notes);
    private VerifiedSession? Session=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    [HttpGet] public Task<IActionResult> List([FromQuery]int skip=0,[FromQuery]int limit=50,[FromQuery]string? module=null,
        [FromQuery]string? error_category=null,[FromQuery]bool? is_resolved=null,[FromQuery]string? search=null,
        [FromQuery]string? start_date=null,[FromQuery]string? end_date=null,CancellationToken ct=default)=>
        Respond(()=>service.ReadAsync(Session,new(skip,limit,module,error_category,is_resolved,search,start_date,end_date),ct:ct));
    [HttpGet("stats")] public Task<IActionResult> Stats(CancellationToken ct)=>Respond(()=>service.ReadAsync(Session,new(),stats:true,ct:ct));
    [HttpGet("{id:int}")] public Task<IActionResult> Get(int id,CancellationToken ct)=>Respond(()=>service.ReadAsync(Session,new(),id,ct:ct));
    [HttpPut("{id:int}")][RequestSizeLimit(16384)] public Task<IActionResult> Save(int id,[FromBody]Update? input,CancellationToken ct)=>
        Respond(()=>service.ChangeAsync(Session,id,input!.Resolved,input.Notes,ct),input?.Resolved is not null);
    [HttpDelete("{id:int}")] public Task<IActionResult> Delete(int id,CancellationToken ct)=>Respond(()=>service.ChangeAsync(Session,id,null,null,ct));
    private async Task<IActionResult> Respond(Func<Task<ErrorLogResult>> operation,bool valid=true)
    {
        Response.Headers.CacheControl="no-store";
        if(!valid||!ModelState.IsValid)return UnprocessableEntity(new{detail="Invalid error-log request"});
        var result=await operation();if(result.Total is {} total)Response.Headers["X-Total-Count"]=total.ToString(System.Globalization.CultureInfo.InvariantCulture);
        return StatusCode(result.Status,result.Value);
    }
}
