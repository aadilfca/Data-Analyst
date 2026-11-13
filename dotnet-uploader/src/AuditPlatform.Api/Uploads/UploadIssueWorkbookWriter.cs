using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml;
using AuditPlatform.Api.Configuration;
namespace AuditPlatform.Api.Uploads;

// Small OOXML writer over the framework ZIP/XML libraries. No package install,
// formulas, external links, transaction mirrors or silently truncated vouchers.
public static class UploadIssueWorkbookWriter
{
    private const string Spreadsheet="http://schemas.openxmlformats.org/spreadsheetml/2006/main";
    private sealed record Cell(string? Value,int Style=3,string Type="inlineStr");
    public static byte[] Write(ReceiptWorkbookData data,UploadImportOptions options,CancellationToken ct=default)
    {
        if(data.Previous.Count+data.Incoming.Count>options.MaximumWorkbookRows||data.Receipt.EvidenceStatus!="available") throw new InvalidDataException("Complete evidence exceeds the workbook limit.");
        using var output=new CappedMemory(options.MaximumWorkbookBytes);long remaining=options.MaximumWorkbookBytes*4L;
        using(var zip=new ZipArchive(output,ZipArchiveMode.Create,true,Encoding.UTF8))
        {
            void Entry(string name,Action<XmlWriter> write)
            {
                var entry=zip.CreateEntry(name,CompressionLevel.Fastest);entry.LastWriteTime=new(2000,1,1,0,0,0,TimeSpan.Zero);
                using var stream=entry.Open();using var budget=new BudgetStream(stream,n=>{remaining-=n;if(remaining<0) throw new InvalidDataException("Workbook expansion limit exceeded.");});
                using var writer=XmlWriter.Create(budget,new XmlWriterSettings{Encoding=new UTF8Encoding(false),CloseOutput=false,CheckCharacters=true});
                try {write(writer);}catch(ArgumentException error) {throw new InvalidDataException("Evidence contains unsupported XML text.",error);}
            }
            const string rel="http://schemas.openxmlformats.org/package/2006/relationships";
            Entry("[Content_Types].xml",w=>
            {
                w.WriteStartElement("Types","http://schemas.openxmlformats.org/package/2006/content-types");
                void Type(string element,string key,string value,string type) {w.WriteStartElement(element);w.WriteAttributeString(key,value);w.WriteAttributeString("ContentType",type);w.WriteEndElement();}
                Type("Default","Extension","rels","application/vnd.openxmlformats-package.relationships+xml");Type("Default","Extension","xml","application/xml");
                Type("Override","PartName","/xl/workbook.xml","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml");
                Type("Override","PartName","/xl/styles.xml","application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml");
                for(int i=1;i<=5;i++) Type("Override","PartName",$"/xl/worksheets/sheet{i}.xml","application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml");w.WriteEndElement();
            });
            Entry("_rels/.rels",w=>{w.WriteStartElement("Relationships",rel);Relationship(w,"rId1","officeDocument","xl/workbook.xml");w.WriteEndElement();});
            string[] names=["Summary","Reconciliation","Issues","Previous rows","Stored rows"];
            Entry("xl/workbook.xml",w=>
            {
                w.WriteStartElement("workbook",Spreadsheet);w.WriteStartElement("sheets");
                for(int i=0;i<names.Length;i++) {w.WriteStartElement("sheet");w.WriteAttributeString("name",names[i]);w.WriteAttributeString("sheetId",(i+1).ToString(CultureInfo.InvariantCulture));w.WriteAttributeString("r","id","http://schemas.openxmlformats.org/officeDocument/2006/relationships","rId"+(i+1));w.WriteEndElement();}
                w.WriteEndElement();w.WriteEndElement();
            });
            Entry("xl/_rels/workbook.xml.rels",w=>
            {
                w.WriteStartElement("Relationships",rel);for(int i=1;i<=5;i++) Relationship(w,"rId"+i,"worksheet",$"worksheets/sheet{i}.xml");Relationship(w,"rId6","styles","styles.xml");w.WriteEndElement();
            });
            Entry("xl/styles.xml",Styles);
            var receipt=data.Receipt;
            List<Cell[]> summary=[[new("Receipt ID"),new(receipt.Id.ToString())],[new("Outcome"),new(receipt.Kind)],[new("Completed at (UTC)"),new(receipt.CompletedAt.ToUniversalTime().ToString("O",CultureInfo.InvariantCulture))],
                [new("Comparison scope"),new(receipt.Source.CompletePeriod?"Complete declared period":"Partial extract — not full-period certification")],
                [new("Period start"),new(receipt.Source.Start??"Not declared")],[new("Period end"),new(receipt.Source.End??"Not declared")],
                [new("Added / changed / removed"),new($"{receipt.Counts.Added} / {receipt.Counts.Changed} / {receipt.Counts.Removed}")],
                [new("Full-source / approved rows"),new($"{receipt.Source.FullSheet.RowCount} / {receipt.Source.ApprovedDetail.RowCount}")],
                [new("Control result"),new(receipt.Reconciliation.Status)],[new("Meaning"),new(receipt.Reconciliation.Message)],
                [new("Audit approval"),new("Technical reconciliation only; this is not manager-reviewed audit approval.")]];
            Entry("xl/worksheets/sheet1.xml",w=>Sheet(w,"Import completion receipt",receipt,["Item","Value"],summary,ct));
            var reconciliation=receipt.Reconciliation.Metrics.Select(m=>new[]{new Cell(m.Label),Numeric(m.Source),Numeric(m.Stored),Numeric(m.Difference),new Cell(m.Matches is null?"Not certified":m.Matches.Value?"Matched":"Mismatch")}).ToArray();
            Entry("xl/worksheets/sheet2.xml",w=>Sheet(w,"Stored controls at completion",receipt,["Metric","Approved source","Stored","Difference","Result"],reconciliation,ct));
            List<Cell[]> issues=[[new("Reconciliation"),new(receipt.Reconciliation.Status),new(receipt.Reconciliation.Message)]];
            foreach(var metric in receipt.Reconciliation.Metrics.Where(m=>m.Matches==false)) issues.Add([new(metric.Label),new("Mismatch"),new($"Source: {metric.Source}; stored: {metric.Stored}; difference: {metric.Difference}")]);
            foreach(var footer in receipt.Source.FooterChecks.Where(c=>c.Status!="matched")) issues.Add([new(footer.Column),new(footer.Status),new($"Excel total: {footer.ExcelTotal}; full detail: {footer.DetailTotal}; difference: {footer.Difference}")]);
            Entry("xl/worksheets/sheet3.xml",w=>Sheet(w,"Import issues and qualifications",receipt,["Check","Status","Detail"],issues,ct));
            var columns=receipt.Columns.OrderBy(c=>c.Order).ToArray();string[] headers=["Document group","Line sequence","Stored / validated date",..columns.Select(c=>c.Name)];
            IEnumerable<Cell[]> Evidence(IReadOnlyList<ReceiptEvidenceRow> rows)
            {
                int sequence=0;
                foreach(var row in rows)
                {
                    ct.ThrowIfCancellationRequested();using var raw=JsonDocument.Parse(row.Raw);var cells=new List<Cell>{new(row.Group),Numeric((++sequence).ToString(CultureInfo.InvariantCulture)),row.Date is null?new(null):new(row.Date+"T00:00:00",5,"d")};
                    foreach(var column in columns)
                    {
                        if(!raw.RootElement.TryGetProperty(column.Key,out var value)||value.ValueKind==JsonValueKind.Null) cells.Add(new(null));
                        else if(value.ValueKind==JsonValueKind.String) cells.Add(new(value.GetString()));
                        else if(value.ValueKind==JsonValueKind.Number) cells.Add(Numeric(value.GetRawText()));
                        else if(value.ValueKind is JsonValueKind.True or JsonValueKind.False) cells.Add(new(value.GetBoolean()?"TRUE":"FALSE"));
                        else throw new InvalidDataException("Unsupported evidence scalar.");
                    }
                    yield return cells.ToArray();
                }
            }
            Entry("xl/worksheets/sheet4.xml",w=>Sheet(w,"Previous stored voucher context",receipt,headers,Evidence(data.Previous),ct));
            Entry("xl/worksheets/sheet5.xml",w=>Sheet(w,"Stored at completion — selected incoming lines",receipt,headers,Evidence(data.Incoming),ct));
        }
        return output.ToArray();
    }
    private static Cell Numeric(string? text)
    {
        // Excel numeric cells have limited significant precision. Preserve larger
        // values as exact literal text rather than allowing spreadsheet rounding.
        if(text is null) return new(null);
        int significant=text.Split('e','E')[0].Count(char.IsDigit);
        return significant<=15&&double.TryParse(text,NumberStyles.Float,CultureInfo.InvariantCulture,out double n)&&double.IsFinite(n)
            &&(Math.Abs(n)>=1e-307||n==0&&!text.Split('e','E')[0].Any(c=>c is >= '1' and <= '9'))
            ?new(text,4,"n"):new(text);
    }
    private static void Relationship(XmlWriter w,string id,string type,string target)
    {w.WriteStartElement("Relationship");w.WriteAttributeString("Id",id);w.WriteAttributeString("Type","http://schemas.openxmlformats.org/officeDocument/2006/relationships/"+type);w.WriteAttributeString("Target",target);w.WriteEndElement();}
    private static string Column(int number)
    {string result="";while(number>0) {number--;result=(char)('A'+number%26)+result;number/=26;}return result;}
    private static void Sheet(XmlWriter w,string title,UploadImportReceipt receipt,string[] headers,IEnumerable<Cell[]> rows,CancellationToken ct)
    {
        w.WriteStartElement("worksheet",Spreadsheet);
        w.WriteStartElement("sheetViews");w.WriteStartElement("sheetView");w.WriteAttributeString("workbookViewId","0");w.WriteAttributeString("showGridLines","0");
        w.WriteStartElement("pane");w.WriteAttributeString("ySplit","5");w.WriteAttributeString("topLeftCell","A6");w.WriteAttributeString("state","frozen");w.WriteEndElement();w.WriteEndElement();w.WriteEndElement();
        w.WriteStartElement("cols");for(int i=1;i<=headers.Length;i++) {w.WriteStartElement("col");w.WriteAttributeString("min",i.ToString());w.WriteAttributeString("max",i.ToString());w.WriteAttributeString("width",i==1?"30":"24");w.WriteAttributeString("customWidth","1");w.WriteEndElement();}w.WriteEndElement();
        w.WriteStartElement("sheetData");int rowNumber=0;
        void Row(Cell[] cells)
        {
            ct.ThrowIfCancellationRequested();rowNumber++;w.WriteStartElement("row");w.WriteAttributeString("r",rowNumber.ToString(CultureInfo.InvariantCulture));
            for(int i=0;i<cells.Length;i++)
            {
                var cell=cells[i];if(cell.Value is null) continue;if(cell.Value.Length>32767) throw new InvalidDataException("An evidence cell exceeds Excel's limit.");
                w.WriteStartElement("c");w.WriteAttributeString("r",Column(i+1)+rowNumber);w.WriteAttributeString("s",cell.Style.ToString(CultureInfo.InvariantCulture));w.WriteAttributeString("t",cell.Type);
                if(cell.Type=="inlineStr") {w.WriteStartElement("is");w.WriteStartElement("t");w.WriteAttributeString("xml","space","http://www.w3.org/XML/1998/namespace","preserve");w.WriteString(ExcelText(cell.Value));w.WriteEndElement();w.WriteEndElement();}
                else w.WriteElementString("v",cell.Value);w.WriteEndElement();
            }
            w.WriteEndElement();
        }
        Row([new(title,2)]);Row([new($"Client {receipt.ClientId} / Template {receipt.TemplateId} / File {receipt.FileId} / Sheet {receipt.Sheet}")]);
        Row([new("Source SHA-256: "+receipt.SourceHash)]);Row([]);Row(headers.Select(h=>new Cell(h,1)).ToArray());foreach(var row in rows) Row(row);w.WriteEndElement();
        w.WriteStartElement("autoFilter");w.WriteAttributeString("ref",$"A5:{Column(headers.Length)}{Math.Max(rowNumber,5)}");w.WriteEndElement();
        w.WriteStartElement("mergeCells");for(int i=1;i<=3;i++) {w.WriteStartElement("mergeCell");w.WriteAttributeString("ref",$"A{i}:{Column(headers.Length)}{i}");w.WriteEndElement();}w.WriteEndElement();
        w.WriteStartElement("pageSetup");w.WriteAttributeString("orientation","landscape");w.WriteAttributeString("paperSize","8");w.WriteAttributeString("fitToWidth","1");w.WriteAttributeString("fitToHeight","0");w.WriteEndElement();w.WriteEndElement();
    }
    // OOXML interprets _xHHHH_ as an escaped character, including in literal
    // strings. Escape that leading underscore so ERP text is not changed.
    private static string ExcelText(string text)=>text.Contains("_x",StringComparison.Ordinal)
        ?Regex.Replace(text,"_(?=x[0-9a-fA-F]{4}_)","_x005F_",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100)):text;
    private static void Styles(XmlWriter w)
    {
        w.WriteStartElement("styleSheet",Spreadsheet);
        w.WriteStartElement("numFmts");w.WriteAttributeString("count","2");foreach(var (id,format) in new[]{("164","0.############################"),("165","dd/mm/yyyy")}) {w.WriteStartElement("numFmt");w.WriteAttributeString("numFmtId",id);w.WriteAttributeString("formatCode",format);w.WriteEndElement();}w.WriteEndElement();
        w.WriteStartElement("fonts");w.WriteAttributeString("count","3");for(int i=0;i<3;i++) {w.WriteStartElement("font");w.WriteStartElement("name");w.WriteAttributeString("val","Calibri");w.WriteEndElement();w.WriteStartElement("sz");w.WriteAttributeString("val",i==2?"16":"11");w.WriteEndElement();if(i>0) w.WriteElementString("b","");w.WriteEndElement();}w.WriteEndElement();
        w.WriteStartElement("fills");w.WriteAttributeString("count","3");foreach(string pattern in new[]{"none","gray125","solid"}) {w.WriteStartElement("fill");w.WriteStartElement("patternFill");w.WriteAttributeString("patternType",pattern);if(pattern=="solid") {w.WriteStartElement("fgColor");w.WriteAttributeString("rgb","FFE2EFDA");w.WriteEndElement();}w.WriteEndElement();w.WriteEndElement();}w.WriteEndElement();
        w.WriteStartElement("borders");w.WriteAttributeString("count","1");w.WriteStartElement("border");foreach(string name in new[]{"left","right","top","bottom","diagonal"}) w.WriteElementString(name,"");w.WriteEndElement();w.WriteEndElement();
        w.WriteStartElement("cellStyleXfs");w.WriteAttributeString("count","1");w.WriteStartElement("xf");w.WriteAttributeString("numFmtId","0");w.WriteAttributeString("fontId","0");w.WriteAttributeString("fillId","0");w.WriteAttributeString("borderId","0");w.WriteEndElement();w.WriteEndElement();
        w.WriteStartElement("cellXfs");w.WriteAttributeString("count","6");for(int i=0;i<6;i++) {w.WriteStartElement("xf");w.WriteAttributeString("numFmtId",i==3?"49":i==4?"164":i==5?"165":"0");w.WriteAttributeString("fontId",i==2?"2":i==1?"1":"0");w.WriteAttributeString("fillId",i==1?"2":"0");w.WriteAttributeString("borderId","0");w.WriteAttributeString("xfId","0");w.WriteAttributeString("applyAlignment","1");w.WriteStartElement("alignment");w.WriteAttributeString("horizontal",i>=4?"right":"left");w.WriteAttributeString("vertical","top");w.WriteAttributeString("wrapText","1");w.WriteEndElement();w.WriteEndElement();}w.WriteEndElement();
        w.WriteStartElement("cellStyles");w.WriteAttributeString("count","1");w.WriteStartElement("cellStyle");w.WriteAttributeString("name","Normal");w.WriteAttributeString("xfId","0");w.WriteAttributeString("builtinId","0");w.WriteEndElement();w.WriteEndElement();w.WriteEndElement();
    }
    private sealed class CappedMemory(int maximum):MemoryStream
    {
        public override void Write(byte[] buffer,int offset,int count) {if(Math.Max(Length,Position+count)>maximum) throw new InvalidDataException("Workbook byte limit exceeded.");base.Write(buffer,offset,count);}
        public override void Write(ReadOnlySpan<byte> buffer) {if(Math.Max(Length,Position+buffer.Length)>maximum) throw new InvalidDataException("Workbook byte limit exceeded.");base.Write(buffer);}
        public override void WriteByte(byte value) {if(Math.Max(Length,Position+1)>maximum) throw new InvalidDataException("Workbook byte limit exceeded.");base.WriteByte(value);}
    }
    private sealed class BudgetStream(Stream target,Action<int> count):Stream
    {
        public override bool CanRead=>false;public override bool CanSeek=>false;public override bool CanWrite=>true;public override long Length=>throw new NotSupportedException();public override long Position {get=>throw new NotSupportedException();set=>throw new NotSupportedException();}
        public override void Flush()=>target.Flush();public override void Write(byte[] buffer,int offset,int length) {count(length);target.Write(buffer,offset,length);}
        public override void Write(ReadOnlySpan<byte> buffer) {count(buffer.Length);target.Write(buffer);}
        public override int Read(byte[] buffer,int offset,int length)=>throw new NotSupportedException();public override long Seek(long offset,SeekOrigin origin)=>throw new NotSupportedException();public override void SetLength(long value)=>throw new NotSupportedException();
    }
}
