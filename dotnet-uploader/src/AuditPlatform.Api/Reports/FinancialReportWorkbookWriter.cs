using System.IO.Compression;
using System.Text;
using System.Xml;

namespace AuditPlatform.Api.Reports;

internal sealed record FinancialReportWorkbookSheet(string Name,
    IReadOnlyList<IReadOnlyList<string?>> Rows);

/// <summary>Bounded OOXML export using literal text cells and no spreadsheet formulas.</summary>
internal static class FinancialReportWorkbookWriter
{
    private const string MainNs = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
    private const string RelNs = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
    private const string PackageNs = "http://schemas.openxmlformats.org/package/2006/relationships";

    public static byte[] Write(IReadOnlyList<FinancialReportWorkbookSheet> sheets,
        ReportInputOptions options, CancellationToken ct = default)
    {
        if (!options.Valid() || sheets.Count is < 1 or > 8
            || sheets.Sum(sheet => sheet.Rows.Count) > options.MaximumWorkbookRows)
            throw new InvalidDataException("Financial report workbook exceeds its configured bounds.");
        if (sheets.Select(sheet => sheet.Name).Distinct(StringComparer.Ordinal).Count() != sheets.Count
            || sheets.Any(sheet => !ValidSheetName(sheet.Name)
                || sheet.Rows.Any(row => row.Count > 16384)))
            throw new InvalidDataException("Financial report workbook has an invalid sheet layout.");

        using var output = new CappedStream(options.MaximumWorkbookBytes);
        using (var zip = new ZipArchive(output, ZipArchiveMode.Create, true, Encoding.UTF8))
        {
            WriteEntry(zip,"[Content_Types].xml",writer =>
            {
                writer.WriteStartElement("Types","http://schemas.openxmlformats.org/package/2006/content-types");
                writer.WriteStartElement("Default");writer.WriteAttributeString("Extension","rels");writer.WriteAttributeString("ContentType","application/vnd.openxmlformats-package.relationships+xml");writer.WriteEndElement();
                writer.WriteStartElement("Default");writer.WriteAttributeString("Extension","xml");writer.WriteAttributeString("ContentType","application/xml");writer.WriteEndElement();
                writer.WriteStartElement("Override");writer.WriteAttributeString("PartName","/xl/workbook.xml");writer.WriteAttributeString("ContentType","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml");writer.WriteEndElement();
                for(int i=0;i<sheets.Count;i++){writer.WriteStartElement("Override");writer.WriteAttributeString("PartName",$"/xl/worksheets/sheet{i+1}.xml");writer.WriteAttributeString("ContentType","application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml");writer.WriteEndElement();}
                writer.WriteEndElement();
            });
            WriteEntry(zip,"_rels/.rels",writer=>
            {
                writer.WriteStartElement("Relationships",PackageNs);writer.WriteStartElement("Relationship");
                writer.WriteAttributeString("Id","rId1");writer.WriteAttributeString("Type",RelNs+"/officeDocument");
                writer.WriteAttributeString("Target","xl/workbook.xml");writer.WriteEndElement();writer.WriteEndElement();
            });
            WriteEntry(zip,"xl/workbook.xml",writer=>
            {
                writer.WriteStartElement("workbook",MainNs);writer.WriteAttributeString("xmlns","r",null,RelNs);
                writer.WriteStartElement("sheets");
                for(int i=0;i<sheets.Count;i++){writer.WriteStartElement("sheet");writer.WriteAttributeString("name",sheets[i].Name);writer.WriteAttributeString("sheetId",(i+1).ToString());writer.WriteAttributeString("r","id",RelNs,$"rId{i+1}");writer.WriteEndElement();}
                writer.WriteEndElement();writer.WriteEndElement();
            });
            WriteEntry(zip,"xl/_rels/workbook.xml.rels",writer=>
            {
                writer.WriteStartElement("Relationships",PackageNs);
                for(int i=0;i<sheets.Count;i++){writer.WriteStartElement("Relationship");writer.WriteAttributeString("Id",$"rId{i+1}");writer.WriteAttributeString("Type",RelNs+"/worksheet");writer.WriteAttributeString("Target",$"worksheets/sheet{i+1}.xml");writer.WriteEndElement();}
                writer.WriteEndElement();
            });
            for(int i=0;i<sheets.Count;i++)
            {
                ct.ThrowIfCancellationRequested();var sheet=sheets[i];
                WriteEntry(zip,$"xl/worksheets/sheet{i+1}.xml",writer=>WriteSheet(writer,sheet,options,ct));
            }
        }
        return output.ToArray();
    }

    private static void WriteSheet(XmlWriter writer,FinancialReportWorkbookSheet sheet,
        ReportInputOptions options,CancellationToken ct)
    {
        writer.WriteStartElement("worksheet",MainNs);writer.WriteStartElement("sheetData");
        long cells=0;int rowNumber=0;
        foreach(var row in sheet.Rows)
        {
            ct.ThrowIfCancellationRequested();if(++rowNumber>options.MaximumWorkbookRows)throw new InvalidDataException("Financial report workbook row capacity exceeded.");
            writer.WriteStartElement("row");writer.WriteAttributeString("r",rowNumber.ToString());
            for(int column=0;column<row.Count;column++)
            {
                if(++cells>(long)options.MaximumWorkbookRows*100)throw new InvalidDataException("Financial report workbook cell capacity exceeded.");
                writer.WriteStartElement("c");writer.WriteAttributeString("r",Column(column+1)+rowNumber);writer.WriteAttributeString("t","inlineStr");
                writer.WriteStartElement("is");writer.WriteStartElement("t","http://schemas.openxmlformats.org/spreadsheetml/2006/main");
                writer.WriteAttributeString("xml","space","http://www.w3.org/XML/1998/namespace","preserve");
                writer.WriteString(row[column]??"");writer.WriteEndElement();writer.WriteEndElement();writer.WriteEndElement();
            }
            writer.WriteEndElement();
        }
        writer.WriteEndElement();writer.WriteEndElement();
    }

    private static void WriteEntry(ZipArchive zip,string name,Action<XmlWriter> body)
    {
        var entry=zip.CreateEntry(name,CompressionLevel.Fastest);entry.LastWriteTime=new DateTimeOffset(2000,1,1,0,0,0,TimeSpan.Zero);
        using var stream=entry.Open();using var writer=XmlWriter.Create(stream,new XmlWriterSettings
        {Encoding=new UTF8Encoding(false),CheckCharacters=true,CloseOutput=false});body(writer);
    }
    private static bool ValidSheetName(string name)=>name.Length is >0 and <=31
        &&name.IndexOfAny(['[',']','*','?','/','\\',':'])<0;
    private static string Column(int number){var value="";while(number>0){number--;value=(char)('A'+number%26)+value;number/=26;}return value;}
    private sealed class CappedStream(long maximum):MemoryStream
    {
        private void Check(int count){if(Length+count>maximum)throw new InvalidDataException("Financial report workbook byte capacity exceeded.");}
        public override void Write(byte[] buffer,int offset,int count){Check(count);base.Write(buffer,offset,count);}
        public override void Write(ReadOnlySpan<byte> buffer){Check(buffer.Length);base.Write(buffer);}
        public override void WriteByte(byte value){Check(1);base.WriteByte(value);}
    }
}
