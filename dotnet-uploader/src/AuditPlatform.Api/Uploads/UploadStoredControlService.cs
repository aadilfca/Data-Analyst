using System.Globalization;
using System.Numerics;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Data;
using Npgsql;
namespace AuditPlatform.Api.Uploads;

// Independently aggregate canonical JSON, not write-side counts or approximate
// helpers. Ordinary diagnostic SELECT errors are isolated by a savepoint.
public static class UploadStoredControlService
{
    private const string Whitespace=" \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000";
    private const string NumberPattern="^[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?$";
    public static ReceiptSourceControls Capture(CoverageOutput proof,IdentityProfile profile)=>new(proof.Footing.FullSheet,proof.Footing.SelectedDetail,
        proof.Footing.Checks.Select(c=>new ReceiptControlColumn(c.ColumnId,c.StorageKey,c.Column)).ToArray(),proof.Footing.Checks,
        profile.DocumentKey,proof.CompletePeriodDeclared,proof.DeclaredPeriod?.Start,proof.DeclaredPeriod?.End,
        proof.Footing.ControlTotalRow,proof.Footing.VarianceConfirmed);

    public static async Task<ReceiptReconciliation> VerifyAsync(PostgresTransactionContext db,ReceiptSourceControls source,int client,int template,
        DateTimeOffset checkedAt,CancellationToken ct)
    {
        if(source.Start is null||source.End is null)
            return Compare(source,null,checkedAt,unavailable:source.CompletePeriod);
        await using(var savepoint=db.CreateCommand("SAVEPOINT native_upload_controls")) await savepoint.ExecuteNonQueryAsync(ct);
        try
        {
            var stored=await AggregateAsync(db,source,client,template,ct);
            var result=Compare(source,stored,checkedAt);
            await using var release=db.CreateCommand("RELEASE SAVEPOINT native_upload_controls");await release.ExecuteNonQueryAsync(ct);return result;
        }
        catch(Exception error) when(error is PostgresException or FormatException)
        {
            ct.ThrowIfCancellationRequested();
            // A broken connection or cancelled transaction cannot be certified
            // by this fallback; rollback/release errors escape to the outer owner.
            await using(var rollback=db.CreateCommand("ROLLBACK TO SAVEPOINT native_upload_controls")) await rollback.ExecuteNonQueryAsync(ct);
            await using(var release=db.CreateCommand("RELEASE SAVEPOINT native_upload_controls")) await release.ExecuteNonQueryAsync(ct);
            return Compare(source,null,checkedAt,unavailable:true);
        }
    }

    private static async Task<FootingSummary> AggregateAsync(PostgresTransactionContext db,ReceiptSourceControls source,int client,int template,CancellationToken ct)
    {
        await using var command=db.CreateCommand("");
        string Parameter(object value) {command.Parameters.AddWithValue(value);return "$"+command.Parameters.Count.ToString(CultureInfo.InvariantCulture);}
        string doc=Parameter(source.DocumentKey),spaces=Parameter(Whitespace);
        var projections=new List<string>{$"NULLIF(BTRIM(cd.row_data->>{doc},{spaces}),'') COLLATE \"C\" AS document_id"};
        for(int i=0;i<source.Footings.Count;i++) projections.Add($"NULLIF(BTRIM(cd.row_data->>{Parameter(source.Footings[i].Key)},{spaces}),'') AS amount_{i}");
        string scope=$"cd.client_id={Parameter(client)} AND cd.template_id={Parameter(template)} AND cd.is_deleted IS FALSE AND cd.change_detection_status<>'deleted' AND cd.extract_date BETWEEN {Parameter(source.Start!)}::date AND {Parameter(source.End!)}::date";
        var aggregates=new List<string>{"count(*)","count(DISTINCT document_id)","count(*) FILTER(WHERE document_id IS NULL)"};
        string pattern=Parameter(NumberPattern);
        for(int i=0;i<source.Footings.Count;i++)
        {
            aggregates.Add($"COALESCE(sum(CASE WHEN amount_{i} ~ {pattern} THEN amount_{i}::numeric ELSE 0 END),0)::text");
            aggregates.Add($"count(*) FILTER(WHERE amount_{i} IS NOT NULL AND amount_{i} !~ {pattern})");
        }
        command.CommandText=$"WITH source AS(SELECT {string.Join(',',projections)} FROM public.client_data cd WHERE {scope}) SELECT {string.Join(',',aggregates)} FROM source";
        await using var reader=await command.ExecuteReaderAsync(ct);await reader.ReadAsync(ct);
        if(reader.GetInt64(0)>100000) throw new FormatException("Stored scope exceeds the controlled comparison bound.");
        var totals=new Dictionary<int,string>();
        for(int i=0;i<source.Footings.Count;i++)
        {if(reader.GetInt64(4+i*2)>0) throw new FormatException("Stored Footing values are invalid.");totals.Add(source.Footings[i].Id,reader.GetString(3+i*2));}
        return new((int)reader.GetInt64(0),(int)reader.GetInt64(1),(int)reader.GetInt64(2),totals,true);
    }

    public static ReceiptReconciliation Compare(ReceiptSourceControls source,FootingSummary? stored,DateTimeOffset checkedAt,bool unavailable=false)
    {
        bool comparable=source.CompletePeriod&&!unavailable&&stored is not null;
        var metrics=new List<ReceiptMetric>();
        void Add(string key,string label,string? expected,string? actual)
        {
            string? difference=comparable&&expected is not null&&actual is not null?Difference(actual,expected):null;
            metrics.Add(new(key,label,expected,actual,difference,difference is null?null:Difference(difference,"0")=="0"));
        }
        Add("row_count","Total rows",source.ApprovedDetail.RowCount.ToString(CultureInfo.InvariantCulture),stored?.RowCount.ToString(CultureInfo.InvariantCulture));
        Add("document_count","Unique documents",source.ApprovedDetail.DocumentCount.ToString(CultureInfo.InvariantCulture),stored?.DocumentCount.ToString(CultureInfo.InvariantCulture));
        foreach(var column in source.Footings) Add("footing:"+column.Id.ToString(CultureInfo.InvariantCulture),"Footing — "+column.Name,
            source.ApprovedDetail.Totals.GetValueOrDefault(column.Id),stored?.Totals.GetValueOrDefault(column.Id));
        string status,message;
        if(!source.CompletePeriod&&!unavailable) {status="not_comparable";message="Partial extract: full-period reconciliation is not certified.";}
        else if(!comparable||!source.ApprovedDetail.TotalsComplete||stored?.TotalsComplete!=true) {status="unavailable";message="Stored controls are unavailable. No reconciliation is certified.";}
        else if(metrics.Any(m=>m.Matches!=true)) {status="mismatch";message="Stored data differs from the approved source controls; review is required.";}
        else if(source.ApprovedDetail.MissingDocumentRows>0||stored!.MissingDocumentRows>0) {status="incomplete";message="Document IDs are missing; completeness is not certified.";}
        else {status="matched";message=source.Footings.Count>0?"Approved controls match stored data at completion.":"Row/document counts match. Footing is not configured.";}
        return new(status,message,source.Footings.Count>0,checkedAt,metrics);
    }

    // Aggregate totals may exceed the input parser's 1,000 significant digits.
    // Align bounded integer coefficients rather than narrowing them to decimal.
    public static string Difference(string left,string right)
    {
        (BigInteger N,int Scale) Parse(string value)
        {
            if(value.Length>4096||!Regex.IsMatch(value,@"^[+-]?[0-9]+(?:\.[0-9]+)?$",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100))) throw new FormatException("Unsupported aggregate total.");
            int dot=value.IndexOf('.');return (BigInteger.Parse(value.Replace(".","",StringComparison.Ordinal),CultureInfo.InvariantCulture),dot<0?0:value.Length-dot-1);
        }
        var a=Parse(left);var b=Parse(right);int scale=Math.Max(a.Scale,b.Scale);
        var value=a.N*BigInteger.Pow(10,scale-a.Scale)-b.N*BigInteger.Pow(10,scale-b.Scale);if(value.IsZero) return "0";
        string text=BigInteger.Abs(value).ToString(CultureInfo.InvariantCulture).PadLeft(scale+1,'0');
        if(scale>0) text=text.Insert(text.Length-scale,".").TrimEnd('0').TrimEnd('.');return value.Sign<0?"-"+text:text;
    }
}
