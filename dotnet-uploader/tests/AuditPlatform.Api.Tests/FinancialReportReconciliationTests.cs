using AuditPlatform.Api.Cashflows;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Reports;
using AuditPlatform.Api.Workbooks;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class FinancialReportReconciliationTests
{
    [Fact]
    public void MissingIndependentLedgerEvidenceRemainsUnavailable()
    {
        var snapshot = Snapshot();
        var cashflows = new CashflowCalculationResult(7,"fingerprint","month","combined","gross",
            [new CashflowPeriod("2026-01","2026-01-01","2026-01-31","Jan-26")],
            [new CashflowPeriodTotal("2026-01","100","0","100")],[],[],[],[],"100","0",[]);

        var result = new FinancialReportReconciliationService().Calculate(snapshot,cashflows,
            TestContext.Current.CancellationToken);

        Assert.Equal("Unavailable",result.Status);
        Assert.Equal("Unavailable",Assert.Single(Assert.Single(result.Accounts).Periods).Status);
        Assert.Contains(result.Diagnostics,message=>message.Contains("No imported independent ledger",StringComparison.Ordinal));
    }

    [Fact]
    public void IncompleteIndependentEvidenceSnapshotIsRejected()
    {
        var snapshot=Snapshot() with
        { CashflowLedgerEvidenceRows=new ReportInputDataset<ReportCashflowLedgerEvidenceRow>(true,false,0,0,[]) };
        var cashflows=new CashflowCalculationResult(7,"fingerprint","month","combined","gross",[],[],[],[],[],[],"0","0",[]);

        var error=Assert.Throws<FinancialStatementInputException>(()=>
            new FinancialReportReconciliationService().Calculate(snapshot,cashflows,TestContext.Current.CancellationToken));

        Assert.Contains("complete independent ledger evidence",error.Message);
    }

    [Theory]
    [InlineData("100", "Matched", "matched")]
    [InlineData("90", "Differences", "changed")]
    public void IndependentLedgerAnchorsMatchOrFlagDifferentTreasuryPostings(string cashflowDebit,
        string expectedStatus, string expectedPostingStatus)
    {
        const string roleJson = "{\"debit\":3,\"credit\":4,\"date\":2,\"voucher\":1}";
        const string columnsJson = "[{\"id\":1,\"column_name\":\"Voucher ID\"},{\"id\":2,\"column_name\":\"Date\"},{\"id\":3,\"column_name\":\"Debit\"},{\"id\":4,\"column_name\":\"Credit\"}]";
        string configurationJson = "[{\"credit\": 4, \"date\": 2, \"debit\": 3, \"voucher\": 1}, "
            + "[{\"column_name\": \"Voucher ID\", \"id\": 1}, {\"column_name\": \"Date\", \"id\": 2}, "
            + "{\"column_name\": \"Debit\", \"id\": 3}, {\"column_name\": \"Credit\", \"id\": 4}]]";
        string configuration = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(configurationJson))).ToLowerInvariant();
        string fingerprint = Convert.ToHexString(MD5.HashData(Encoding.UTF8.GetBytes("101:1:abc"))).ToLowerInvariant();
        var evidence = new ReportCashflowLedgerEvidenceUpload(40, 9, "B1", "Operating bank",
            new DateOnly(2026, 1, 1), new DateOnly(2026, 1, 31),
            JsonSerializer.Serialize(new { balance_check = new { configuration, canonical_fingerprint = fingerprint,
                source_mode = "replace_range", status = "consistent", opening_source = "erp_opening",
                opening = "1000", closing = "1100" } }),
            roleJson, columnsJson);
        var row = new ReportCashflowLedgerEvidenceRow(101, 40, 9, "B1", 1, "abc",
            new DateOnly(2026, 1, 10), "{\"Voucher ID\":\"V1\",\"Date\":\"2026-01-10\",\"Debit\":\"100\",\"Credit\":\"0\"}");
        var snapshot = Snapshot() with
        {
            CashflowLedgerEvidenceUploads = Dataset([evidence]),
            CashflowLedgerEvidenceRows = Dataset([row])
        };
        var cashflows = new CashflowCalculationResult(7, "fingerprint", "month", "combined", "gross",
            [new CashflowPeriod("2026-01", "2026-01-01", "2026-01-31", "Jan-26")], [], [], [], [], [],
            "100", "0", [new CashflowPostingEvidence(1, "V1", "2026-01-10", "B1", cashflowDebit, "0")]);

        var result = new FinancialReportReconciliationService().Calculate(snapshot, cashflows,
            TestContext.Current.CancellationToken);

        Assert.Equal(expectedStatus, result.Status);
        Assert.Equal(expectedPostingStatus, Assert.Single(result.PostingFindings).Status);
        var period = Assert.Single(Assert.Single(result.Accounts).Periods);
        Assert.Equal(expectedStatus, period.Status);
        Assert.Equal("100", period.LedgerMovement);
        Assert.Equal("1100", period.LedgerClosing);
        Assert.Equal(expectedStatus == "Matched" ? "0" : "10", period.Difference);

        var sheets = FinancialReportWorkbookWriter.CashflowSheets(cashflows, result,
            includeReconciliation: true, snapshot: snapshot);
        var proof = Assert.Single(sheets, sheet => sheet.Name == "Reconciliation Proof");
        Assert.Contains(proof.Rows, row => row.Contains("Overall status") && row.Contains(expectedStatus));
        Assert.Contains(proof.Rows, row => row.Contains("V1") && row.Contains(expectedPostingStatus));
        Assert.Contains(sheets, sheet => sheet.Name == "Reconciliation Evidence");

        byte[] workbook = FinancialReportWorkbookWriter.Write(sheets, new ReportInputOptions(),
            TestContext.Current.CancellationToken);
        string path = Path.Combine(Path.GetTempPath(), "reconciliation-built-" + Guid.NewGuid().ToString("N") + ".xlsx");
        try
        {
            File.WriteAllBytes(path, workbook);
            var exportedProof = WorkbookReader.Read(path, ".xlsx", "Reconciliation Proof",
                ct: TestContext.Current.CancellationToken).ToArray();
            var exportedText = exportedProof.SelectMany(row => row.Cells).Select(cell => cell.Text).ToArray();
            Assert.Contains(expectedStatus, exportedText);
            Assert.Contains("V1", exportedText);
            Assert.Contains(expectedPostingStatus, exportedText);
        }
        finally { if (File.Exists(path)) File.Delete(path); }
    }

    private static ReportInputSnapshot Snapshot() => new(
        new ReportInputScope(7,ReportInputKind.LedgerSource,null,3,[30],null,"2026-01-01","2026-01-31",
            ReportAccountCategorySource.Cashflow,null,"combined"),
        new ReportInputIdentity("test","fingerprint","configuration",DateTime.UtcNow),
        Dataset<ReportLedgerUpload>([]),Dataset<ReportClientDataRow>([]),Dataset<ReportTrialBalanceUpload>([]),
        Dataset<ReportTrialBalanceAccount>([]),Dataset<ReportTrialBalanceAdjustment>([]),
        Dataset<ReportAccountAssignment>([]),Dataset<ReportCategoryMapping>([]),Dataset<ReportCategory>([]),
        Dataset<ReportStatementCalculation>([]),Dataset<ReportStatementComponent>([]),Dataset<ReportProfitLink>([]),
        Dataset<ReportProfitStatus>([]),Dataset<ReportCashflowSettings>([]),
        Dataset<ReportCashflowAccountAssignment>([]),Dataset<ReportCashflowRoleCategory>([]),
        Dataset([new ReportCashflowTreasuryAccount("B1","bank")]),Dataset<ReportCashflowColumnMapping>([]))
        { CashflowLedgerEvidenceUploads=Dataset<ReportCashflowLedgerEvidenceUpload>([]),
          CashflowLedgerEvidenceRows=Dataset<ReportCashflowLedgerEvidenceRow>([]) };

    private static ReportInputDataset<T> Dataset<T>(IReadOnlyList<T> rows)=>new(true,true,rows.Count,0,rows);
}
