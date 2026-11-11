using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Templates;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/templates")]
public sealed class TemplatesController(TenantTemplateService templates,IOptions<TemplateManagementOptions> options) : ControllerBase
{
    public sealed record Input([property:JsonPropertyName("template_name")]string? Name,
        [property:JsonPropertyName("selected_columns")]int[]? Columns,
        [property:JsonPropertyName("data_purpose")]string? Purpose=null,
        [property:JsonPropertyName("ledger_columns")]Dictionary<string,int>? Ledger=null,
        [property:JsonPropertyName("column_roles")]Dictionary<int,string?>? Roles=null,
        [property:JsonPropertyName("column_comparisons")]Dictionary<int,bool>? Comparisons=null,
        [property:JsonPropertyName("rate_source")]string? RateSource=null,
        [property:JsonPropertyName("has_subtotals_by_default")]bool Subtotals=false,
        [property:JsonPropertyName("column_versions")]Dictionary<int,long>? ColumnVersions=null);
    public sealed record LedgerInput([property:JsonPropertyName("ledger_columns")]Dictionary<string,int?>? Roles,
        [property:JsonPropertyName("template_name")]string Name="Account Ledger",
        [property:JsonPropertyName("column_comparisons")]Dictionary<string,bool>? Comparisons=null,
        [property:JsonPropertyName("has_subtotals_by_default")]bool Subtotals=false);
    private VerifiedSession? Session=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    private string? Revision=>Request.Headers["X-Template-Revision"].Count==1 ? Request.Headers["X-Template-Revision"].ToString():null;
    [HttpGet]
    public async Task<IActionResult> List([FromQuery]int? limit,[FromQuery]int offset=0,CancellationToken ct=default)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||limit is <1 or >200||offset<0) return Invalid();
        var result=await templates.ListAsync(Session,ct); if(result.Status!=TemplateStatus.Success) return Error(result);
        var rows=result.Templates!;
        if(limit is not null) Response.Headers["X-Total-Count"]=rows.Count.ToString();
        return Ok((limit is null ? rows:rows.Skip(offset).Take(limit.Value)).Select(Wire));
    }
    [HttpGet("{id:int}")]
    public async Task<IActionResult> Get(int id,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await templates.GetAsync(Session,id,ct);
        return result.Status==TemplateStatus.Success ? Ok(Wire(result.Templates![0])):Error(result);
    }
    [HttpGet("{id:int}/columns")]
    public async Task<IActionResult> Columns(int id,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await templates.GetAsync(Session,id,ct);
        return result.Status==TemplateStatus.Success ? Ok(result.Templates![0].Columns.Select(c=>ColumnWire(c,true))):Error(result);
    }
    [HttpGet("{id:int}/upload-columns")]
    public async Task<IActionResult> UploadColumns(int id,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await templates.GetAsync(Session,id,ct); if(result.Status!=TemplateStatus.Success) return Error(result);
        var t=result.Templates![0];
        return Ok(new {template_id=t.Id,columns=t.Columns.Select(c=>ColumnWire(c)),total_columns=t.Columns.Count,
            data_purpose=t.Purpose,ledger_columns=t.Ledger,configuration_key=t.ConfigurationKey,template_kind=t.Kind});
    }
    [HttpGet("by-name/{name}")]
    public async Task<IActionResult> ByName(string name,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await templates.ListAsync(Session,ct); if(result.Status!=TemplateStatus.Success) return Error(result);
        var t=result.Templates!.SingleOrDefault(t=>t.Name==name);
        return t is null ? Error(new(TemplateStatus.NotFound)):Ok(new {id=t.Id,template_name=t.Name,is_system=t.System});
    }
    [HttpPost]
    public Task<IActionResult> Create([FromBody]Input? input,CancellationToken ct)=>Save(null,input,ct);
    [HttpPut("{id:int}")]
    public Task<IActionResult> Update(int id,[FromBody]Input? input,CancellationToken ct)=>Save(id,input,ct);
    private async Task<IActionResult> Save(int? id,Input? input,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||input?.Name is null||input.Columns is null) return Invalid();
        var result=await templates.SaveAsync(Session,id,new(input.Name,input.Columns,input.Purpose,input.Ledger,input.Roles,
            input.Comparisons,input.RateSource,input.Subtotals,Revision,input.ColumnVersions),ct);
        return result.Status==TemplateStatus.Success ? Ok(new {message="Template saved successfully",template_id=result.Id}):Error(result);
    }
    [HttpDelete("{id:int}")]
    public async Task<IActionResult> Delete(int id,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await templates.DeleteAsync(Session,id,Revision,ct);
        return result.Status==TemplateStatus.Success ? Ok(new {message="Template deleted successfully (soft-delete)"}):Error(result);
    }
    [HttpPost("{id:int}/reorder")]
    public async Task<IActionResult> Reorder(int id,[FromBody]int[]? columns,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||columns is null) return Invalid();
        var result=await templates.ReorderAsync(Session,id,columns,Revision,ct);
        return result.Status==TemplateStatus.Success ? Ok(new {message="Order updated successfully"}):Error(result);
    }
    [HttpDelete("{id:int}/columns/{column:int}")]
    public async Task<IActionResult> Remove(int id,int column,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await templates.RemoveAsync(Session,id,column,Revision,ct);
        return result.Status==TemplateStatus.Success ? Ok(new {message="Column removed from template"}):Error(result);
    }
    [HttpGet("ledger-setup")]
    public async Task<IActionResult> LedgerPreview(CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await templates.LedgerPreviewAsync(Session,ct); if(result.Status!=TemplateStatus.Success) return Error(result);
        var draft=result.Templates![0]; string[] roles=["voucher","date","description","debit","credit","balance"];
        return Ok(new {template_name="Account Ledger",columns=draft.Columns.Select((c,index)=>new {role=roles[index],column_name=c.Name,
            data_type=c.Type,column_id=c.Id==0 ? (int?)null:c.Id,storage_key=c.StorageKey,definition_version=c.Version})});
    }
    [HttpPost("ledger-setup")]
    public async Task<IActionResult> LedgerSetup([FromBody]LedgerInput? input,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid||input?.Roles is null||input.Name is null) return Invalid();
        var result=await templates.PrepareLedgerAsync(Session,input.Name,input.Roles,input.Comparisons,input.Subtotals,ct);
        return result.Status==TemplateStatus.Success ? Ok(new {message=result.Reused ? "Account Ledger template already configured.":"Account Ledger template created successfully",
            template_id=result.Id,reused=result.Reused}):Error(result);
    }
    private IActionResult? Guard()
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {detail="Template management is unavailable"});
        return Session is null ? Unauthorized(new {detail="Not authenticated"}):null;
    }
    private IActionResult Invalid()=>UnprocessableEntity(new {detail=new[] {new {type="invalid_request",loc=new[] {"request"},msg="Invalid template request",input="[redacted]"}}});
    private IActionResult Error(TemplateResult result)=>StatusCode(result.Status switch {
        TemplateStatus.Forbidden=>403,TemplateStatus.NotFound=>404,TemplateStatus.Review=>409,_=>400},
        new {detail=result.Detail??(result.Status==TemplateStatus.NotFound ? "Template not found":"Template access denied")});
    private static object Wire(TemplateDefinition t)=>new {id=t.Id,template_name=t.Name,created_by=t.CreatedBy,created_date=t.CreatedDate,
        is_active=true,is_system=t.System,rate_source=t.RateSource,has_subtotals_by_default=t.Subtotals,data_purpose=t.Purpose,ledger_columns=t.Ledger,
        columns=t.Columns.Select(c=>ColumnWire(c)),definition_version=t.Version,configuration_key=t.ConfigurationKey,template_kind=t.Kind};
    private static object ColumnWire(TemplateColumn c,bool columnId=false)
    {
        var row=new Dictionary<string,object?> { [columnId ? "column_id":"id"]=c.Id,["column_name"]=c.Name,["storage_key"]=c.StorageKey,
            ["data_type"]=c.Type,["category"]=c.Category,["reconciliation_role"]=c.Role,["compare_for_changes"]=c.Compare,
            ["comparison_review_required"]=c.Review,["sort_order"]=c.Order,["definition_version"]=c.Version,["is_active"]=c.Active };
        return row;
    }
}
