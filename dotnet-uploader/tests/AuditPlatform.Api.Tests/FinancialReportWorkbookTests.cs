using AuditPlatform.Api.Reports;
using AuditPlatform.Api.Workbooks;
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
}
