using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.Uploads;

// Comparison-only views use stable identity serialization separate
// from JSONB/raw guards; no source amount is rounded or converted to decimal.
public static class UploadIdentityValue
{
    private static readonly TimeSpan Deadline=TimeSpan.FromMilliseconds(100);
    public static IdentityAtom FromCell(WorkbookCell cell,string type)
    {
        if(UploadInputValidationService.Blank(cell)) return new("blank");
        return cell.Kind switch {
            WorkbookCellKind.Text=>new("text",cell.Text),
            WorkbookCellKind.Number when type=="Numeric"=>new("number",cell.NumberToken),
            WorkbookCellKind.Boolean=>new("boolean",cell.Boolean==true?"True":"False"),
            WorkbookCellKind.DateTime=>new("text",cell.DateTime!.Value.ToString("dd/MM/yyyy",CultureInfo.InvariantCulture)),
            _=>throw new IdentityReview("source_coercion_review_required")};
    }
    public static IReadOnlyDictionary<string,IdentityAtom> FromJson(string json)
    {
        using var document=JsonDocument.Parse(json);
        if(document.RootElement.ValueKind!=JsonValueKind.Object) throw new IdentityReview("stored_values_review_required");
        var result=new Dictionary<string,IdentityAtom>(StringComparer.Ordinal);
        foreach(var property in document.RootElement.EnumerateObject())
        {
            if(!result.TryAdd(property.Name,property.Value.ValueKind switch {
                JsonValueKind.Null=>new("blank"),JsonValueKind.String=>new("text",property.Value.GetString()),
                JsonValueKind.Number=>new("number",property.Value.GetRawText()),JsonValueKind.True=>new("boolean","True"),
                JsonValueKind.False=>new("boolean","False"),_=>throw new IdentityReview("stored_values_review_required")}))
                throw new IdentityReview("stored_values_review_required");
        }
        return result;
    }
    private sealed record Number(string Digits,int Power,bool Negative)
    {
        public string Equality=>Digits=="0"?"0":(Negative?"-":"")+Digits+"e"+Power.ToString(CultureInfo.InvariantCulture);
        public string Normalized()
        {
            // normalize() in the existing identity helper can round under its
            // Decimal context. Review such cases; never hash narrowed digits.
            if(Digits.Length>28) throw new IdentityReview("identity_precision_review_required");
            if(Digits=="0") return "0";
            int adjusted=Digits.Length+Power-1;string value;
            if(Power<=0 && adjusted>=-6)
            {
                int split=Digits.Length+Power;
                value=split>0?Digits.Insert(split,Power==0?"":"."):"0."+new string('0',-split)+Digits;
            }
            else value=Digits[0]+(Digits.Length>1?"."+Digits[1..]:"")+"E"+(adjusted>=0?"+":"")+adjusted.ToString(CultureInfo.InvariantCulture);
            return (Negative?"-":"")+value;
        }
    }
    private static Number Numeric(IdentityAtom value)
    {
        if(value.Kind=="boolean") throw new IdentityReview("numeric_boolean_review_required");
        string raw=UploadNumericParser.Trim(value.Text??"");
        var text=new StringBuilder();
        foreach(var rune in raw.EnumerateRunes())
            text.Append(Rune.GetUnicodeCategory(rune)==UnicodeCategory.DecimalDigitNumber?((char)('0'+(int)Rune.GetNumericValue(rune))).ToString():rune.ToString());
        string token=text.ToString();
        token=token.Replace("_","",StringComparison.Ordinal);
        var match=Regex.Match(token,@"^(?<s>[+-]?)(?:(?<w>[0-9]+)(?:\.(?<f>[0-9]*))?|\.(?<f>[0-9]+))(?:[eE](?<e>[+-]?[0-9]+))?$",RegexOptions.CultureInvariant,Deadline);
        if(!match.Success) throw new IdentityReview("numeric_comparison_review_required");
        int exponent=0;
        if(match.Groups["e"].Success && (!int.TryParse(match.Groups["e"].Value,CultureInfo.InvariantCulture,out exponent)||exponent is <-100000 or >100000))
            throw new IdentityReview("identity_precision_review_required");
        string digits=(match.Groups["w"].Value+match.Groups["f"].Value).TrimStart('0');
        if(digits.Length==0) return new("0",0,false);
        int power=exponent-match.Groups["f"].Value.Length;
        int count=digits.Length;digits=digits.TrimEnd('0');power+=count-digits.Length;
        return new(digits,power,match.Groups["s"].Value=="-");
    }
    private static Number? LegacyNumeric(IdentityAtom value)
    {
        try {return Numeric(value);}
        catch(IdentityReview review) when(review.Code is "numeric_boolean_review_required" or "numeric_comparison_review_required") {return null;}
    }
    public static bool Blank(IdentityAtom value)=>value.Kind=="blank"||value.Kind=="text"&&UploadNumericParser.Trim(value.Text??"").Length==0;
    public static string Raw(IdentityAtom value)=>value.Kind=="blank"?"":UploadNumericParser.Trim(value.Text??"");
    public static string RawComparison(IdentityAtom value)=>FalseLike(value)?"":Raw(value);
    private static bool FalseLike(IdentityAtom value)=>Blank(value)||value.Kind=="boolean"&&value.Text=="False"
        ||value.Kind=="number"&&Numeric(value).Digits=="0";
    private static int CompareCanonicalKeys(string left,string right)
    {
        var a=left.EnumerateRunes().GetEnumerator();var b=right.EnumerateRunes().GetEnumerator();
        while(true) {bool x=a.MoveNext(),y=b.MoveNext();if(!x||!y) return x.CompareTo(y);int compared=a.Current.Value.CompareTo(b.Current.Value);if(compared!=0) return compared;}
    }
    private static string Json(object? value)
    {
        var result=new StringBuilder();
        void Append(string text) {if(result.Length+text.Length>1048576) throw new IdentityReview("comparison_capacity_review_required");result.Append(text);}
        void Write(object? item)
        {
            if(item is null) {Append("null");return;}
            if(item is string text)
            {
                Append("\"");foreach(char c in text) Append(c switch {'"'=>"\\\"",'\\'=>"\\\\",'\b'=>"\\b",'\f'=>"\\f",'\n'=>"\\n",'\r'=>"\\r",'\t'=>"\\t",
                    <' ' or >= '\x7f'=>"\\u"+((int)c).ToString("x4",CultureInfo.InvariantCulture),_=>c.ToString()});
                Append("\"");return;
            }
            if(item is not IEnumerable<object?> array) throw new InvalidOperationException("Identity codec accepts strings/arrays only.");
            Append("[");bool first=true;foreach(var child in array) {if(!first) Append(",");Write(child);first=false;}Append("]");
        }
        Write(value);return result.ToString();
    }
    public static string Hash(string text)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    public static string Group(IdentityProfile profile,IReadOnlyDictionary<string,IdentityAtom> row)
    {
        var value=row.GetValueOrDefault(profile.DocumentKey,new("blank"));
        if(Blank(value)) throw new IdentityReview("blank_document_identity_review_required");
        if(value.Kind=="number") throw new IdentityReview("identifier_coercion_review_required");
        return profile.TemplateId.ToString(CultureInfo.InvariantCulture)+(profile.AccountCode is null?"-":"-ledger:"+profile.AccountCode+":")+Raw(value);
    }
    private static string Line(IdentityAtom atom)
    {
        string value=Raw(atom);
        if(value.Length==0) throw new IdentityReview("line_identifier_required");
        if(value.Any(c=>c>127)) throw new IdentityReview("line_casefold_review_required");
        return value.ToLowerInvariant();
    }
    public static string SourceIdentity(IdentityProfile profile,IReadOnlyDictionary<string,IdentityAtom> row,int occurrence=1)
    {
        string key=Group(profile,row);
        if(profile.LineKey is not null) key+="|line:"+Line(row.GetValueOrDefault(profile.LineKey,new("blank")));
        else
        {
            key+="|fingerprint:v2:"+Hash(SourceFingerprint(profile,row));
            if(occurrence>1) key+="|occurrence:"+occurrence.ToString(CultureInfo.InvariantCulture);
        }
        return Hash(key);
    }
    public static string SourceFingerprint(IdentityProfile profile,IReadOnlyDictionary<string,IdentityAtom> row,bool includedOnly=false)
    {
        var values=new Dictionary<string,object?>(StringComparer.Ordinal);
        foreach(var field in row) if(!FalseLike(field.Value)) values[field.Key]=Raw(field.Value);
        foreach(var column in profile.Columns.Where(c=>c.Type=="Numeric"))
        {
            var value=row.GetValueOrDefault(column.Key,new("blank"));
            values[column.Key]=Blank(value)?new object?[]{"numeric_blank"}:LegacyNumeric(value) is {} number
                ?new object?[]{"numeric",number.Normalized()}:new object?[]{"invalid_numeric",Raw(value)};
        }
        var fields=values.AsEnumerable();
        if(includedOnly) {var included=profile.Columns.Where(c=>c.Compare).Select(c=>c.Key).ToHashSet(StringComparer.Ordinal);fields=fields.Where(v=>included.Contains(v.Key));}
        return Json(fields.OrderBy(v=>v.Key,Comparer<string>.Create(CompareCanonicalKeys)).Select(v=>(object?)new object?[]{v.Key,v.Value}));
    }
    public static string Fingerprint(IdentityProfile profile,IReadOnlyDictionary<string,IdentityAtom> row,string? primary,
        string delimiter,string order,bool stored=false,bool equivalent=false)
    {
        var values=ComparisonValues(profile,row,primary,delimiter,order,stored,equivalent);
        object? fields=values.OrderBy(v=>v.Key,Comparer<string>.Create(CompareCanonicalKeys)).Select(v=>(object?)new object?[]{v.Key,v.Value});
        return Json(equivalent?fields:new object?[]{fields,primary});
    }
    public static IReadOnlyDictionary<string,string> ComparedFields(IdentityProfile profile,IReadOnlyDictionary<string,IdentityAtom> row,
        string? primary,string delimiter,string order,bool stored=false)=>ComparisonValues(profile,row,primary,delimiter,order,stored,true)
            .ToDictionary(v=>v.Key,v=>Json(v.Value),StringComparer.Ordinal);
    public static string FieldKey(IEnumerable<KeyValuePair<string,string>> fields)=>Json(fields.OrderBy(v=>v.Key,Comparer<string>.Create(CompareCanonicalKeys))
        .Select(v=>(object?)new object?[]{v.Key,v.Value}));
    public static bool Movement(IdentityAtom value)
    {
        if(Blank(value)) return false;
        return Numeric(value).Digits!="0";
    }
    public static string LineKey(IdentityAtom value)=>Line(value);
    private static Dictionary<string,object?> ComparisonValues(IdentityProfile profile,IReadOnlyDictionary<string,IdentityAtom> row,
        string? primary,string delimiter,string order,bool stored,bool equivalent)
    {
        var values=new Dictionary<string,object?>(StringComparer.Ordinal);
        foreach(var column in profile.Columns.Where(c=>c.Compare))
        {
            var atom=row.GetValueOrDefault(column.Key,new("blank"));
            if(column.Type=="Numeric") {if(!Blank(atom)) values[column.Key]=LegacyNumeric(atom) is {} number
                ?new object?[]{"numeric",number.Equality}:new object?[]{"invalid_numeric",Raw(atom)};continue;}
            if(FalseLike(atom)) continue;
            if(atom.Kind=="number") throw new IdentityReview("text_coercion_review_required");
            string value=Raw(atom);
            if(equivalent&&column.Type=="Date")
            {
                string? day=column.Key==profile.DateKey?primary:null;
                day??=Calendar(value,delimiter,order,stored);
                if(day is not null) {values[column.Key]=new object?[]{"calendar_date",day};continue;}
            }
            if(equivalent&&column.Description) value=Regex.Replace(value.Replace("\r\n","\n",StringComparison.Ordinal).Replace('\r','\n'),"[ \\t]+"," ",RegexOptions.CultureInvariant,Deadline).Trim();
            values[column.Key]=value;
        }
        // Ledger notes are data, not positions, and are always compared.
        if(row.TryGetValue("Ledger Source Notes",out var notes)&&!Blank(notes)) values["Ledger Source Notes"]=Raw(notes);
        return values;
    }
    public static string? Calendar(string value,string delimiter,string order,bool stored=false)
    {
        if(!stored) {try {return UploadDateParser.Parse(new(0,0,WorkbookCellKind.Text,Text:value),delimiter,order)?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture);} catch(FormatException) {return null;}}
        var days=new HashSet<string>();
        foreach(string separator in new[]{"/","-",""}) foreach(string components in new[]{"DMY","MDY","YMD"})
        {var day=Calendar(value,separator,components);if(day is not null) days.Add(day);}
        return days.Count==1?days.Single():null;
    }
}
