using System.Globalization;
using System.Numerics;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.Uploads;

// Finite coefficient * 10^-scale. Input bounds follow the established Footing
// parser; arithmetic headroom covers their aligned sums over a bounded workbook.
// This type never changes a source cell or approves report calculation policy.
public readonly record struct UploadExactAmount : IComparable<UploadExactAmount>
{
    private readonly BigInteger coefficient;
    private readonly int scale;
    private readonly bool negativeZero;
    private UploadExactAmount(BigInteger value,int places,bool signedZero=false)
    {
        if(places is < -1000 or >1999 || value.GetBitLength()>13610) throw new FormatException("Amount exceeds exact-arithmetic limits.");
        coefficient=value;scale=places;negativeZero=value.IsZero&&signedZero;
    }
    public static UploadExactAmount Zero=>new(BigInteger.Zero,0);
    public bool IsZero=>coefficient.IsZero;
    public static UploadExactAmount Parse(string text)
    {
        text=UploadNumericParser.Trim(text);
        if(text.Length is <1 or >65536) throw new FormatException("Enter a finite numeric Footing amount.");
        Match match;
        try {match=Regex.Match(text,@"^(?<sign>[+-]?)(?:(?<whole>[0-9]+)(?:\.(?<fraction>[0-9]*))?|\.(?<fraction>[0-9]+))(?:[eE](?<exp>[+-]?[0-9]+))?$",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100));}
        catch(RegexMatchTimeoutException) {throw new FormatException("Enter a finite numeric Footing amount.");}
        if(!match.Success) throw new FormatException("Enter a finite numeric Footing amount.");
        string digits=match.Groups["whole"].Value+match.Groups["fraction"].Value;
        string significant=digits.TrimStart('0');if(significant.Length==0) significant="0";
        string exponent=match.Groups["exp"].Value;int power=0;
        if(exponent.Length>0)
        {
            bool minus=exponent[0]=='-';string magnitude=exponent.TrimStart('+','-').TrimStart('0');
            if(magnitude.Length>4 || magnitude.Length>0&&!int.TryParse(magnitude,CultureInfo.InvariantCulture,out power))
                throw new FormatException("Amount exceeds supported numeric range.");
            if(minus) power=-power;
        }
        int places=checked(match.Groups["fraction"].Value.Length-power);
        int adjusted=significant=="0"?-places:significant.Length-places-1;
        if(significant.Length>1000 || adjusted is < -1000 or >1000) throw new FormatException("Amount exceeds supported numeric range.");
        bool negative=match.Groups["sign"].Value=="-";
        var value=BigInteger.Parse(significant,CultureInfo.InvariantCulture);if(negative) value=-value;
        return new(value,places,negative);
    }
    public static UploadExactAmount FromCell(WorkbookCell cell,bool required=false)
    {
        if(cell.FormulaState==WorkbookFormulaState.MissingResult) throw new FormatException("Recalculate and save the formula in Excel.");
        if(cell.Kind==WorkbookCellKind.Blank || cell.Kind==WorkbookCellKind.Text && UploadNumericParser.Trim(cell.Text??"").Length==0)
        {if(required) throw new FormatException("A saved numeric control total is required.");return Zero;}
        if(cell.Kind is not (WorkbookCellKind.Number or WorkbookCellKind.Text)) throw new FormatException("Enter a saved numeric Footing amount.");
        return Parse(cell.Kind==WorkbookCellKind.Number?cell.NumberToken??"":cell.Text??"");
    }
    private static BigInteger Power(int exponent)
    {if(exponent is <0 or >4000) throw new FormatException("Amount exceeds exact-arithmetic limits.");return BigInteger.Pow(10,exponent);}
    public static UploadExactAmount operator +(UploadExactAmount left,UploadExactAmount right)
    {
        int places=Math.Max(left.scale,right.scale);
        return new(left.coefficient*Power(places-left.scale)+right.coefficient*Power(places-right.scale),places,
            left.negativeZero&&right.negativeZero);
    }
    public static UploadExactAmount operator -(UploadExactAmount left,UploadExactAmount right)=>left+new UploadExactAmount(-right.coefficient,right.scale,!right.negativeZero);
    public UploadExactAmount Abs()=>new(BigInteger.Abs(coefficient),scale);
    public int CompareTo(UploadExactAmount other)
    {
        int places=Math.Max(scale,other.scale);
        return (coefficient*Power(places-scale)).CompareTo(other.coefficient*Power(places-other.scale));
    }
    public string ToFixedString()
    {
        string digits=BigInteger.Abs(coefficient).ToString(CultureInfo.InvariantCulture);
        if(scale<0 && !coefficient.IsZero) digits+=new string('0',-scale);
        else if(scale>0) {if(digits.Length<=scale) digits=new string('0',scale-digits.Length+1)+digits;digits=digits.Insert(digits.Length-scale,".");}
        return coefficient.Sign<0||negativeZero?"-"+digits:digits;
    }
    // Conservative interoperability guard: never silently certify a value that
    // an older float-based reader could change, including long numeric text.
    public bool HasLegacyConversionRisk(WorkbookCell cell)
    {
        string? token=cell.Kind==WorkbookCellKind.Number?cell.NumberToken:cell.Text;
        if(!double.TryParse(token,NumberStyles.Float,CultureInfo.InvariantCulture,out var value)||!double.IsFinite(value)) return true;
        try {return CompareTo(Parse(value.ToString("R",CultureInfo.InvariantCulture)))!=0;} catch(FormatException) {return true;}
    }
}
