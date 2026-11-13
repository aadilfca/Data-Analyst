using AuditPlatform.Api.Templates;

namespace AuditPlatform.Api.DataBank;

public sealed record DataBankQuery(string Operation, int ClientId, int? TemplateId = null,
    string? Start = null, string? End = null, int Page = 1, int Limit = 50, string? Search = null,
    double? Minimum = null, double? Maximum = null, bool Sunday = false, string? Quality = null,
    string? Account = null, bool Metrics = false, int? Year = null, int? Month = null, int[]? TemplateIds = null,
    string Action = "all", int? RowId = null, string RevisionSource = "uploads");
public sealed record DataBankResult(string Status, object? Value = null, string? Code = null);
public sealed record DataBankDownload(string Status, byte[]? Bytes = null, string? Name = null, string? Code = null);
internal sealed record DataBankStoredRow(int Id, string Raw, string? Metadata = null, string? Account = null, string? AccountName = null);
internal sealed record DataBankStoredHistory(int Id, int RowId, string Action, string? At, string? Actor,
    int? SourceUpload, int? ReplacingUpload, string Raw, string? Changes, string? Account, string? AccountName, string? Metadata);
internal sealed record DataBankWorkbook(DataBankTemplate Template, DataBankQuery Query, bool Backup, DateTimeOffset CapturedAt,
    IReadOnlyList<DataBankStoredRow> Current, IReadOnlyList<DataBankStoredHistory> History);
internal sealed record DataBankTemplate(int Id, string Name, string Purpose, bool Subtotals,
    IReadOnlyList<TemplateColumn> Columns)
{
    public TemplateColumn? Document => Columns.SingleOrDefault(c => c.Role == "document_id") ?? Columns.FirstOrDefault();
}
internal sealed class DataBankRejected(string status, string code) : Exception(code)
{
    public string Status { get; } = status;
    public string Code { get; } = code;
}
