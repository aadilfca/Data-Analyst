using AuditPlatform.Api.Reports;
using AuditPlatform.Api.Workbooks;
using System.Diagnostics;
using System.Globalization;
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
    public void WorkbookRejectsTextBeyondExcelCellLimitAndPreservesUnicodeAtTheLimit()
    {
        var options = new ReportInputOptions();
        var maximumAscii = new string('x', 32767);
        byte[] accepted = FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Report", new IReadOnlyList<string?>[]
            { new string?[] { "Evidence" }, new string?[] { maximumAscii } })
        ], options, TestContext.Current.CancellationToken);
        Assert.NotEmpty(accepted);

        var maximumUnicode = string.Concat(Enumerable.Repeat("🧾", 32767));
        accepted = FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Report", new IReadOnlyList<string?>[]
            { new string?[] { "Evidence" }, new string?[] { maximumUnicode } })
        ], options, TestContext.Current.CancellationToken);
        Assert.NotEmpty(accepted);

        var oversized = new string('x', 32768);
        var error = Assert.Throws<InvalidDataException>(() => FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Report", new IReadOnlyList<string?>[]
            { new string?[] { "Evidence" }, new string?[] { oversized } })
        ], options, TestContext.Current.CancellationToken));
        Assert.Contains("32,767-character limit", error.Message);
    }

    [Fact]
    public void LargeSyntheticWorkbookReportsWriterTimeAndManagedAllocation()
    {
        const int dataRows = 20000;
        var rows = new IReadOnlyList<string?>[dataRows + 1];
        rows[0] = new string?[] { "Source row", "Account code", "Account", "Amount", "Voucher", "Description", "Status", "Count" };
        for (var index = 1; index <= dataRows; index++)
        {
            rows[index] = new string?[]
            {
                index.ToString(CultureInfo.InvariantCulture),
                $"AC{index % 5000:D4}",
                $"Account {index % 1000:D4}",
                (index % 997).ToString(CultureInfo.InvariantCulture),
                $"V-{index:D8}",
                $"Synthetic report evidence row {index:D8}",
                index % 7 == 0 ? "Review" : "Matched",
                (index % 12).ToString(CultureInfo.InvariantCulture)
            };
        }

        var options = new ReportInputOptions { MaximumWorkbookRows = dataRows + 1 };
        var allocatedBefore = GC.GetAllocatedBytesForCurrentThread();
        var timer = Stopwatch.StartNew();
        byte[] workbook = FinancialReportWorkbookWriter.Write([
            new FinancialReportWorkbookSheet("Report", rows)
        ], options, TestContext.Current.CancellationToken);
        timer.Stop();
        var allocated = GC.GetAllocatedBytesForCurrentThread() - allocatedBefore;

        Console.WriteLine($"Synthetic workbook: {dataRows:N0} rows, {workbook.Length:N0} bytes, " +
            $"{timer.ElapsedMilliseconds:N0} ms, {allocated:N0} managed bytes allocated on the writing thread.");
        Assert.InRange(workbook.Length, 1, options.MaximumWorkbookBytes);
    }

    [Fact]
    public void WorkbookPreservesCashflowSummaryAndLedgerwiseGoldenCells()
    {
        // Fixed rows define the expected January 2026 Cashflow workbook output.
        // Expected report values are fixed here; the test is fully self-contained.
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
