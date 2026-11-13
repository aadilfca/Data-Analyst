using AuditPlatform.Api.Templates;

namespace AuditPlatform.Api.DataBank;

public sealed record DataBankQuery(string Operation, int ClientId, int? TemplateId = null,
    string? Start = null, string? End = null, int Page = 1, int Limit = 50, string? Search = null,
    double? Minimum = null, double? Maximum = null, bool Sunday = false, string? Quality = null,
    string? Account = null, bool Metrics = false, int? Year = null, int? Month = null, int[]? TemplateIds = null);
public sealed record DataBankResult(string Status, object? Value = null, string? Code = null);
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
