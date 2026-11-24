using System.Globalization;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.Uploads;

// Uses explicit date choices; it never guesses component
// order, rounds an amount, or changes a native Excel calendar date's timezone.
public static class UploadDateParser
{
    public static bool ValidFormat(string delimiter,string order)=>delimiter.Length<=3
        && order is "DMY" or "MDY" or "YMD";

    public static DateTime? Parse(WorkbookCell cell,string delimiter,string order)
    {
        if(!ValidFormat(delimiter,order)) throw new FormatException("Unsupported date format.");
        if(cell.Kind==WorkbookCellKind.Blank || cell.Kind==WorkbookCellKind.Text && string.IsNullOrWhiteSpace(cell.Text)) return null;
        if(cell.Kind==WorkbookCellKind.DateTime) return cell.DateTime;
        if(cell.Kind is WorkbookCellKind.Number or WorkbookCellKind.Boolean)
        {
            // Bare serials retain the workbook import origin, including in a 1904
            // workbook; date-formatted native cells were decoded by P17 already.
            double serial;
            // Boolean date inputs retain the defined 0/1 behavior; eligibility checks belong to P19.

            if(cell.Kind==WorkbookCellKind.Boolean) serial=cell.Boolean==true?1:0;
            else if(!double.TryParse(cell.NumberToken,CultureInfo.InvariantCulture,out serial) || !double.IsFinite(serial)) throw new FormatException("Invalid Excel date serial.");
            try {return new DateTime(1899,12,30).AddDays(serial);} catch(ArgumentOutOfRangeException) {throw new FormatException("Excel date serial is out of range.");}
        }
        if(cell.Kind!=WorkbookCellKind.Text) throw new FormatException("The source cell is not a calendar date.");
        string text=cell.Text!.Trim(); if(text.Length>64) throw new FormatException("Invalid calendar date.");
        string part=delimiter.Length==0?"":Regex.Escape(delimiter);
        string pattern=order=="YMD"?$"^(?<y>[0-9]{{4}}){part}(?<m>[0-9]{{1,2}}){part}(?<d>[0-9]{{1,2}})$"
            :order=="DMY"?$"^(?<d>[0-9]{{1,2}}){part}(?<m>[0-9]{{1,2}}){part}(?<y>[0-9]{{4}})$"
            :$"^(?<m>[0-9]{{1,2}}){part}(?<d>[0-9]{{1,2}}){part}(?<y>[0-9]{{4}})$";
        var match=Regex.Match(text,pattern,RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100));
        if(!match.Success) throw new FormatException("Invalid calendar date for the selected format.");
        try {return new DateTime(int.Parse(match.Groups["y"].Value,CultureInfo.InvariantCulture),
            int.Parse(match.Groups["m"].Value,CultureInfo.InvariantCulture),int.Parse(match.Groups["d"].Value,CultureInfo.InvariantCulture));}
        catch(ArgumentOutOfRangeException) {throw new FormatException("Invalid calendar date for the selected format.");}
    }
}
