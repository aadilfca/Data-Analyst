using System.Text.Json.Serialization;
using AuditPlatform.Api.Uploads;

namespace AuditPlatform.Api.Reports;

public enum ReportInputKind { FinancialStatements, LedgerSource }
public enum ReportAccountCategorySource { LedgerSummary, FinancialStatement, Cashflow }

public sealed record ReportInputRequest
{
    public int ClientId { get; init; }
    public ReportInputKind Kind { get; init; }
    public int? UploadId { get; init; }
    public int? TemplateId { get; init; }
    public int? TrialBalanceUploadId { get; init; }
    public string? PeriodStart { get; init; }
    public string? PeriodEnd { get; init; }
    public ReportAccountCategorySource CategorySource { get; init; } = ReportAccountCategorySource.LedgerSummary;
    public IReadOnlyList<string>? SelectedAccountCodes { get; init; }
    public string? TreasuryView { get; init; }
}

public sealed record ReportInputDataset<T>(bool Requested, bool Complete, long RowCount,
    long Utf8Bytes, IReadOnlyList<T> Items);

public sealed record ReportInputIdentity(string Version, string Fingerprint, string ConfigurationFingerprint,
    DateTime CapturedAtUtc);

public sealed record ReportInputScope(int ClientId, ReportInputKind Kind, int? UploadId, int? TemplateId,
    IReadOnlyList<int> ResolvedUploadIds, int? TrialBalanceUploadId, string? PeriodStart, string? PeriodEnd,
    ReportAccountCategorySource? CategorySource, IReadOnlyList<string>? SelectedAccountCodes, string? TreasuryView);

public sealed record ReportInputSnapshot(ReportInputScope Scope, ReportInputIdentity Identity,
    ReportInputDataset<ReportLedgerUpload> ClientDataUploads,
    ReportInputDataset<ReportClientDataRow> ClientData,
    ReportInputDataset<ReportTrialBalanceUpload> TrialBalanceUploads,
    ReportInputDataset<ReportTrialBalanceAccount> TrialBalanceAccounts,
    ReportInputDataset<ReportTrialBalanceAdjustment> TrialBalanceAdjustments,
    ReportInputDataset<ReportAccountAssignment> AccountAssignments,
    ReportInputDataset<ReportCategoryMapping> CategoryMappings,
    ReportInputDataset<ReportCategory> Categories,
    ReportInputDataset<ReportStatementCalculation> StatementCalculations,
    ReportInputDataset<ReportStatementComponent> StatementComponents,
    ReportInputDataset<ReportProfitLink> ProfitLinks,
    ReportInputDataset<ReportProfitStatus> ProfitStatuses,
    ReportInputDataset<ReportCashflowSettings> CashflowSettings,
    ReportInputDataset<ReportCashflowAccountAssignment> CashflowAccountAssignments,
    ReportInputDataset<ReportCashflowRoleCategory> CashflowRoleCategories,
    ReportInputDataset<ReportCashflowTreasuryAccount> CashflowTreasuryAccounts,
    ReportInputDataset<ReportCashflowColumnMapping> CashflowColumnMappings);

public sealed record ReportInputCaptureResult(string Status, ReportInputSnapshot? Snapshot = null, string? Code = null);

public sealed record ReportLedgerUpload(int Id, int ClientId, int TemplateId, string FileName, DateTime? UploadDate);

public sealed record ReportTrialBalanceUpload(int Id, int ClientId, DateOnly AsOfDate, string FileName,
    DateOnly? PeriodStart, DateOnly? PeriodEnd, string? PeriodType, DateTime? UploadedAt,
    string? ValidationControlsJson);

public sealed record ReportClientDataRow(int Id, int TemplateId, int UploadId, int Version,
    string? SourceFingerprint, DateOnly? ExtractDate, ReportAmountValue? ExtractAmount, string RowDataJson);

public sealed record ReportTrialBalanceAccount(int Id, int UploadId, string AccountCode, string? SourceAccountCode,
    string? AccountName, string? ParentAccountCode, int Level, string Classification, bool ManuallyOverridden,
    int SortOrder, string? RawRowDataJson, ReportAmountValue? Debit, ReportAmountValue? Credit,
    ReportAmountValue? OpeningDebit, ReportAmountValue? OpeningCredit,
    ReportAmountValue? PeriodDebit, ReportAmountValue? PeriodCredit,
    ReportAmountValue? ClosingDebit, ReportAmountValue? ClosingCredit);

public sealed record ReportTrialBalanceAdjustment(int Id, int UploadId, int AccountId, int SourceExcelRow,
    ReportAmountValue ExpectedClosingNet, ReportAmountValue OriginalClosingNet, ReportAmountValue DifferenceNet,
    ReportAmountValue ClosingDebitAdjustment, ReportAmountValue ClosingCreditAdjustment);

public sealed record ReportAccountAssignment(int ClientId, string AccountCode, string? VerificationWorkstream,
    int? FinancialStatementCategoryId, int? NoteCategoryId);

public sealed record ReportCategoryMapping(int ClientId, string AccountCode, string Source,
    int? CategoryId, int? CategorySetId, string? CategoryKey, string? CategoryLabel);

public sealed record ReportCategory(int Id, int ClientId, string CategoryType, string CategoryKey, string Label,
    string? StatementSection, string? PresentationRole, string? IncomeStatementRole, string RowType,
    int? ParentCategoryId, int? SourceStatementCategoryId, int SortOrder, bool IsActive);

public sealed record ReportStatementCalculation(int Id, int ClientId, string StatementSection,
    string CalculationType, string Label, int? ParentHeadingId, int SortOrder);

public sealed record ReportStatementComponent(int CalculationId, int? SourceCategoryId,
    int? SourceCalculationId, string Operator, int SortOrder);

public sealed record ReportProfitLink(int ClientId, int SourceCalculationId, int TargetCategoryId);

public sealed record ReportProfitStatus(int UploadId, int ClientId, string ClosingStatus, string ContextKey);

public sealed record ReportCashflowSettings(int ClientId, bool MatchInternalTransfers, string ReceiptPresentation,
    int? SourceUploadId, int? SourceTemplateId);

public sealed record ReportCashflowAccountAssignment(int ClientId, string AccountCode, string Section,
    string? CategoryLabel, string CashBankRole, string TreatmentType);

public sealed record ReportCashflowRoleCategory(int ClientId, string RoleKey, int CategoryId);

public sealed record ReportCashflowTreasuryAccount(string AccountCode, string TreasuryType);

public sealed record ReportCashflowColumnMapping(int TemplateId, string Role, string ColumnName);

public sealed record ReportAmountValue(string RawValue, string StorageType, string ExactValue)
{
    [JsonIgnore]
    public UploadExactAmount Amount => UploadExactAmount.Parse(ExactValue);

    public static ReportAmountValue From(string? rawValue, string storageType)
    {
        if (rawValue is null) throw new FormatException("A report amount is missing.");
        var amount = UploadExactAmount.Parse(rawValue);
        return new(rawValue, storageType, amount.ToFixedString());
    }
}
