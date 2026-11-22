using AuditPlatform.Api.Reports;
using AuditPlatform.Api.Workbooks;
using System.IO.Compression;
using System.Xml.Linq;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class FinancialReportWorkbookTests
{
    [Fact]
    public void WorkbookWritesAuditEvidenceAsLiteralText()
    {
        var options = new ReportInputOptions();
        var workbook = FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Report", new IReadOnlyList<string?>[]
            {
                new string?[] { "Account", "Amount" },
                new string?[] { "=1+1", "0001234567890123456789.01" }
            })
        ], options, TestContext.Current.CancellationToken);
        string path = Path.Combine(Path.GetTempPath(), "report-" + Guid.NewGuid().ToString("N") + ".xlsx");
        try
        {
            File.WriteAllBytes(path, workbook);
            var rows = WorkbookReader.Read(path, ".xlsx", "Report", ct: TestContext.Current.CancellationToken).ToArray();
            Assert.Equal("=1+1", rows[1].Cells.Single(cell => cell.Column == 1).Text);
            Assert.Equal("0001234567890123456789.01", rows[1].Cells.Single(cell => cell.Column == 2).Text);
        }
        finally { if (File.Exists(path)) File.Delete(path); }
    }

    [Fact]
    public void WorkbookRejectsInvalidSheetAndOversizedRows()
    {
        var options = new ReportInputOptions();
        Assert.Throws<InvalidDataException>(() => FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("bad/name", [])
        ], options,TestContext.Current.CancellationToken));

        options.MaximumWorkbookRows = 1;
        Assert.Throws<InvalidDataException>(() => FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Report", new IReadOnlyList<string?>[]
            { new string?[] { "header" }, new string?[] { "extra" } })
        ], options,TestContext.Current.CancellationToken));
    }

    [Fact]
    public void WorkbookWritesSheetsInRequestedOrderWithHeaderAndTotalFormatting()
    {
        byte[] workbook = FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Summary", new IReadOnlyList<string?>[]
            {
                new string?[] { "Account", "Amount" },
                new string?[] { "Cash", "10" },
                new string?[] { "Total", "10" }
            }),
            new FinancialReportWorkbookSheet("Evidence", new IReadOnlyList<string?>[]
            { new string?[] { "Source row", "Value" }, new string?[] { "1", "evidence" } })
        ], new ReportInputOptions(), TestContext.Current.CancellationToken);

        using var archive = new ZipArchive(new MemoryStream(workbook), ZipArchiveMode.Read);
        XNamespace ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
        var names = XDocument.Load(archive.GetEntry("xl/workbook.xml")!.Open())
            .Root!.Element(ns + "sheets")!.Elements(ns + "sheet").Select(x => (string)x.Attribute("name")!).ToArray();
        Assert.Equal(["Summary", "Evidence"], names);
        var sheet = XDocument.Load(archive.GetEntry("xl/worksheets/sheet1.xml")!.Open());
        var rows = sheet.Descendants(ns + "row").ToArray();
        Assert.Equal("1", (string)rows[0].Element(ns + "c")!.Attribute("s")!);
        Assert.Equal("2", (string)rows[2].Element(ns + "c")!.Attribute("s")!);
    }

    [Fact]
    public void WorkbookEnforcesConfiguredByteLimit()
    {
        var options = new ReportInputOptions { MaximumWorkbookBytes = 1_048_576 };
        var chars = new char[1_100_000];
        var random = new Random(91);
        const string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        for (var i = 0; i < chars.Length; i++) chars[i] = alphabet[random.Next(alphabet.Length)];
        string oversizedCell = new(chars);

        Assert.Throws<InvalidDataException>(() => FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Report", new IReadOnlyList<string?>[]
            { new string?[] { "Evidence" }, new string?[] { oversizedCell } })
        ], options, TestContext.Current.CancellationToken));
    }

    [Fact]
    public void WorkbookPreservesLegacyCashflowSummaryAndLedgerwiseGoldenCells()
    {
        // These rows mirror the controlled Python build_cashflow_workbook fixture for January 2026.
        // The expected report values are checked in; this test does not run or depend on Python.
        byte[] workbook = FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Cashflows Summary", new IReadOnlyList<string?>[]
            {
                new string?[] { "Category", "Jan 2026", "Total", "% of section", "Local total" },
                new string?[] { "Sales", "100", "100", "100.00%", "100 Rs" },
                new string?[] { "Total Receipts", "100", "100", "100.00%", "100 Rs" },
                new string?[] { "Supplier Payments", "25", "25", "100.00%", "25 Rs" },
                new string?[] { "Total Payments", "25", "25", "100.00%", "25 Rs" }
            }),
            new FinancialReportWorkbookSheet("Ledgerwise Summary", new IReadOnlyList<string?>[]
            {
                new string?[] { "Account Code", "Account Name/Ledger", "Ledger Total Amount (Cash and Bank)", "Total Amount as per Cashflows Summary", "Difference (if any)" },
                new string?[] { "", "Sales", "100", "100", "0" },
                new string?[] { "4000", "Receipts", "100", "", "" },
                new string?[] { "", "Supplier Payments", "25", "25", "0" },
                new string?[] { "2000", "Suppliers", "25", "", "" }
            }),
            new FinancialReportWorkbookSheet("Export Controls", new IReadOnlyList<string?>[]
            {
                new string?[] { "Control", "Value" },
                new string?[] { "Period start", "2026-01-01" },
                new string?[] { "Period end", "2026-01-31" },
                new string?[] { "Grouping", "month" },
                new string?[] { "Treasury view", "combined" },
                new string?[] { "Receipt presentation", "net" }
            })
        ], new ReportInputOptions(), TestContext.Current.CancellationToken);

        string path = Path.Combine(Path.GetTempPath(), "report-golden-" + Guid.NewGuid().ToString("N") + ".xlsx");
        try
        {
            File.WriteAllBytes(path, workbook);
            using var fileStream = File.OpenRead(path);
            using var archive = new ZipArchive(fileStream, ZipArchiveMode.Read);
            XNamespace ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
            var names = XDocument.Load(archive.GetEntry("xl/workbook.xml")!.Open())
                .Root!.Element(ns + "sheets")!.Elements(ns + "sheet").Select(x => (string)x.Attribute("name")!).ToArray();
            Assert.Equal(["Cashflows Summary", "Ledgerwise Summary", "Export Controls"], names);

            string CellValue(string sheetPath, string address)
            {
                var sheet = XDocument.Load(archive.GetEntry(sheetPath)!.Open());
                var cell = sheet.Descendants(ns + "c").Single(item => (string)item.Attribute("r")! == address);
                string? type = (string?)cell.Attribute("t");
                if (type == "inlineStr") return string.Concat(cell.Descendants(ns + "t").Select(item => item.Value));
                string? value = cell.Element(ns + "v")?.Value;
                if (type == "s" && value is not null)
                {
                    var shared = XDocument.Load(archive.GetEntry("xl/sharedStrings.xml")!.Open());
                    return string.Concat(shared.Root!.Elements(ns + "si").ElementAt(int.Parse(value))
                        .Descendants(ns + "t").Select(item => item.Value));
                }
                return value ?? string.Empty;
            }

            Assert.Equal("Sales", CellValue("xl/worksheets/sheet1.xml", "A2"));
            Assert.Equal("100", CellValue("xl/worksheets/sheet1.xml", "B2"));
            Assert.Equal("100.00%", CellValue("xl/worksheets/sheet1.xml", "D2"));
            Assert.Equal("Total Receipts", CellValue("xl/worksheets/sheet1.xml", "A3"));
            Assert.Equal("25", CellValue("xl/worksheets/sheet1.xml", "B4"));
            Assert.Equal("Total Payments", CellValue("xl/worksheets/sheet1.xml", "A5"));
            Assert.Equal("Sales", CellValue("xl/worksheets/sheet2.xml", "B2"));
            Assert.Equal("0", CellValue("xl/worksheets/sheet2.xml", "E2"));
            Assert.Equal("4000", CellValue("xl/worksheets/sheet2.xml", "A3"));
            Assert.Equal("2026-01-01", CellValue("xl/worksheets/sheet3.xml", "B2"));
            Assert.Equal("combined", CellValue("xl/worksheets/sheet3.xml", "B5"));
        }
        finally { if (File.Exists(path)) File.Delete(path); }
    }
}
