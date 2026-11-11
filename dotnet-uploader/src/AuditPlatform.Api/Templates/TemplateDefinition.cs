using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AuditPlatform.Api.Templates;

public sealed record TemplateColumn(int Id,string Name,string StorageKey,string Type,string Category,
    string? Role,bool? Compare,bool Review,int Order,long Version,bool Active);
public sealed record TemplateDefinition(int Id,string Name,string CreatedBy,DateTime CreatedDate,bool System,
    bool Subtotals,string Purpose,Dictionary<string,int> Ledger,string? RateSource,string Kind,string Origin,
    long Version,IReadOnlyList<TemplateColumn> Columns)
{
    // Includes current column definitions: a changed label/type invalidates an
    // editor snapshot even if no template mapping was edited in the meantime.
    public string ConfigurationKey => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
        JsonSerializer.Serialize(new {Id,Version,Name,Subtotals,Purpose,Ledger,RateSource,Kind,Columns}))));
}
public sealed record TemplateInput(string Name,IReadOnlyList<int> Columns,string? Purpose=null,
    Dictionary<string,int>? Ledger=null,Dictionary<int,string?>? Roles=null,Dictionary<int,bool>? Comparisons=null,
    string? RateSource=null,bool Subtotals=false,string? Revision=null,Dictionary<int,long>? ColumnVersions=null);
public enum TemplateStatus { Success,Forbidden,NotFound,Invalid,Conflict,Review }
public sealed record TemplateResult(TemplateStatus Status,IReadOnlyList<TemplateDefinition>? Templates=null,
    int? Id=null,string? Detail=null,bool Reused=false);
