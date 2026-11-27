using AuditPlatform.Api.Workbooks;
using AuditPlatform.Api.Templates;

namespace AuditPlatform.Api.Uploads;

// Source positions are one-based and independent of editable display labels.
// This context is informational, never an import/coverage approval token.
public sealed record PreparationMapping(int ColumnId,int? SourcePosition);
public sealed record PreparationRequest(int ClientId,int TemplateId,string Sheet,string Operation="map",
    string? Sha256=null,string? ConfigurationKey=null,long? MappingRevision=null,
    IReadOnlyList<PreparationMapping>? Mappings=null,string? DateDelimiter=null,string? DateOrder=null,
    IReadOnlyList<int>? ExcludedRows=null,IReadOnlyList<int>? ForceIncludeRows=null,int Page=1,int PageSize=10,
    Guid RequestId=default,int PageCount=1);
public sealed record PreparationHeader(int Position,string Label,bool Ambiguous);
public sealed record PreparedMapping(int ColumnId,string StorageKey,string Name,string Type,string? Role,
    bool Compare,int? SourcePosition,double Score,string Origin);
public sealed record PreparationContext(Guid RequestId,int FileId,int ClientId,int TemplateId,string Sheet,
    string Sha256,string ConfigurationKey,long MappingRevision,string SelectionKey);
public sealed record PreparedDateSample(int ColumnId,int Row,WorkbookCell Source,string? Parsed,string? Error);
public sealed record PreparedDateSuggestion(string Status,string? Start=null,string? End=null,int Count=0);
public sealed record PreparedRow(int Number,IReadOnlyList<WorkbookCell> Cells,bool Flagged,bool Excluded,bool ForceInclude);
public sealed record PreparationPage(int Page,IReadOnlyList<PreparedRow> Rows);
public sealed record PreparationOutput(PreparationContext Context,IReadOnlyList<PreparationHeader> Headers,
    IReadOnlyList<PreparedMapping> Mappings,IReadOnlyList<string> Warnings,
    IReadOnlyList<PreparedDateSample>? Samples=null,PreparedDateSuggestion? DateSuggestion=null,
    IReadOnlyList<PreparedRow>? Rows=null,int? TotalRows=null,int? SelectedRows=null,
    IReadOnlyList<PreparationPage>? CachedPages=null,bool? PreviewComplete=null);
public sealed record PreparationResult(string Status,PreparationOutput? Output=null,string? Code=null);
public sealed record OwnedWorkbookResult<T>(string Status,T? Value=default,string? Code=null);
internal sealed record PreparedSource(PreparationContext Context,TemplateDefinition Template,
    IReadOnlyList<PreparationHeader> Headers,IReadOnlyList<PreparedMapping> Mappings,IReadOnlyList<string> Warnings);
