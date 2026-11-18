using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Templates;

namespace AuditPlatform.Api.Mappings;

public sealed record SavedHeader(int ColumnId,string Header,string StorageKey);
public sealed record MappingEntry(int ColumnId,string Header);
public sealed record MappingSnapshot(long Revision,string ConfigurationKey,string BindingKey,bool NeedsReview,IReadOnlyList<SavedHeader> Entries);
public enum MappingStatus { Success,Forbidden,NotFound,Invalid,Conflict,Review }
public sealed record MappingResult(MappingStatus Status,MappingSnapshot? Snapshot=null,int Count=0,string? Detail=null);

public static class SavedMappingDefinition
{
    public static bool NeedsReview(TemplateDefinition template,string? storedBinding,IReadOnlyList<SavedHeader> entries,bool pendingAdoption=false)
    {
        var current=template.Columns.ToDictionary(column=>column.Id,column=>column.StorageKey);
        return pendingAdoption || entries.Count>0 && (storedBinding!=BindingKey(template) || template.Columns.Any(column=>column.Review)
            || entries.Any(entry=>!current.TryGetValue(entry.ColumnId,out var key)||key!=entry.StorageKey));
    }

    // Display-only changes do not invalidate stable header bindings. Edits to
    // meaning/order/policy require review, independently of the editor token.
    public static string BindingKey(TemplateDefinition t)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
        JsonSerializer.Serialize(new {t.Id,t.Purpose,Ledger=t.Ledger.OrderBy(k=>k.Key,StringComparer.Ordinal).ToArray(),
            t.Kind,t.RateSource,t.Subtotals,Columns=t.Columns.Select(c=>new {c.Id,c.StorageKey,c.Type,c.Role,c.Order,c.Active,
                c.Review,Compare=TenantTemplateService.EffectiveComparison(c,t)})}))));
}
