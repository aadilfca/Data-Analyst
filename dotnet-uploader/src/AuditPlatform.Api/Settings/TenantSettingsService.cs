using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Npgsql;

namespace AuditPlatform.Api.Settings;

public enum SettingsStatus { Success, Forbidden, NotFound, Invalid }
public sealed record SettingsResult(SettingsStatus Status, object? Value = null, string? Detail = null);
public sealed record Holiday(int Id, DateOnly Date, string Label, bool RepeatsYearly, DateTime CreatedAt);
public sealed record CashflowDefaults(int FiscalMonth, string Period, string? CustomStart, string? CustomEnd);

// Settings are workspace-wide, not legal ownership or engagement permissions.
// No SQLite access, schema initialization or implicit legacy-data copying.
public sealed class TenantSettingsService(PostgresTransactions transactions, PostgresAuditWriter audit, TimeProvider? clock = null)
{
    private sealed record Actor(int TenantId, int UserId, string Name, string Role);
    private static readonly Dictionary<string,string> Defaults = new(StringComparer.Ordinal)
    {
        ["date_format"]="DD/MM/YYYY", ["amount_grouping"]="000", ["decimal_precision"]="0", ["rate_precision"]="0",
        ["default_date_range"]="Current Month", ["materiality_threshold"]="50000",
        ["cashflow_fiscal_year_start_month"]="1", ["cashflow_default_period"]="ytd",
        ["cashflow_custom_start"]="", ["cashflow_custom_end"]=""
    };
    private static readonly HashSet<string> Ordinary = ["date_format","amount_grouping","decimal_precision","rate_precision","default_date_range","materiality_threshold"];
    private static readonly HashSet<string> Periods = ["financial_year","ytd","last_12_months","custom"];
    private const string BackupKey="upload_allow_without_backup";

    public Task<SettingsResult> ReadAsync(VerifiedSession? session, string? key, CancellationToken ct = default) =>
        RunAsync(session,false,async (db,actor,token)=>
        {
            if (key is not null && !Defaults.ContainsKey(key)) return Missing(key);
            var values=await ValuesAsync(db,actor.TenantId,token);
            return new(SettingsStatus.Success,key is null ? values : new { key,value=values[key] });
        },ct);

    public Task<SettingsResult> SaveAsync(VerifiedSession? session,string key,string value,CancellationToken ct = default) =>
        RunAsync(session,true,async (db,actor,token)=>
        {
            if (!Ordinary.Contains(key)) return Defaults.ContainsKey(key) || key==BackupKey
                ? new(SettingsStatus.Forbidden,Detail: "Use the dedicated administrator setting route") : Missing(key);
            // Keep legacy string values/precision. Validation for typed report
            // defaults and backup policy belongs to their dedicated endpoints.
            await PutAsync(db,actor.TenantId,key,value,token);
            await AuditAsync(db,actor,"Update_"+key,null,token);
            return new(SettingsStatus.Success,new {message=$"Setting '{key}' updated successfully",key,value});
        },ct);

    public Task<SettingsResult> CashflowsAsync(VerifiedSession? session,CashflowDefaults? update,CancellationToken ct = default) =>
        RunAsync(session,update is not null,async (db,actor,token)=>
        {
            var values=await ValuesAsync(db,actor.TenantId,token);
            if (update is not null)
            {
                var error=ValidateDefaults(update);
                if (error is not null) return new(SettingsStatus.Invalid,Detail: error);
                values["cashflow_fiscal_year_start_month"]=update.FiscalMonth.ToString(CultureInfo.InvariantCulture);
                values["cashflow_default_period"]=update.Period;
                values["cashflow_custom_start"]=string.IsNullOrEmpty(update.CustomStart) ? "" : ParseDate(update.CustomStart)!.Value.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture);
                values["cashflow_custom_end"]=string.IsNullOrEmpty(update.CustomEnd) ? "" : ParseDate(update.CustomEnd)!.Value.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture);
                foreach (var key in Defaults.Keys.Where(k=>k.StartsWith("cashflow_",StringComparison.Ordinal)))
                    await PutAsync(db,actor.TenantId,key,values[key],token);
                await AuditAsync(db,actor,"UpdateCashflowDefaults",null,token);
            }
            return new(SettingsStatus.Success,ResolveCashflows(values,DateOnly.FromDateTime((clock??TimeProvider.System).GetLocalNow().DateTime)));
        },ct);

    public Task<SettingsResult> BackupsAsync(VerifiedSession? session,bool? update,CancellationToken ct = default) =>
        RunAsync(session,true,async (db,actor,token)=>
        {
            bool allowed;
            if (update is not null)
            {
                allowed=update.Value;
                await PutAsync(db,actor.TenantId,BackupKey,allowed ? "true" : "false",token);
                await AuditAsync(db,actor,"UpdateUploadBackupPolicy",null,token);
            }
            else
            {
                await using var command=db.CreateCommand("SELECT value FROM public.platform_tenant_settings WHERE tenant_id=$1 AND key=$2");
                command.Parameters.AddWithValue(actor.TenantId); command.Parameters.AddWithValue(BackupKey);
                // A missing/unknown policy requires backups; only explicit true opts out.
                allowed=(await command.ExecuteScalarAsync(token)) as string == "true";
            }
            return new(SettingsStatus.Success,new {allow_without_backup=allowed});
        },ct);

    public Task<SettingsResult> HolidaysAsync(VerifiedSession? session,CancellationToken ct = default) =>
        RunAsync(session,false,async (db,actor,token)=>
        {
            await using var command=db.CreateCommand("SELECT id,holiday_date,label,repeats_yearly,created_at FROM public.platform_tenant_holidays WHERE tenant_id=$1 ORDER BY holiday_date,id");
            command.Parameters.AddWithValue(actor.TenantId);
            await using var reader=await command.ExecuteReaderAsync(token);
            var rows=new List<object>();
            while (await reader.ReadAsync(token)) rows.Add(HolidayWire(ReadHoliday(reader)));
            return new(SettingsStatus.Success,rows);
        },ct);

    public Task<SettingsResult> SaveHolidayAsync(VerifiedSession? session,int? id,string date,string label,bool yearly,CancellationToken ct = default) =>
        RunAsync(session,true,async (db,actor,token)=>
        {
            // Match the legacy precedence: an update first checks scoped existence.
            if (id is not null && !await HolidayExistsAsync(db,actor.TenantId,id.Value,token)) return HolidayMissing(id.Value);
            var parsed=ParseDate(date);
            if (parsed is null) return new(SettingsStatus.Invalid,Detail: $"Invalid date '{date}' - expected format YYYY-MM-DD");
            if (string.IsNullOrWhiteSpace(label)) return new(SettingsStatus.Invalid,Detail: "Label cannot be empty");
            await using var command=db.CreateCommand(id is null ? """
                INSERT INTO public.platform_tenant_holidays(tenant_id,holiday_date,label,repeats_yearly)
                VALUES($1,$2,$3,$4) RETURNING id,holiday_date,label,repeats_yearly,created_at
                """ : """
                UPDATE public.platform_tenant_holidays SET holiday_date=$2,label=$3,repeats_yearly=$4
                WHERE tenant_id=$1 AND id=$5 RETURNING id,holiday_date,label,repeats_yearly,created_at
                """);
            command.Parameters.AddWithValue(actor.TenantId); command.Parameters.AddWithValue(parsed.Value);
            command.Parameters.AddWithValue(label.Trim()); command.Parameters.AddWithValue(yearly);
            if (id is not null) command.Parameters.AddWithValue(id.Value);
            Holiday holiday;
            await using (var reader=await command.ExecuteReaderAsync(token))
            { await reader.ReadAsync(token); holiday=ReadHoliday(reader); }
            await AuditAsync(db,actor,id is null ? "CreateHoliday" : "UpdateHoliday",holiday.Id,token);
            return new(SettingsStatus.Success,HolidayWire(holiday));
        },ct);

    public Task<SettingsResult> DeleteHolidayAsync(VerifiedSession? session,int id,CancellationToken ct = default) =>
        RunAsync(session,true,async (db,actor,token)=>
        {
            await using var command=db.CreateCommand("DELETE FROM public.platform_tenant_holidays WHERE tenant_id=$1 AND id=$2");
            command.Parameters.AddWithValue(actor.TenantId); command.Parameters.AddWithValue(id);
            if (await command.ExecuteNonQueryAsync(token)==0) return HolidayMissing(id);
            await AuditAsync(db,actor,"DeleteHoliday",id,token);
            return new(SettingsStatus.Success,new { message=$"Holiday ID {id} deleted successfully" });
        },ct);

    private Task<SettingsResult> RunAsync(VerifiedSession? session,bool admin,
        Func<PostgresTransactionContext,Actor,CancellationToken,Task<SettingsResult>> operation,CancellationToken ct)
    {
        if (session is null || !session.TenantContextRequired || session.TenantId is not >0 || !AuthenticationService.ValidToken(session.Token))
            return Task.FromResult(new SettingsResult(SettingsStatus.Forbidden));
        return transactions.ExecuteAsync(async (db,token)=>
        {
            // Same tenant-first lock order as existing tenant administration.
            await using (var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE"))
            {
                tenant.Parameters.AddWithValue(session.TenantId.Value);
                if (await tenant.ExecuteScalarAsync(token) is null) return new SettingsResult(SettingsStatus.Forbidden);
            }
            Actor actor;
            await using (var command=db.CreateCommand("""
                SELECT u.id,u.username,m.tenant_role FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                  AND s.selected_tenant_id=$3 AND s.context_version=$4
                  AND m.tenant_id=$3 AND m.is_active IS TRUE FOR SHARE OF s,u,m
                """))
            {
                command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
                command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion);
                await using var reader=await command.ExecuteReaderAsync(token);
                if (!await reader.ReadAsync(token) || (admin && reader.GetString(2)!="admin")) return new SettingsResult(SettingsStatus.Forbidden);
                actor=new(session.TenantId.Value,reader.GetInt32(0),reader.GetString(1),reader.GetString(2));
            }
            return await operation(db,actor,token);
        },ct);
    }

    private static async Task<Dictionary<string,string>> ValuesAsync(PostgresTransactionContext db,int tenant,CancellationToken ct)
    {
        var values=new Dictionary<string,string>(Defaults,StringComparer.Ordinal);
        await using var command=db.CreateCommand("SELECT key,value FROM public.platform_tenant_settings WHERE tenant_id=$1");
        command.Parameters.AddWithValue(tenant); await using var reader=await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) if (Defaults.ContainsKey(reader.GetString(0))) values[reader.GetString(0)]=reader.GetString(1);
        return values;
    }
    private static async Task PutAsync(PostgresTransactionContext db,int tenant,string key,string value,CancellationToken ct)
    {
        await using var command=db.CreateCommand("""
            INSERT INTO public.platform_tenant_settings(tenant_id,key,value) VALUES($1,$2,$3)
            ON CONFLICT(tenant_id,key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(key); command.Parameters.AddWithValue(value);
        await command.ExecuteNonQueryAsync(ct);
    }
    // Restore reuses the same setting rules within its caller's transaction.
    internal async Task<string?> RestoreAsync(PostgresTransactionContext db,int tenant,int user,string name,
        IReadOnlyDictionary<string,string> input,CancellationToken ct)
    {
        var error=await ValidateRestoreAsync(db,tenant,input,ct);
        if(error is not null) return error;
        foreach (var (key,value) in input) await PutAsync(db,tenant,key,value,ct);
        if (input.Count>0) await AuditAsync(db,new Actor(tenant,user,name,"admin"),"RestoreSettings",null,ct);
        return null;
    }
    internal static async Task<string?> ValidateRestoreAsync(PostgresTransactionContext db,int tenant,
        IReadOnlyDictionary<string,string> input,CancellationToken ct)
    {
        if (input.Any(pair => pair.Key is null || pair.Value is null || pair.Key.Contains('\0') || pair.Value.Contains('\0')
            || (!Defaults.ContainsKey(pair.Key) && pair.Key != BackupKey))) return "Invalid settings in backup";
        var values = await ValuesAsync(db,tenant,ct);
        foreach (var (key,value) in input) if (Defaults.ContainsKey(key)) values[key]=value;
        if (!int.TryParse(values["cashflow_fiscal_year_start_month"],NumberStyles.None,CultureInfo.InvariantCulture,out var month))
            return "Invalid Cashflows settings in backup";
        var error=ValidateDefaults(new CashflowDefaults(month,values["cashflow_default_period"],
            values["cashflow_custom_start"],values["cashflow_custom_end"]));
        if (error is not null) return error;
        if (input.TryGetValue(BackupKey,out var policy) && policy is not ("true" or "false"))
            return "Invalid upload backup policy";
        return null;
    }
    private Task<int> AuditAsync(PostgresTransactionContext db,Actor actor,string action,int? id,CancellationToken ct) =>
        audit.WriteAsync(db,new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),new LogActor("authenticated",actor.UserId,actor.Name),null,
            action,"TenantSettings",JsonSerializer.SerializeToElement(new {tenant_id=actor.TenantId,record_id=id})),ct);
    private static DateOnly? ParseDate(string? value) => DateOnly.TryParseExact(value,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var result) ? result : null;
    private static SettingsResult Missing(string key) => new(SettingsStatus.NotFound,Detail: $"Setting '{key}' not found");
    private static SettingsResult HolidayMissing(int id) => new(SettingsStatus.NotFound,Detail: $"Holiday ID {id} not found");
    private static async Task<bool> HolidayExistsAsync(PostgresTransactionContext db,int tenant,int id,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT id FROM public.platform_tenant_holidays WHERE tenant_id=$1 AND id=$2 FOR UPDATE");
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(id); return await command.ExecuteScalarAsync(ct) is not null;
    }
    private static Holiday ReadHoliday(NpgsqlDataReader r) => new(r.GetInt32(0),r.GetFieldValue<DateOnly>(1),r.GetString(2),r.GetBoolean(3),r.GetDateTime(4));
    private static object HolidayWire(Holiday h) => new {id=h.Id,date=h.Date.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),label=h.Label,repeats_yearly=h.RepeatsYearly,created_at=h.CreatedAt.ToString("O")};

    private static string? ValidateDefaults(CashflowDefaults update)
    {
        if (update.FiscalMonth is <1 or >12) return "Fiscal-year start month must be between 1 and 12";
        if (!Periods.Contains(update.Period)) return "Invalid Cashflows default period";
        foreach (var (value,name) in new[] {(update.CustomStart,"Custom start date"),(update.CustomEnd,"Custom end date")})
        {
            if (string.IsNullOrEmpty(value)) { if (update.Period=="custom") return name+" is required for a custom period"; }
            else if (ParseDate(value) is null) return name+" must use YYYY-MM-DD format";
        }
        if (ParseDate(update.CustomStart) is { } start && ParseDate(update.CustomEnd) is { } end && start>end)
            return "Custom start date cannot be after custom end date";
        return null;
    }
    public static object ResolveCashflows(IReadOnlyDictionary<string,string> values,DateOnly today)
    {
        string Get(string key) => values.TryGetValue(key,out var value) ? value : Defaults[key];
        var fiscal=int.TryParse(Get("cashflow_fiscal_year_start_month"),out var month) && month is >=1 and <=12 ? month : 1;
        var period=Get("cashflow_default_period"); if (!Periods.Contains(period)) period="ytd";
        var rawStart=Get("cashflow_custom_start"); var rawEnd=Get("cashflow_custom_end");
        var customStart=ParseDate(rawStart); var customEnd=ParseDate(rawEnd);
        if ((!string.IsNullOrEmpty(rawStart) && customStart is null) || (!string.IsNullOrEmpty(rawEnd) && customEnd is null)) customStart=customEnd=null;
        if (period=="custom" && (customStart is null || customEnd is null || customStart>customEnd)) period="ytd";
        var fiscalStart=new DateOnly(today.Month>=fiscal ? today.Year : today.Year-1,fiscal,1);
        var start=period switch { "financial_year" or "ytd"=>fiscalStart, "last_12_months"=>new DateOnly(today.Year,today.Month,1).AddMonths(-11), _=>customStart!.Value };
        var end=period switch { "financial_year"=>fiscalStart.AddYears(1).AddDays(-1), "custom"=>customEnd!.Value, _=>today };
        return new {fiscal_year_start_month=fiscal,default_period=period,custom_start=customStart?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture)??"",
            custom_end=customEnd?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture)??"",resolved_period=new {start=start.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),end=end.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),group_by="month"}};
    }
}
