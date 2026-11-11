using System.Globalization;
using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Mappings;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/upload")]
public sealed class SavedMappingsController(TenantMappingService mappings,IOptions<SavedMappingOptions> options) : ControllerBase
{
    public sealed record Entry([property:JsonPropertyName("template_column_id")]int ColumnId,
        [property:JsonPropertyName("excel_column")]string? Header);
    public sealed record Input([property:JsonPropertyName("client_id")]int Client,
        [property:JsonPropertyName("template_id")]int Template,IReadOnlyList<Entry>? Mappings,
        [property:JsonPropertyName("mapping_revision")]long? Revision,
        [property:JsonPropertyName("configuration_key")]string? Configuration);
    private VerifiedSession? Session=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    [HttpGet("saved-mapping")]
    public async Task<IActionResult> Read([FromQuery(Name="client_id")]int? client,[FromQuery(Name="template_id")]int? template,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||client is not >0||template is not >0) return Invalid();
        var result=await mappings.ReadAsync(Session,client.Value,template.Value,ct);
        return result.Status==MappingStatus.Success ? Ok(Wire(result.Snapshot!)):Error(result);
    }
    [HttpPost("save-mapping")]
    public async Task<IActionResult> Save([FromBody]Input? input,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||input?.Client is not >0||input.Template<=0||input.Mappings is null
            || input.Mappings.Any(e=>e is null||e.Header is null)||input.Revision is null||input.Configuration is null) return Invalid();
        var result=await mappings.SaveAsync(Session,input.Client,input.Template,input.Mappings.Select(e=>new MappingEntry(e.ColumnId,e.Header!)).ToArray(),
            input.Revision.Value,input.Configuration,ct);
        return result.Status==MappingStatus.Success ? Ok(Wire(result.Snapshot!,"Column mapping saved successfully","saved_count",result.Count)):Error(result);
    }
    [HttpDelete("saved-mapping")]
    public async Task<IActionResult> Clear([FromQuery(Name="client_id")]int? client,[FromQuery(Name="template_id")]int? template,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||client is not >0||template is not >0||Request.Headers["X-Mapping-Revision"].Count!=1
            || !long.TryParse(Request.Headers["X-Mapping-Revision"],NumberStyles.None,CultureInfo.InvariantCulture,out var revision)
            || Request.Headers["X-Template-Revision"].Count!=1) return Invalid();
        var result=await mappings.ClearAsync(Session,client.Value,template.Value,revision,Request.Headers["X-Template-Revision"].ToString(),ct);
        return result.Status==MappingStatus.Success ? Ok(Wire(result.Snapshot!,"Saved column mapping cleared","deleted_count",result.Count)):Error(result);
    }
    private IActionResult? Guard()
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {detail="Saved mappings are unavailable"});
        return Session is null ? Unauthorized(new {detail="Not authenticated"}):null;
    }
    private IActionResult Invalid()=>UnprocessableEntity(new {detail=new[] {new {type="invalid_request",loc=new[] {"request"},msg="Invalid saved mapping request",input="[redacted]"}}});
    private IActionResult Error(MappingResult result)=>StatusCode(result.Status switch {
        MappingStatus.Forbidden=>403,MappingStatus.NotFound=>404,MappingStatus.Conflict or MappingStatus.Review=>409,_=>400},
        new {detail=result.Detail??(result.Status==MappingStatus.NotFound ? "Mapping scope not found":"Mapping access denied")});
    private static Dictionary<string,object?> Wire(MappingSnapshot snapshot,string? message=null,string? countName=null,int count=0)
    {
        var wire=new Dictionary<string,object?> { ["has_saved_mapping"]=snapshot.Entries.Count>0,
            ["mappings"]=snapshot.Entries.Select(e=>new {template_column_id=e.ColumnId,excel_column=e.Header,storage_key=e.StorageKey}),
            ["mapping_revision"]=snapshot.Revision,["configuration_key"]=snapshot.ConfigurationKey,["binding_key"]=snapshot.BindingKey,["needs_review"]=snapshot.NeedsReview };
        if(message is not null) wire["message"]=message;
        if(countName is not null) wire[countName]=count;
        return wire;
    }
}
