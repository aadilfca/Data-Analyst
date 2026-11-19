using System.Text.Json;
using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Columns;
using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/columns")]
public sealed class ColumnsController(TenantColumnService columns,IOptions<ColumnManagementOptions> options) : ControllerBase
{
    public sealed record Input([property:JsonPropertyName("column_name")]string? Name,
        [property:JsonPropertyName("data_type")]string? Type,string Category="General",string? Notes=null,
        [property:JsonPropertyName("reconciliation_role")]string? Role=null,
        [property:JsonPropertyName("is_active")]JsonElement Active=default);
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    [HttpGet]
    public Task<IActionResult> Active(CancellationToken ct)=>List(false,ct);
    [HttpGet("all")]
    public Task<IActionResult> All(CancellationToken ct)=>List(true,ct);
    private async Task<IActionResult> List(bool all,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await columns.ListAsync(Session,all,ct);
        return result.Status==ColumnStatus.Success ? Ok(result.Columns!.Select(c=>Wire(c,all))) : Error(result);
    }
    [HttpGet("categories")]
    public async Task<IActionResult> Categories(CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await columns.ListAsync(Session,false,ct);
        return result.Status==ColumnStatus.Success ? Ok(result.Columns!.Select(c=>c.Category).Distinct(StringComparer.Ordinal)
            .Order(StringComparer.Ordinal).Select(category=>new {category})) : Error(result);
    }
    [HttpGet("close-matches")]
    public async Task<IActionResult> CloseMatches([FromQuery]string? name,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||name is null) return Invalid();
        var result=await columns.ListAsync(Session,false,ct);
        if(result.Status!=ColumnStatus.Success) return Error(result);
        var typed=name.Trim();
        return Ok(result.Columns!.Where(c=>typed.Length>0 && !c.Name.Equals(typed,StringComparison.OrdinalIgnoreCase)
            && (c.Name.Contains(typed,StringComparison.OrdinalIgnoreCase)||typed.Contains(c.Name,StringComparison.OrdinalIgnoreCase)))
            .Select(c=>new {id=c.Id,column_name=c.Name,data_type=c.Type,category=c.Category}));
    }
    [HttpGet("{id:int}/usage")]
    public async Task<IActionResult> Usage(int id,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await columns.UsageAsync(Session,id,ct);
        return result.Status==ColumnStatus.Success ? Ok(new {documents_count=0,cross_checks_count=0,templates_count=result.TemplatesCount}) : Error(result);
    }
    [HttpPost]
    public Task<IActionResult> Create([FromBody]Input? input,CancellationToken ct)=>Save(null,input,ct);
    [HttpPut("{id:int}")]
    public Task<IActionResult> Update(int id,[FromBody]Input? input,CancellationToken ct)=>Save(id,input,ct);
    private async Task<IActionResult> Save(int? id,Input? input,CancellationToken ct)
    {
        if(Guard(write:true) is {} error) return error;
        if(!ModelState.IsValid||input?.Name is null||input.Type is null||input.Category is null) return Invalid();
        var active=ActiveValue(input.Active);
        if(id is not null&&active is null) return Invalid();
        var result=await columns.SaveAsync(Session,id,new(input.Name,input.Type,input.Category,input.Notes,active??true,input.Role),ct);
        if(result.Status!=ColumnStatus.Success) return Error(result);
        return id is null ? Ok(new {message=$"Column '{input.Name}' created successfully",column_id=result.ColumnId})
            : Ok(new {message=$"Column '{input.Name}' updated successfully"});
    }
    [HttpDelete("{id:int}")]
    public async Task<IActionResult> Delete(int id,CancellationToken ct)
    {
        if(Guard(write:true) is {} error) return error;
        var result=await columns.DeleteAsync(Session,id,ct);
        return result.Status==ColumnStatus.Success ? Ok(new {message="Column deleted successfully"}) : Error(result);
    }
    private IActionResult? Guard(bool write=false)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {detail="Column management is unavailable"});
        if(write&&!options.Value.WriteEnabled) return StatusCode(503,new {detail="Column changes are disabled"});
        return Session is null ? Unauthorized(new {detail="Not authenticated"}) : null;
    }
    private IActionResult Invalid()=>UnprocessableEntity(new {detail=new[] {new {type="invalid_request",loc=new[] {"request"},msg="Invalid column request",input="[redacted]"}}});
    private IActionResult Error(ColumnResult result)=>StatusCode(result.Status switch {
        ColumnStatus.Forbidden=>403,ColumnStatus.NotFound=>404,ColumnStatus.DependencyReview=>409,_=>400},
        new {detail=result.Detail??(result.Status==ColumnStatus.NotFound ? "Column not found" : "Column access denied")});
    private static object Wire(TenantColumn c,bool all)
    {
        var result=new Dictionary<string,object?> { ["id"]=c.Id,["column_name"]=c.Name,["data_type"]=c.Type,["category"]=c.Category,["notes"]=c.Notes,
            ["storage_key"]=c.StorageKey,["definition_version"]=c.Version };
        if(all) result["is_active"]=c.Active;
        return result;
    }
    // Existing callers send 1/0; preserve that valid legacy representation as
    // well as JSON booleans. Identity/storage fields are never accepted here.
    private static bool? ActiveValue(JsonElement value)=>value.ValueKind switch {
        JsonValueKind.Undefined or JsonValueKind.True=>true, JsonValueKind.False=>false,
        JsonValueKind.Number when value.TryGetInt32(out var number)&&number is 0 or 1=>number==1,
        JsonValueKind.String=>value.GetString()?.ToLowerInvariant() switch {"1" or "true" or "yes" or "on"=>true,"0" or "false" or "no" or "off"=>false,_=>null}, _=>null };
}
