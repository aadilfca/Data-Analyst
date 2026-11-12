namespace AuditPlatform.Api.Uploads;

public sealed record DerivedTreasury(string Account,string Type);
public sealed record UploadDerivedProfile(Dictionary<string,string> Keys,Dictionary<string,int> ColumnIds,string? RateMode,
    string TemplateKey,int Precision=0,string ContextKey="",IReadOnlyList<DerivedTreasury>? Treasury=null,bool CashflowSource=false,IReadOnlyList<string>? NumericKeys=null);
public sealed record UploadDerivedValues(string? Voucher,string? Account,string? AccountName,string? Date,string? Month,string? Fortnight,
    double Debit,double Credit,double? Amount,string? TreasuryType,string? TreasuryAccount,string Status);
public sealed record UploadVoucherDecision(string Voucher,string Identity,string Key,string Fingerprint,string? Date,long Lines,
    int Upload,string Sequence,string Status="current",string Reason="first_or_latest_exact_voucher_version",string? Canonical=null);
