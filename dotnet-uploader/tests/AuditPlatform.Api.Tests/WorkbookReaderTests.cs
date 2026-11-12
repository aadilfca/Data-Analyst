using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Workbooks;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class WorkbookGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public async ValueTask InitializeAsync()
    {
        var root=Root(); DirectoryPath=Path.Combine(root,".tools","p17-tests-"+Guid.NewGuid().ToString("N"));
        var start=new ProcessStartInfo(Path.Combine(root,"..","venv","Scripts","python.exe"))
            {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root,"tests","p17_workbook_oracle.py")); start.ArgumentList.Add(DirectoryPath);
        using var process=Process.Start(start)!;
        var output=process.StandardOutput.ReadToEndAsync(); var errors=process.StandardError.ReadToEndAsync();
        using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(60));
        try {await process.WaitForExitAsync(deadline.Token);} catch {process.Kill(entireProcessTree:true);throw;}
        Assert.True(process.ExitCode==0,await output+await errors);
        Oracle=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(DirectoryPath,"oracle.json")));
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
    private static string Root()
    {
        for(var path=new DirectoryInfo(AppContext.BaseDirectory);path is not null;path=path.Parent)
            if(File.Exists(Path.Combine(path.FullName,"AuditPlatform.slnx"))) return path.FullName;
        throw new InvalidOperationException("Solution not found");
    }
}

public sealed class WorkbookReaderTests(WorkbookGoldenFixture fixture) : IClassFixture<WorkbookGoldenFixture>
{
    private static CancellationToken Token=>TestContext.Current.CancellationToken;
    private IEnumerable<string> Workbooks()
    {
        using var manifest=JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory,"Fixtures","workbook-reader-cases.json")));
        return manifest.RootElement.GetProperty("workbooks").EnumerateArray().Select(e=>e.GetString()!).ToArray();
    }
    [Fact]
    public void SourceCellsRetainTypesPrecisionFormulaCacheErrorsAndPhysicalPositions()
    {
        foreach(var name in Workbooks())
        {
            string path=Path.Combine(fixture.DirectoryPath,name),extension=Path.GetExtension(name);
            var rows=WorkbookReader.Read(path,extension,"Data",ct:Token).ToArray();
            Assert.Equal(Enumerable.Range(1,7),rows.Select(r=>r.Number));
            var cells=rows.Single(r=>r.Number==2).Cells;
            Assert.Equal("00123",cells[0].Text); Assert.Equal(WorkbookCellKind.Text,cells[0].Kind);
            Assert.Equal("123456789012345678901234567890.1234500",cells[2].Text);
            Assert.Equal(WorkbookCellKind.Blank,cells[3].Kind); Assert.Equal("0",cells[4].NumberToken);
            Assert.True(cells[5].Boolean); Assert.Equal(new DateTime(2026,4,1),cells[6].DateTime);
            Assert.Equal(DateTimeKind.Unspecified,cells[6].DateTime!.Value.Kind);
            Assert.Equal("#REF!",cells[11].Error); Assert.Equal(WorkbookCellKind.Text,cells[12].Kind); Assert.Equal("#REF!",cells[12].Text);
            Assert.Equal("NaN",cells[13].Text);
            if(extension==".xlsx")
            {
                Assert.Equal("123456789012345678901234567890.1234500",cells[14].NumberToken);
                Assert.Equal(WorkbookFormulaState.Cached,cells[9].FormulaState); Assert.Equal("3",cells[9].NumberToken);
                Assert.Equal(WorkbookFormulaState.MissingResult,cells[10].FormulaState); Assert.Equal(WorkbookCellKind.Blank,cells[10].Kind);
                Assert.Equal("#DIV/0!",rows[2].Cells[11].Error);
            }
            else Assert.All(cells,c=>Assert.Equal(WorkbookFormulaState.Unknown,c.FormulaState));
            Assert.Equal(cells[0].Text,rows[2].Cells[0].Text); // Repeated source lines remain separate.
            Assert.Equal("Unicode اردو café",rows.Single(r=>r.Number==5).Cells[0].Text);
            Assert.Equal("Merged",rows.Single(r=>r.Number==5).Cells[1].Text);
            Assert.Equal(WorkbookCellKind.Blank,rows.Single(r=>r.Number==5).Cells[2].Kind);
            Assert.Equal("last",rows.Single(r=>r.Number==7).Cells[0].Text);
            foreach(var row in rows) Assert.All(row.Cells,c=>Assert.Equal(row.Number,c.Row));
            var expected=fixture.Oracle.RootElement.GetProperty(name);
            Assert.Equal(expected.GetProperty("sha256").GetString(),Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant());
            Assert.Equal("Blank",expected.GetProperty("legacy").GetProperty("Data")[0][13].GetProperty("kind").GetString());
        }
    }

    [Fact]
    public void IndependentSourceOracleMatchesReadableValuesAndRecordsCoercionDifferences()
    {
        foreach(var name in Workbooks())
        {
            var rows=WorkbookReader.Read(Path.Combine(fixture.DirectoryPath,name),Path.GetExtension(name),"Data",ct:Token).ToArray();
            var source=fixture.Oracle.RootElement.GetProperty(name).GetProperty("source").GetProperty("Data");
            foreach(var row in rows)
            {
                for(int col=0;col<row.Cells.Count;col++)
                {
                    var cell=row.Cells[col]; var expected=source[row.Number-1][col]; string kind=expected.GetProperty("kind").GetString()!;
                    // Date/time and elapsed representations differ by format's source
                    // API; dedicated assertions below compare their actual meaning.
                    if(col is 7 or 8 || row.Number==2 && col==14 || row.Number==3 && col==14) continue;
                    Assert.True(kind==cell.Kind.ToString(),$"{name} R{row.Number}C{col+1}: {kind} != {cell.Kind}");
                    if(kind=="Text") Assert.Equal(expected.GetProperty("value").GetString(),cell.Text);
                    if(kind=="Error") Assert.Equal(expected.GetProperty("value").GetString(),cell.Error);
                    if(kind=="Boolean") Assert.Equal(expected.GetProperty("value").GetBoolean(),cell.Boolean);
                    if(kind=="Number") Assert.Equal(double.Parse(expected.GetProperty("value").GetString()!,CultureInfo.InvariantCulture),double.Parse(cell.NumberToken!,CultureInfo.InvariantCulture));
                    if(kind=="DateTime") Assert.Equal(DateTime.Parse(expected.GetProperty("value").GetString()!,CultureInfo.InvariantCulture),cell.DateTime);
                }
            }
            var time=rows[1].Cells[7]; var duration=rows[1].Cells[8];
            Assert.True(time.Time.HasValue,$"{name}: {JsonSerializer.Serialize(time)}");
            Assert.True(duration.Time.HasValue,$"{name}: {JsonSerializer.Serialize(duration)}");
            Assert.Equal(12*3600+34*60+56,time.Time!.Value.TotalSeconds,3);
            Assert.Equal(49*3600,duration.Time!.Value.TotalSeconds,3);
        }
    }

    [Fact]
    public void EpochBoundariesAndBrowserDifferencesAreExplicit()
    {
        foreach(var name in Workbooks())
        {
            var observed=fixture.Oracle.RootElement.GetProperty(name);
            var rows=WorkbookReader.Read(Path.Combine(fixture.DirectoryPath,name),Path.GetExtension(name),"Epoch",ct:Token).ToArray();
            Assert.Equal(Enumerable.Range(1,6),rows.Select(r=>r.Number));
            Assert.Equal(WorkbookCellKind.Time,rows[1].Cells[0].Kind); Assert.Equal(TimeSpan.Zero,rows[1].Cells[0].Time);
            var source=observed.GetProperty("source").GetProperty("Epoch");
            for(int index=2;index<6;index++)
                Assert.Equal(DateTime.Parse(source[index][0].GetProperty("value").GetString()!,CultureInfo.InvariantCulture),rows[index].Cells[0].DateTime);
            var browser=observed.GetProperty("browser");
            Assert.Equal(name.Contains("1904"),browser.GetProperty("date1904").GetBoolean());
            Assert.Equal("Text",browser.GetProperty("sheets").GetProperty("Data")[1][13].GetProperty("kind").GetString());
            Assert.Equal("Blank",observed.GetProperty("legacy").GetProperty("Data")[0][13].GetProperty("kind").GetString());
            if(name=="golden_1900.xlsx")
            {
                var preview=browser.GetProperty("sheets").GetProperty("Epoch")[4][0].GetProperty("datePreview");
                Assert.Equal(1900,preview[0].GetInt32()); Assert.Equal(2,preview[1].GetInt32()); Assert.Equal(29,preview[2].GetInt32());
                Assert.Equal(new DateTime(1900,2,28),rows[4].Cells[0].DateTime);
            }
        }
    }

    [Fact]
    public void RawNumericLexemesSurviveFloatCoercionAndStyledTrailingRowsRemainPositional()
    {
        using var manifest=JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory,"Fixtures","workbook-reader-cases.json")));
        var expected=manifest.RootElement.GetProperty("precisionTokens").EnumerateArray().Select(e=>e.GetString()).ToArray();
        foreach(var name in new[]{"golden_1900.xlsx","golden_1904.xlsx"})
        {
            var rows=WorkbookReader.Read(Path.Combine(fixture.DirectoryPath,name),".xlsx","Precision",ct:Token).Skip(1).ToArray();
            Assert.Equal(expected,rows.Select(r=>r.Cells[0].NumberToken));
            var browser=fixture.Oracle.RootElement.GetProperty(name).GetProperty("browser").GetProperty("sheets").GetProperty("Precision");
            Assert.Equal("9007199254740992",browser[1][0].GetProperty("value").GetString());
            var source=fixture.Oracle.RootElement.GetProperty(name).GetProperty("source").GetProperty("Precision");
            Assert.Equal("9007199254740993",source[1][0].GetProperty("value").GetString());
            var trailing=WorkbookReader.Read(Path.Combine(fixture.DirectoryPath,name),".xlsx","Trailing",ct:Token).ToArray();
            Assert.Equal(10,trailing.Last().Number);
            Assert.All(trailing.Skip(1),r=>Assert.All(r.Cells,c=>Assert.Equal(WorkbookCellKind.Blank,c.Kind)));
        }
    }

    [Fact]
    public void FormulaCacheKindsAndInvalidNumericCellsDoNotBecomeOrdinaryBlanks()
    {
        string path=Path.Combine(fixture.DirectoryPath,"golden_1900.xlsx");
        var rows=WorkbookReader.Read(path,".xlsx","Formulas",ct:Token).ToArray();
        var cells=rows[1].Cells;
        Assert.Equal("3",cells[0].NumberToken); Assert.Equal("cached text",cells[1].Text); Assert.True(cells[2].Boolean);
        Assert.Equal(WorkbookCellKind.Text,cells[3].Kind); Assert.Equal("",cells[3].Text);
        Assert.Equal("#DIV/0!",cells[4].Error); Assert.Equal(WorkbookFormulaState.MissingResult,cells[5].FormulaState);
        Assert.Equal(WorkbookFormulaState.Cached,cells[6].FormulaState); Assert.Equal(WorkbookFormulaState.Cached,rows[2].Cells[6].FormulaState);
        Assert.All(cells.Take(5),cell=>Assert.Equal(WorkbookFormulaState.Cached,cell.FormulaState));
        var invalid=WorkbookReader.Read(path,".xlsx","InvalidNumbers",ct:Token).Skip(1).ToArray();
        Assert.Equal(3,invalid.Length);
        Assert.All(invalid,row=>{Assert.Equal(WorkbookCellKind.Error,row.Cells[0].Kind);Assert.Equal("invalid_number",row.Cells[0].Error);});
        Assert.Equal("OverflowError",fixture.Oracle.RootElement.GetProperty("golden_1900.xlsx").GetProperty("legacyFailures").GetProperty("InvalidNumbers").GetString());
    }

    [Fact]
    public void ResourceBoundsAndAmbiguousArchiveEntriesAreRejected()
    {
        string path=Path.Combine(fixture.DirectoryPath,"golden_1900.xlsx");
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(path,".xlsx","Data",new(MaximumFileBytes:10),Token).ToArray());
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(path,".xlsx","Data",new(MaximumSheets:1),Token).ToArray());
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(path,".xlsx","Data",new(MaximumCellCharacters:4),Token).ToArray());
        // A renamed ZIP still receives the decompression budget, and duplicate
        // package paths cannot let the metadata and value readers choose differently.
        string renamed=Path.Combine(fixture.DirectoryPath,"renamed.xls"); File.Copy(path,renamed);
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(renamed,".xls","Data",new(MaximumExpandedBytes:10),Token).ToArray());
        string duplicate=Path.Combine(fixture.DirectoryPath,"duplicate.xlsx"); File.Copy(path,duplicate);
        using(var archive=ZipFile.Open(duplicate,ZipArchiveMode.Update))
        { using var writer=new StreamWriter(archive.CreateEntry("xl/workbook.xml").Open());writer.Write("<workbook/>"); }
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(duplicate,".xlsx","Data",ct:Token).ToArray());
    }

    [Fact]
    public void DiscoveryLimitsCancellationAndEarlyDisposalPreserveSources()
    {
        string path=Path.Combine(fixture.DirectoryPath,"golden_1900.xlsx");
        var sheets=WorkbookReader.Discover(path,".xlsx",new(),Token);
        Assert.Equal(new[]{"Data","Epoch","Empty","Hidden","Precision","Trailing","Formulas","InvalidNumbers"},sheets.Select(s=>s.Name));
        Assert.Empty(sheets.Single(s=>s.Name=="Empty").Header);
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(path,".xlsx","Data",new(MaximumRows:2),Token).ToArray());
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(path,".xlsx","Data",new(MaximumColumns:2),Token).ToArray());
        Assert.Throws<WorkbookReadException>(()=>WorkbookReader.Read(path,".xlsx","missing",ct:Token).ToArray());
        using(var cancelled=new CancellationTokenSource())
        {cancelled.Cancel();Assert.Throws<OperationCanceledException>(()=>WorkbookReader.Read(path,".xlsx","Data",ct:cancelled.Token).ToArray());}
        using(var partial=WorkbookReader.Read(path,".xlsx","Data",ct:Token).GetEnumerator()) Assert.True(partial.MoveNext());
        using var exclusive=new FileStream(path,FileMode.Open,FileAccess.ReadWrite,FileShare.None);
        Assert.True(exclusive.Length>0);
    }
}
