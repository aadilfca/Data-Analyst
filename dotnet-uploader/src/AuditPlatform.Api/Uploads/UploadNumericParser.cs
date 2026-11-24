using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.Uploads;

public sealed record NumericCheck(bool Valid,string? Error=null,bool NonFinite=false,
    string? Token=null,bool Negative=false,bool Zero=false);

// Checks syntax/sign/finite range only. It does no monetary arithmetic and
// never replaces the original cell with a rounded double or C# decimal.
public static class UploadNumericParser
{
    private const string Whitespace=" \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000";
    private static readonly char[] TrimCharacters=Whitespace.ToCharArray();
    private static readonly TimeSpan Deadline=TimeSpan.FromMilliseconds(100);
    public static string Trim(string value)=>value.Trim(TrimCharacters);
    private static bool Match(string text,string pattern)=>Regex.IsMatch(text,pattern,RegexOptions.CultureInvariant,Deadline);
    private static string Digits(string text)
    {
        var result=new StringBuilder(text.Length);
        foreach(var rune in text.EnumerateRunes())
            if(Rune.GetUnicodeCategory(rune)==UnicodeCategory.DecimalDigitNumber) result.Append((char)('0'+(int)Rune.GetNumericValue(rune)));
            else result.Append(rune.ToString());
        return result.ToString();
    }
    public static NumericCheck Check(WorkbookCell cell,bool ledger=false,bool balance=false)
    {
        try {return CheckCore(cell,ledger,balance);}
        catch(RegexMatchTimeoutException) {return new(false,"Enter a valid number.");}
    }
    private static NumericCheck CheckCore(WorkbookCell cell,bool ledger,bool balance)
    {
        var invalid=new NumericCheck(false,"Enter a valid number.");
        if(cell.Kind==WorkbookCellKind.Blank) return new(true);
        if(cell.Kind==WorkbookCellKind.Boolean) return ledger?invalid:new(true,Token:cell.Boolean==true?"1":"0",Zero:cell.Boolean!=true);
        if(cell.Kind is not (WorkbookCellKind.Text or WorkbookCellKind.Number)) return invalid;
        string raw=cell.Kind==WorkbookCellKind.Number?cell.NumberToken??"":cell.Text??"";
        if(raw.Length>65536) return invalid;
        string text=Digits(Trim(raw));if(text.Length==0) return new(true);
        // These separators are whitespace; numeric parsing rejects them inside values.
        // Ledger text explicitly strips before parsing; ordinary fields do not.
        if(!ledger && raw.Any(c=>c is >= '\x1c' and <= '\x1f')) return invalid;
        bool native=cell.Kind==WorkbookCellKind.Number;
        if(!ledger || native)
        {
            // Numeric parsing accepts Unicode decimal digits and single underscores
            // between digits. Ordinary numeric columns do not accept commas.
            if(!native) text=Regex.Replace(text,"(?<=[0-9])_(?=[0-9])","",RegexOptions.CultureInvariant,Deadline);
            string lower=text.ToLowerInvariant();
            if(lower is "inf" or "+inf" or "-inf" or "infinity" or "+infinity" or "-infinity" or "nan" or "+nan" or "-nan")
                return invalid with {NonFinite=true};
            if(!Match(text,@"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
                || !double.TryParse(text,NumberStyles.Float,CultureInfo.InvariantCulture,out double value)) return invalid;
            if(!double.IsFinite(value)) return invalid with {NonFinite=true};
            var parsed=Parts(text);
            if(ledger && !balance && parsed.Negative) return new(false,"Debit and credit must be finite, non-negative amounts.");
            return parsed;
        }
        text=text.ToUpperInvariant();
        if(balance)
        {
            var prefix=Regex.Match(text,@"^(DR|CR)\s*(.+)$",RegexOptions.CultureInvariant,Deadline);
            if(prefix.Success) text=Trim(prefix.Groups[2].Value)+" "+prefix.Groups[1].Value;
            var parens=Regex.Match(text,@"^\(([^()]+)\)(\s*(?:DR|CR))?$",RegexOptions.CultureInvariant,Deadline);
            if(parens.Success) text="-"+Trim(parens.Groups[1].Value)+parens.Groups[2].Value;
        }
        var match=Regex.Match(text,@"^([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?:\s*(DR|CR))?$",RegexOptions.CultureInvariant,Deadline);
        if(!match.Success || !balance && match.Groups[2].Success) return new(false,"Enter a valid number. Only balances may include DR or CR.");
        string token=match.Groups[1].Value.Replace(",","");var number=Parts(token);
        if(!balance && number.Negative) return new(false,"Debit and credit must be finite, non-negative amounts.");
        string label=match.Groups[2].Value;
        if(label=="DR" && number.Negative) return new(false,"Balance sign conflicts with the DR label.");
        if(label.Length>0) number=Parts((label=="CR"?"-":"")+token.TrimStart('+','-'));
        return number;
    }
    private static NumericCheck Parts(string token)
    {
        string mantissa=token.Split('e','E')[0].TrimStart('+','-');
        bool zero=mantissa.All(c=>c is '0' or '.');
        return new(true,Token:token,Negative:token.StartsWith('-')&&!zero,Zero:zero);
    }
}
