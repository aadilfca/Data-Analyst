using System.Collections.ObjectModel;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.TrialBalance;

public static class TrialBalanceParser
{
    // Trusted server-side file adapter only. No HTTP path, client permission,
    // persistence or import authorization is supplied by this parsing phase.
    public static TrialBalanceReview Parse(string path,string extension,TrialBalanceInput input,
        TrialBalanceLimits? limits=null,CancellationToken cancellationToken=default)
    {
        var bounds=limits??new();var workbook=bounds.Workbook??new();
        if(bounds.MaximumDataRows is <1 or >100000||bounds.MaximumEvidenceBytes is <1 or >33554432
            ||bounds.MaximumIssues is <1 or >10000||bounds.TimeoutSeconds is <1 or >60
            ||workbook.MaximumFileBytes is <1 or >67108864||workbook.MaximumExpandedBytes is <1 or >268435456
            ||workbook.MaximumColumns is <1 or >1024||workbook.MaximumSheets is <1 or >100
            ||workbook.MaximumCellCharacters is <1 or >65536||workbook.MaximumRows<1)
            throw new TrialBalanceRejected("invalid_limits","Trial Balance limits exceed the approved bounds.");
        if(input?.Mapping is null) throw new TrialBalanceRejected("invalid_mapping","Supply explicit Trial Balance mappings.");
        if(input.Mapping.Count>9||input.CodeOverrides?.Count>bounds.MaximumDataRows||input.ClassificationOverrides?.Count>bounds.MaximumDataRows
            ||input.ExcludedExcelRows?.Count>bounds.MaximumDataRows||input.Mapping.Any(p=>p.Key.Length>64||p.Value?.Length>65536))
            throw new TrialBalanceRejected("request_capacity","Trial Balance review choices exceed their limit.");
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(TimeSpan.FromSeconds(bounds.TimeoutSeconds));var ct=deadline.Token;
        try
        {
            ct.ThrowIfCancellationRequested();
            var frozen=new TrialBalanceInput(new ReadOnlyDictionary<string,string?>(input.Mapping.ToDictionary()),
                new ReadOnlyDictionary<int,string>((input.CodeOverrides??new Dictionary<int,string>()).ToDictionary()),
                new ReadOnlyDictionary<int,string>((input.ClassificationOverrides??new Dictionary<int,string>()).ToDictionary(p=>p.Key,p=>p.Value?.Trim()??"")),
                Array.AsReadOnly((input.ExcludedExcelRows??[]).ToArray()),input.TotalExcelRow,input.NoTotalRow);
            long inputBytes=TrialBalanceBudget.Size(frozen,bounds.MaximumEvidenceBytes);
            workbook=workbook with {MaximumRows=Math.Min(workbook.MaximumRows,bounds.MaximumDataRows+1)};
            var sheets=WorkbookReader.Discover(path,extension,workbook,ct);
            string sheet=sheets[0].Name,hash=HashFile(path,workbook.MaximumFileBytes,ct);
            using var reader=WorkbookReader.Read(path,extension,sheet,workbook,ct).GetEnumerator();
            if(!reader.MoveNext()||reader.Current.Number!=1) throw new TrialBalanceRejected("invalid_header","The first row must contain source headers.");
            var labels=reader.Current.Cells.Select(Text).ToArray();
            var headers=reader.Current.Cells.Select((c,i)=>new TrialBalanceHeader(c.Column,labels[i],string.IsNullOrWhiteSpace(labels[i])
                ||labels.Count(n=>n==labels[i])>1||c.FormulaState==WorkbookFormulaState.MissingResult||c.Kind!=WorkbookCellKind.Text)).ToArray();
            var binding=TrialBalanceMapping.Bind(frozen.Mapping,headers);
            long retained=inputBytes+TrialBalanceBudget.Size(headers,bounds.MaximumEvidenceBytes-inputBytes);
            var rows=new List<TrialBalanceSourceRow>();
            while(reader.MoveNext())
            {
                ct.ThrowIfCancellationRequested();
                if(rows.Count>=bounds.MaximumDataRows) throw new TrialBalanceRejected("row_capacity","Trial Balance has too many source rows.");
                var source=new TrialBalanceSourceRow(reader.Current.Number,Array.AsReadOnly(reader.Current.Cells.ToArray()));
                retained+=TrialBalanceBudget.Size(source,bounds.MaximumEvidenceBytes-retained);rows.Add(source);
            }
            var result=TrialBalanceValidation.Build(sheet,hash,Fingerprint(frozen),Array.AsReadOnly(headers),rows.AsReadOnly(),frozen,binding,bounds,ct);
            TrialBalanceBudget.Size(result,bounds.MaximumEvidenceBytes-inputBytes);ct.ThrowIfCancellationRequested();
            if(hash!=HashFile(path,workbook.MaximumFileBytes,ct)) throw new TrialBalanceRejected("source_changed","The source workbook changed during review.");
            return result;
        }
        catch(OperationCanceledException) when(!cancellationToken.IsCancellationRequested&&deadline.IsCancellationRequested)
        {throw new TrialBalanceRejected("parsing_timeout","Trial Balance parsing exceeded its deadline.");}
    }
    internal static WorkbookCell Cell(TrialBalanceSourceRow row,int position)=>row.Cells.FirstOrDefault(c=>c.Column==position)??new(row.ExcelRow,position,WorkbookCellKind.Blank);
    internal static string Text(WorkbookCell c)=>c.Kind switch {
        WorkbookCellKind.Text=>c.Text??"",WorkbookCellKind.Number=>c.NumberToken??"",WorkbookCellKind.Boolean=>c.Boolean==true?"True":"False",
        WorkbookCellKind.DateTime=>c.DateTime!.Value.ToString("yyyy-MM-dd HH:mm:ss",CultureInfo.InvariantCulture),
        WorkbookCellKind.Time or WorkbookCellKind.Duration=>c.Time?.ToString()??"",WorkbookCellKind.Error=>c.Error??"",_=>""};
    private static string HashFile(string path,long maximum,CancellationToken ct)
    {
        using var stream=File.OpenRead(path);if(stream.Length>maximum) throw new TrialBalanceRejected("file_capacity","Workbook exceeds its file limit.");
        using var hash=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);byte[] buffer=new byte[65536];long read=0;int length;
        while((length=stream.Read(buffer))>0)
        {ct.ThrowIfCancellationRequested();read+=length;if(read>maximum) throw new TrialBalanceRejected("file_capacity","Workbook exceeds its file limit.");hash.AppendData(buffer,0,length);}
        return Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
    }
    private static string Fingerprint(TrialBalanceInput value)=>Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(new {
        Mapping=value.Mapping.OrderBy(p=>p.Key,StringComparer.Ordinal),Code=value.CodeOverrides!.OrderBy(p=>p.Key),
        Classification=value.ClassificationOverrides!.OrderBy(p=>p.Key),Excluded=value.ExcludedExcelRows!.Order(),value.TotalExcelRow,value.NoTotalRow}))).ToLowerInvariant();
}
