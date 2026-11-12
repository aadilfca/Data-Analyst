using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
using ExcelDataReader;

namespace AuditPlatform.Api.Workbooks;

// Read-only, bounded source adapter. It performs no date-text interpretation,
// accounting calculations, canonical serialization or formula evaluation.
public static class WorkbookReader
{
    private sealed record SourceCell(string Type,string? Token,bool HasValue,bool Formula,string? FormulaText);
    private sealed record SourceRow(int Number,Dictionary<int,SourceCell> Cells);
    private static readonly Regex NumberSyntax=new(@"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100));

    public static IReadOnlyList<WorkbookSheet> Discover(string path,string extension,WorkbookReadLimits limits,CancellationToken ct=default)
    {
        Validate(path,extension,limits,ct);
        using var stream=File.OpenRead(path);
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        using var reader=ExcelReaderFactory.CreateReader(stream,new ExcelReaderConfiguration {SinglePassMode=true});
        if(reader.ResultsCount>limits.MaximumSheets) throw Invalid();
        var sheets=new List<WorkbookSheet>();
        do
        {
            ct.ThrowIfCancellationRequested(); if(sheets.Count>=limits.MaximumSheets) throw Invalid();
            // Read uses the same adapter as full rows. Disposal after the first
            // row closes both the value reader and supplemental XML stream.
            using var rows=ReadCurrentRows(reader,path,reader.Name,limits,ct).GetEnumerator();
            sheets.Add(new(reader.Name,rows.MoveNext()?rows.Current.Cells:[]));
        } while(reader.NextResult());
        if(sheets.Count==0 || sheets.All(s=>s.Header.Count==0)) throw Invalid();
        return sheets;
    }

    public static IEnumerable<WorkbookRow> Read(string path,string extension,string sheet,WorkbookReadLimits? limits=null,CancellationToken ct=default)
    {
        var selected=limits??new WorkbookReadLimits(); Validate(path,extension,selected,ct);
        foreach(var row in ReadValidated(path,sheet,selected,ct)) yield return row;
    }

    private static IEnumerable<WorkbookRow> ReadValidated(string path,string sheet,WorkbookReadLimits limits,CancellationToken ct)
    {
        using var stream=File.OpenRead(path);
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        using var reader=ExcelReaderFactory.CreateReader(stream,new ExcelReaderConfiguration {SinglePassMode=true});
        if(reader.ResultsCount>limits.MaximumSheets) throw Invalid();
        int sheets=0;
        while(reader.Name!=sheet)
        {
            ct.ThrowIfCancellationRequested(); if(++sheets>=limits.MaximumSheets || !reader.NextResult()) throw new WorkbookReadException("Sheet not found.");
        }
        foreach(var row in ReadCurrentRows(reader,path,sheet,limits,ct)) yield return row;
    }

    private static IEnumerable<WorkbookRow> ReadCurrentRows(IExcelDataReader reader,string path,string sheet,WorkbookReadLimits limits,CancellationToken ct)
    {
        using var metadata=OpenMetadata(path,sheet,limits,ct);
        using var source=metadata?.GetEnumerator();
        bool next=source?.MoveNext()==true;
        while(reader.Read())
        {
            ct.ThrowIfCancellationRequested(); int number=checked(reader.Depth+1);
            if(number>limits.MaximumRows || reader.FieldCount>limits.MaximumColumns) throw Invalid();
            if(next && source!.Current.Number<number) throw new WorkbookReadException("Workbook row positions disagree.");
            var raw=next && source!.Current.Number==number ? source.Current:null;
            int width=Math.Max(reader.FieldCount,raw?.Cells.Keys.DefaultIfEmpty(0).Max()??0);
            if(width>limits.MaximumColumns) throw Invalid();
            var cells=new List<WorkbookCell>(width);
            for(int index=0;index<width;index++)
            {
                ct.ThrowIfCancellationRequested(); SourceCell? original=null; raw?.Cells.TryGetValue(index+1,out original);
                var value=index<reader.FieldCount?reader.GetValue(index):null;
                var error=index<reader.FieldCount?reader.GetCellError(index):null;
                string? format=index<reader.FieldCount?reader.GetNumberFormatString(index):null;
                cells.Add(Cell(number,index+1,value,error,format,original,metadata is not null,limits));
            }
            yield return new(sheet,number,cells);
            if(raw is not null) next=source!.MoveNext();
        }
        if(next) throw new WorkbookReadException("Workbook row positions disagree.");
    }

    private static WorkbookCell Cell(int row,int column,object? value,CellError? error,string? format,SourceCell? source,bool xml,WorkbookReadLimits limits)
    {
        var formula=!xml?WorkbookFormulaState.Unknown:source?.Formula==true
            ? source.HasValue && (!string.IsNullOrEmpty(source.Token)||source.Type=="str")?WorkbookFormulaState.Cached:WorkbookFormulaState.MissingResult
            :WorkbookFormulaState.None;
        var result=new WorkbookCell(row,column,WorkbookCellKind.Blank,NumberFormat:format,FormulaState:formula,
            Formula:source?.FormulaText,SourceToken:source?.Token);
        if(source?.Type=="e" || error is not null)
            return result with {Kind=WorkbookCellKind.Error,Error=source?.Token??ErrorText(error!.Value)};
        if(source?.Formula==true && formula==WorkbookFormulaState.MissingResult) return result;
        if(source?.Type=="str" && source.HasValue && source.Token=="") return result with {Kind=WorkbookCellKind.Text,Text=""};
        if(source is {Type:"" or "n",HasValue:true} && !string.IsNullOrEmpty(source.Token) && value is not (DateTime or TimeSpan))
        {
            string token=source.Token.Trim();
            if(token.Length>256 || !NumberSyntax.IsMatch(token) || !double.TryParse(token,CultureInfo.InvariantCulture,out double finite) || !double.IsFinite(finite))
                return result with {Kind=WorkbookCellKind.Error,Error="invalid_number"};
            return result with {Kind=WorkbookCellKind.Number,NumberToken=token};
        }
        if(value is null or DBNull) return result;
        if(value is string text)
        { if(text.Length>limits.MaximumCellCharacters) throw Invalid(); return result with {Kind=WorkbookCellKind.Text,Text=text}; }
        if(value is bool boolean) return result with {Kind=WorkbookCellKind.Boolean,Boolean=boolean};
        if(value is DateTime date)
        {
            bool fraction=source is {Type:"" or "n"} && double.TryParse(source.Token,CultureInfo.InvariantCulture,out var serial) && serial>=0 && serial<1;
            bool binaryEpoch=!xml && (date.Date==new DateTime(1899,12,31) || date.Date==new DateTime(1899,12,30) || date.Date==new DateTime(1904,1,1));
            if(fraction || binaryEpoch) return result with {Kind=WorkbookCellKind.Time,Time=date.TimeOfDay};
            return result with {Kind=WorkbookCellKind.DateTime,DateTime=System.DateTime.SpecifyKind(date,DateTimeKind.Unspecified)};
        }
        if(value is TimeSpan time) return result with {Kind=time.TotalDays>=1 || time.TotalDays<0?WorkbookCellKind.Duration:WorkbookCellKind.Time,Time=time};
        if(value is double or float or int or long or decimal)
        {
            string token=source is {Type:"" or "n",HasValue:true}?source.Token??"":value is double d?d.ToString("R",CultureInfo.InvariantCulture):Convert.ToString(value,CultureInfo.InvariantCulture)!;
            if(token.Length>256 || !NumberSyntax.IsMatch(token)) return result with {Kind=WorkbookCellKind.Error,Error="invalid_number"};
            return result with {Kind=WorkbookCellKind.Number,NumberToken=token};
        }
        throw new WorkbookReadException("Unsupported workbook cell type.");
    }
    private static string ErrorText(CellError error)=>error switch {
        CellError.NULL=>"#NULL!",CellError.DIV0=>"#DIV/0!",CellError.VALUE=>"#VALUE!",CellError.REF=>"#REF!",
        CellError.NAME=>"#NAME?",CellError.NUM=>"#NUM!",CellError.NA=>"#N/A",_=>"#GETTING_DATA"};

    private static WorkbookReadException Invalid()=>new("Workbook exceeds reader limits or is invalid.");
    private static XmlReaderSettings XmlSettings(WorkbookReadLimits limits)=>new() {DtdProcessing=DtdProcessing.Prohibit,XmlResolver=null,MaxCharactersInDocument=limits.MaximumExpandedBytes,IgnoreComments=true,CloseInput=true};

    private static void Validate(string path,string extension,WorkbookReadLimits limits,CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        if(extension.ToLowerInvariant() is not (".xlsx" or ".xls") || limits.MaximumFileBytes<1 || limits.MaximumExpandedBytes<1
            || limits.MaximumSheets<1 || limits.MaximumColumns<1 || limits.MaximumRows<1 || limits.MaximumCellCharacters<1) throw Invalid();
        using var stream=File.OpenRead(path);
        if(stream.Length==0 || stream.Length>limits.MaximumFileBytes) throw Invalid();
        int first=stream.ReadByte(),second=stream.ReadByte(); stream.Position=0;
        if(extension.ToLowerInvariant()==".xlsx" || first=='P' && second=='K')
        {
            using var zip=new ZipArchive(stream,ZipArchiveMode.Read);
            if(zip.Entries.Count>10000 || zip.GetEntry("xl/workbook.xml") is null || zip.Entries.Select(e=>e.FullName).Distinct(StringComparer.Ordinal).Count()!=zip.Entries.Count) throw Invalid();
            long expanded=0; var buffer=new byte[65536];
            foreach(var entry in zip.Entries)
            {
                ct.ThrowIfCancellationRequested(); using var input=entry.Open(); int count;
                while((count=input.Read(buffer))>0)
                { expanded+=count; if(expanded>limits.MaximumExpandedBytes) throw Invalid(); ct.ThrowIfCancellationRequested(); }
            }
        }
    }

    // Supplemental XML is streamed beside the value reader. It preserves numeric
    // lexemes and formula/cache presence that a converted GetValue cannot attest.
    private sealed class Metadata(ZipArchive archive,Stream stream,XmlReader xml,WorkbookReadLimits limits,CancellationToken ct) : IEnumerable<SourceRow>,IDisposable
    {
        public IEnumerator<SourceRow> GetEnumerator()
        {
            int previous=0;
            while(xml.Read())
            {
                ct.ThrowIfCancellationRequested();
                if(xml.NodeType!=XmlNodeType.Element || xml.LocalName!="row") continue;
                if(!int.TryParse(xml.GetAttribute("r"),out int number) || number<=previous || number>limits.MaximumRows) throw Invalid();
                previous=number; var cells=new Dictionary<int,SourceCell>();
                using(var row=xml.ReadSubtree())
                {
                    while(row.Read())
                    {
                        ct.ThrowIfCancellationRequested();
                        if(row.NodeType!=XmlNodeType.Element || row.LocalName!="c") continue;
                        var address=row.GetAttribute("r")??""; int column=0,offset=0;
                        while(offset<address.Length && address[offset] is >= 'A' and <= 'Z') column=checked(column*26+address[offset++]-'A'+1);
                        if(column<1 || column>limits.MaximumColumns || !int.TryParse(address[offset..],out int cellRow) || cellRow!=number || cells.ContainsKey(column)) throw Invalid();
                        string type=row.GetAttribute("t")??""; string? token=null,formulaText=null; bool hasValue=false,formula=false;
                        using(var cell=row.ReadSubtree())
                        {
                            while(cell.Read())
                            {
                                if(cell.NodeType!=XmlNodeType.Element) continue;
                                if(cell.LocalName is "f" or "v")
                                {
                                    bool isFormula=cell.LocalName=="f";
                                    using var content=cell.ReadSubtree(); content.Read();
                                    var text=new StringBuilder(); var buffer=new char[1024];
                                    while(content.Read()) if(content.NodeType is XmlNodeType.Text or XmlNodeType.CDATA)
                                    {
                                        int count; while((count=content.ReadValueChunk(buffer,0,buffer.Length))>0)
                                        { ct.ThrowIfCancellationRequested(); if(text.Length+count>limits.MaximumCellCharacters) throw Invalid(); text.Append(buffer,0,count); }
                                    }
                                    if(isFormula) {formula=true;formulaText=text.ToString();} else {hasValue=true;token=text.ToString();}
                                }
                            }
                        }
                        cells.Add(column,new(type,token,hasValue,formula,formulaText));
                    }
                }
                yield return new(number,cells);
            }
        }
        System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator()=>GetEnumerator();
        public void Dispose() {xml.Dispose();stream.Dispose();archive.Dispose();}
    }

    private static Metadata? OpenMetadata(string path,string sheet,WorkbookReadLimits limits,CancellationToken ct)
    {
        var stream=File.OpenRead(path); int a=stream.ReadByte(),b=stream.ReadByte(); stream.Position=0;
        if(a!='P' || b!='K') {stream.Dispose();return null;}
        ZipArchive? archive=null;
        try
        {
            archive=new ZipArchive(stream,ZipArchiveMode.Read);
            XDocument Load(string name)
            { using var input=(archive.GetEntry(name)??throw Invalid()).Open(); var settings=XmlSettings(limits); settings.MaxCharactersInDocument=Math.Min(1048576,limits.MaximumExpandedBytes);
              using var reader=XmlReader.Create(input,settings); return XDocument.Load(reader); }
            var workbook=Load("xl/workbook.xml"); var relationships=Load("xl/_rels/workbook.xml.rels");
            var selected=workbook.Descendants().SingleOrDefault(e=>e.Name.LocalName=="sheet" && (string?)e.Attribute("name")==sheet)??throw Invalid();
            string relationship=selected.Attributes().Single(a=>a.Name.LocalName=="id").Value;
            var target=relationships.Descendants().Single(e=>e.Name.LocalName=="Relationship" && (string?)e.Attribute("Id")==relationship);
            if((string?)target.Attribute("TargetMode")=="External") throw Invalid();
            string value=(string?)target.Attribute("Target")??throw Invalid();
            if(value.Contains('\\') || value.Contains(':') || value.Contains('%')) throw Invalid();
            var parts=new List<string>();
            foreach(var part in (value.StartsWith('/')?value:"xl/"+value).Split('/'))
            { if(part is "" or ".") continue; if(part=="..") {if(parts.Count==0) throw Invalid();parts.RemoveAt(parts.Count-1);} else parts.Add(part); }
            var entry=archive.GetEntry(string.Join('/',parts))??throw Invalid();
            var input=entry.Open();
            return new(archive,stream,XmlReader.Create(input,XmlSettings(limits)),limits,ct);
        }
        catch {archive?.Dispose();stream.Dispose();throw;}
    }
}
