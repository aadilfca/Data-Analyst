using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Settings;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/settings")]
public sealed class SettingsController(TenantSettingsService settings,IOptions<SettingsManagementOptions> options) : ControllerBase
{
    public sealed record SettingInput(string? Value);
    public sealed record HolidayInput(string? Date,string? Label,[property: JsonPropertyName("repeats_yearly")] bool RepeatsYearly=false);
    public sealed record CashflowInput([property: JsonPropertyName("fiscal_year_start_month")] int FiscalMonth=1,
        [property: JsonPropertyName("default_period")] string? Period="ytd",
        [property: JsonPropertyName("custom_start")] string? CustomStart=null,
        [property: JsonPropertyName("custom_end")] string? CustomEnd=null);
    public sealed record BackupInput([property: JsonPropertyName("allow_without_backup")] bool? Allowed);
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

    [HttpGet]
    public Task<IActionResult> List(CancellationToken ct) => Execute(()=>settings.ReadAsync(Session,null,ct));
    [HttpGet("{key}")]
    public Task<IActionResult> Get(string key,CancellationToken ct) => Execute(()=>settings.ReadAsync(Session,key,ct));
    [HttpPost("{key}")]
    public Task<IActionResult> Save(string key,[FromBody] SettingInput? input,CancellationToken ct) =>
        Execute(()=>settings.SaveAsync(Session,key,input!.Value!,ct),input?.Value is not null);

    [HttpGet("holidays")]
    public Task<IActionResult> Holidays(CancellationToken ct) => Execute(()=>settings.HolidaysAsync(Session,ct));
    [HttpPost("holidays")]
    public Task<IActionResult> CreateHoliday([FromBody] HolidayInput? input,CancellationToken ct) => SaveHoliday(null,input,ct);
    [HttpPut("holidays/{id:int}")]
    public Task<IActionResult> UpdateHoliday(int id,[FromBody] HolidayInput? input,CancellationToken ct) => SaveHoliday(id,input,ct);
    private Task<IActionResult> SaveHoliday(int? id,HolidayInput? input,CancellationToken ct) =>
        Execute(()=>settings.SaveHolidayAsync(Session,id,input!.Date!,input.Label!,input.RepeatsYearly,ct),input?.Date is not null && input.Label is not null);
    [HttpDelete("holidays/{id:int}")]
    public Task<IActionResult> DeleteHoliday(int id,CancellationToken ct) => Execute(()=>settings.DeleteHolidayAsync(Session,id,ct));

    [HttpGet("cashflows-defaults")]
    public Task<IActionResult> Cashflows(CancellationToken ct) => Execute(()=>settings.CashflowsAsync(Session,null,ct));
    [HttpPost("cashflows-defaults")]
    public Task<IActionResult> SaveCashflows([FromBody] CashflowInput? input,CancellationToken ct) =>
        Execute(()=>settings.CashflowsAsync(Session,new(input!.FiscalMonth,input.Period!,input.CustomStart,input.CustomEnd),ct),input?.Period is not null);
    [HttpGet("upload-backups")]
    public Task<IActionResult> Backups(CancellationToken ct) => Execute(()=>settings.BackupsAsync(Session,null,ct));
    [HttpPost("upload-backups")]
    public Task<IActionResult> SaveBackups([FromBody] BackupInput? input,CancellationToken ct) =>
        Execute(()=>settings.BackupsAsync(Session,input!.Allowed,ct),input?.Allowed is not null);

    private async Task<IActionResult> Execute(Func<Task<SettingsResult>> operation,bool valid=true)
    {
        Response.Headers.CacheControl="no-store";
        if (!options.Value.Enabled) return StatusCode(503,new {detail="Settings are unavailable"});
        if (Session is null) return Unauthorized(new {detail="Not authenticated"});
        if (!valid || !ModelState.IsValid) return UnprocessableEntity(new {detail=new[] {
            new {type="invalid_request",loc=new[] {"body"},msg="Invalid settings request",input="[redacted]"} }});
        var result=await operation();
        return result.Status switch {
            SettingsStatus.Success=>Ok(result.Value),
            SettingsStatus.Forbidden=>StatusCode(403,new {detail=result.Detail??"Settings access denied"}),
            SettingsStatus.NotFound=>NotFound(new {detail=result.Detail}),
            _=>BadRequest(new {detail=result.Detail??"Invalid settings request"}) };
    }
}
