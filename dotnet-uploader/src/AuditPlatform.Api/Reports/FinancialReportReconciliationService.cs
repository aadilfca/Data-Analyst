using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Cashflows;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Uploads;

namespace AuditPlatform.Api.Reports;

/// <summary>Compares Cashflows to independent imported ledger evidence without guessing missing balances.</summary>
internal sealed class FinancialReportReconciliationService
{
    private sealed record LedgerLine(int Id, string Voucher, DateOnly Date, UploadExactAmount Debit,
        UploadExactAmount Credit, int Version, string? Fingerprint);
    private sealed record Anchor(ReportCashflowLedgerEvidenceUpload Upload, DateOnly Start, DateOnly End,
        UploadExactAmount Opening, UploadExactAmount Closing, string OpeningSource,
        IReadOnlyList<LedgerLine> Lines);
    private static readonly UploadExactAmount Tolerance = UploadExactAmount.Parse("0.01");

    public FinancialReportReconciliationResult Calculate(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows, CancellationToken ct = default)
    {
        if (snapshot.Scope.Kind != ReportInputKind.LedgerSource
            || snapshot.Scope.CategorySource != ReportAccountCategorySource.Cashflow
            || !snapshot.CashflowLedgerEvidenceUploads.Requested || !snapshot.CashflowLedgerEvidenceUploads.Complete
            || !snapshot.CashflowLedgerEvidenceRows.Requested || !snapshot.CashflowLedgerEvidenceRows.Complete)
            throw new FinancialStatementInputException("A complete independent ledger evidence snapshot is required for reconciliation.");
        if (!StringComparer.Ordinal.Equals(snapshot.Identity.Fingerprint, cashflows.InputFingerprint))
            throw new FinancialStatementInputException("Cashflows and ledger evidence do not share the same captured source identity.");

        var accountTypes = snapshot.CashflowTreasuryAccounts.Items.GroupBy(row => row.AccountCode.Trim(), StringComparer.Ordinal)
            .ToDictionary(group => group.Key,
                group => group.Last().TreasuryType.Equals("cash", StringComparison.OrdinalIgnoreCase) ? "Cash" : "Bank",
                StringComparer.Ordinal);
        var accountResults = new List<FinancialReportAccountReconciliation>();
        var diagnostics = new List<string>();
        foreach (var (code, type) in accountTypes.OrderBy(pair => pair.Key, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var metadata = snapshot.CashflowLedgerEvidenceUploads.Items.Where(row => row.AccountCode == code)
                .OrderBy(row => row.Id).ToArray();
            var linesByUpload = snapshot.CashflowLedgerEvidenceRows.Items.Where(row => metadata.Any(upload => upload.Id == row.UploadId))
                .GroupBy(row => row.UploadId).ToDictionary(group => group.Key, group => group.ToArray());
            var anchors = new List<Anchor>();
            var failed = false;
            if (metadata.Length == 0) diagnostics.Add($"No imported independent ledger is available for Cash/Bank account {code}.");
            if (metadata.Select(row => row.TemplateId).Distinct().Count() > 1)
            { diagnostics.Add($"Several ledger templates are configured for account {code}; balance evidence is unavailable."); failed = true; }

            foreach (var upload in metadata)
            {
                ct.ThrowIfCancellationRequested();
                try
                {
                    using var evidenceDoc = JsonDocument.Parse(upload.EvidenceJson ?? "{}");
                    var evidence = evidenceDoc.RootElement;
                    var check = evidence.TryGetProperty("balance_check", out var checkElement) ? checkElement : default;
                    if (check.ValueKind != JsonValueKind.Object || !ConfigurationCurrent(upload, check))
                    { diagnostics.Add($"Ledger setup for account {code} changed; revalidate the ledger upload."); continue; }
                    if (!TryDate(upload.PeriodStart, out var start) || !TryDate(upload.PeriodEnd, out var end) || start > end)
                        throw new FormatException();
                    var lines = ParseLines(upload, linesByUpload.GetValueOrDefault(upload.Id) ?? []);
                    string fingerprint = CanonicalFingerprint(lines.Where(row => row.Date >= start && row.Date <= end));
                    if (!TryString(check, "canonical_fingerprint", out var savedFingerprint)
                        || !StringComparer.OrdinalIgnoreCase.Equals(fingerprint, savedFingerprint))
                    { diagnostics.Add($"Ledger proof for account {code} changed; current balances require revalidation."); continue; }
                    if (!TryString(check, "source_mode", out var sourceMode) || sourceMode != "replace_range"
                        || !TryString(check, "status", out var proofStatus) || proofStatus != "consistent"
                        || !TryString(check, "opening_source", out var openingSource)
                        || openingSource is not ("erp_opening" or "inferred_from_transaction" or "inferred_from_closing"))
                        continue;
                    if (!TryAmount(check, "opening", out var opening) || !TryAmount(check, "closing", out var closing))
                        throw new FormatException();
                    var rangeMovement = Sum(lines.Where(row => row.Date >= start && row.Date <= end));
                    if ((opening + rangeMovement - closing).Abs().CompareTo(Tolerance) > 0)
                        throw new FormatException();
                    anchors.Add(new(upload,start,end,opening,closing,openingSource,lines));
                }
                catch (Exception error) when (error is JsonException or FormatException or OverflowException or InvalidOperationException)
                { diagnostics.Add($"Ledger evidence for account {code} is invalid; its balances are unavailable."); failed = true; }
            }

            var accountPeriods = new List<FinancialReportPeriodReconciliation>();
            foreach (var period in cashflows.Periods)
            {
                if (!TryDate(period.Start, out var start) || !TryDate(period.End, out var end) || end == DateOnly.MaxValue)
                    throw new FinancialStatementInputException("The selected reconciliation period is invalid.");
                var periodAnchors = anchors.Where(anchor => anchor.Start <= start && anchor.End >= end).ToArray();
                if (failed || periodAnchors.Length == 0)
                {
                    accountPeriods.Add(new(period.Key,"Unavailable",null,null,null,null,null,null));
                    continue;
                }
                var balances = periodAnchors.Select(anchor => (
                    Opening: anchor.Opening + Sum(anchor.Lines.Where(row => row.Date >= anchor.Start && row.Date < start)),
                    Closing: anchor.Opening + Sum(anchor.Lines.Where(row => row.Date >= anchor.Start && row.Date <= end)),
                    Movement: Sum(anchor.Lines.Where(row => row.Date >= start && row.Date <= end)))).ToArray();
                if (balances.Select(row => row.Opening.ToFixedString()).Distinct(StringComparer.Ordinal).Count() > 1
                    || balances.Select(row => row.Closing.ToFixedString()).Distinct(StringComparer.Ordinal).Count() > 1)
                {
                    accountPeriods.Add(new(period.Key,"Review required",null,null,null,null,null,null));
                    diagnostics.Add($"Current independent ledger anchors disagree for {code} in {period.Key}.");
                    continue;
                }
                var balance = balances[0];
                var flow = cashflows.PeriodTotals.Single(row => row.PeriodKey == period.Key);
                var cashflowMovement = UploadExactAmount.Parse(flow.NetMovement);
                var calculated = balance.Opening + cashflowMovement;
                var difference = balance.Closing - calculated;
                accountPeriods.Add(new(period.Key,difference.Abs().CompareTo(Tolerance)<=0?"Matched":"Differences",
                    balance.Opening.ToFixedString(),balance.Closing.ToFixedString(),balance.Movement.ToFixedString(),
                    cashflowMovement.ToFixedString(),calculated.ToFixedString(),difference.ToFixedString()));
            }
            string name = metadata.LastOrDefault()?.AccountName is { Length: > 0 } value && value != code ? value : code;
            accountResults.Add(new(code,name,type,accountPeriods));
        }

        var postingFindings = ComparePostings(snapshot, cashflows, accountTypes, diagnostics, ct);
        string postingStatus = postingFindings.Count == 0 ? "Unavailable"
            : postingFindings.Any(row => row.Status == "unresolved") ? "Review required"
            : postingFindings.Any(row => row.Status != "matched") ? "Differences" : "Matched";
        string status = accountResults.SelectMany(row => row.Periods).Any(row => row.Status == "Unavailable")
            ? "Unavailable" : accountResults.SelectMany(row => row.Periods).Any(row => row.Status == "Review required")
                ? "Review required" : accountResults.SelectMany(row => row.Periods).Any(row => row.Status == "Differences")
                    ? "Differences" : "Matched";
        string digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(snapshot.Identity.Fingerprint
            + "|" + string.Join("|", accountResults.SelectMany(row => row.Periods).Select(row => row.Difference ?? "unavailable"))))).ToLowerInvariant();
        return new(status,digest,postingStatus,accountResults,postingFindings,diagnostics);
    }

    private static IReadOnlyList<FinancialReportPostingFinding> ComparePostings(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows, IReadOnlyDictionary<string,string> accountTypes,
        List<string> diagnostics, CancellationToken ct)
    {
        var findings = new List<FinancialReportPostingFinding>();
        var transaction = cashflows.TreasuryPostings.GroupBy(row => (row.AccountCode,row.VoucherId))
            .ToDictionary(group => group.Key, group => group.Select(row => PostingKey(row.Date,row.Debit,row.Credit))
                .GroupBy(key=>key,StringComparer.Ordinal).ToDictionary(group=>group.Key,group=>group.Count(),StringComparer.Ordinal));
        var ledgerRows = new List<(string Code,string Voucher,string Date,string Debit,string Credit)>();
        foreach(var upload in snapshot.CashflowLedgerEvidenceUploads.Items.Where(row=>accountTypes.ContainsKey(row.AccountCode)))
        {
            try
            {
                using var doc=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");
                var roles=doc.RootElement;
                string? RoleColumn(string role)
                {
                    if(!roles.TryGetProperty(role,out var id)||id.ValueKind==JsonValueKind.Null)return null;
                    string key=id.ToString();using var columns=JsonDocument.Parse(upload.TemplateColumnsJson);
                    foreach(var column in columns.RootElement.EnumerateArray())
                        if(column.TryGetProperty("id",out var columnId)&&columnId.ToString()==key
                            &&column.TryGetProperty("column_name",out var name))return name.GetString();
                    return null;
                }
                var voucherColumn=RoleColumn("voucher_id");var dateColumn=RoleColumn("date");
                var debitColumn=RoleColumn("debit");var creditColumn=RoleColumn("credit");
                if(voucherColumn is null||dateColumn is null||debitColumn is null||creditColumn is null)
                {diagnostics.Add($"Ledger posting identity is incomplete for account {upload.AccountCode}.");continue;}
                foreach(var row in snapshot.CashflowLedgerEvidenceRows.Items.Where(row=>row.UploadId==upload.Id))
                {
                    using var json=JsonDocument.Parse(row.RowDataJson);var root=json.RootElement;
                    string voucher=Column(root,voucherColumn).Trim();string dateText=Column(root,dateColumn);
                    if(voucher.Length==0||!DateOnly.TryParse(dateText,CultureInfo.InvariantCulture,out var day))
                    {ledgerRows.Add((upload.AccountCode,voucher,"!invalid",Column(root,debitColumn),Column(root,creditColumn)));continue;}
                    ledgerRows.Add((upload.AccountCode,voucher,day.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),
                        Column(root,debitColumn),Column(root,creditColumn)));
                }
            }
            catch(JsonException){diagnostics.Add($"Ledger posting rows for account {upload.AccountCode} cannot be read.");}
        }
        var ledger=ledgerRows.GroupBy(row=>(row.Code,row.Voucher)).ToDictionary(group=>group.Key,
            group=>group.Select(row=>PostingKey(row.Date,row.Debit,row.Credit)).GroupBy(key=>key,StringComparer.Ordinal)
                .ToDictionary(group=>group.Key,group=>group.Count(),StringComparer.Ordinal));
        foreach(var key in transaction.Keys.Union(ledger.Keys).OrderBy(key=>key.Item1,StringComparer.Ordinal).ThenBy(key=>key.Item2,StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();var left=ledger.GetValueOrDefault(key)??new(StringComparer.Ordinal);
            var right=transaction.GetValueOrDefault(key)??new(StringComparer.Ordinal);
            bool equal=left.Count==right.Count&&left.All(pair=>right.GetValueOrDefault(pair.Key)==pair.Value);
            string status=key.Item2.Length==0||left.ContainsKey("!invalid")||right.ContainsKey("!invalid")?"unresolved":equal?"matched":left.Count==0?"transaction_only":right.Count==0?"ledger_only":"changed";
            var fields=new List<string>();if(!equal){fields.Add("dates_or_amounts");if(left.Values.Sum()!=right.Values.Sum())fields.Add("line_count");}
            findings.Add(new(key.Item1,key.Item2,status,left.Values.Sum(),right.Values.Sum(),fields));
        }
        return findings;
    }

    private static IReadOnlyList<LedgerLine> ParseLines(ReportCashflowLedgerEvidenceUpload upload,
        IReadOnlyList<ReportCashflowLedgerEvidenceRow> rows)
    {
        using var rolesDoc=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");var roles=rolesDoc.RootElement;
        string ColumnName(string role)
        {
            if(!roles.TryGetProperty(role,out var id))throw new FormatException();string key=id.ToString();
            using var columns=JsonDocument.Parse(upload.TemplateColumnsJson);
            foreach(var col in columns.RootElement.EnumerateArray())
                if(col.TryGetProperty("id",out var found)&&found.ToString()==key&&col.TryGetProperty("column_name",out var name))return name.GetString()??throw new FormatException();
            throw new FormatException();
        }
        string debit=ColumnName("debit"),credit=ColumnName("credit");
        string? dateRole=roles.TryGetProperty("date",out _)?ColumnName("date"):null;
        var result=new List<LedgerLine>();
        foreach(var row in rows)
        {
            if(row.ExtractDate is null)throw new FormatException();using var data=JsonDocument.Parse(row.RowDataJson);
            var day=dateRole is null?row.ExtractDate.Value:ParseDate(Column(data.RootElement,dateRole),row.ExtractDate.Value);
            string dr=Column(data.RootElement,debit),cr=Column(data.RootElement,credit);
            if(string.IsNullOrWhiteSpace(dr)&&string.IsNullOrWhiteSpace(cr))throw new FormatException();
            result.Add(new(row.Id,"",day,Amount(dr),Amount(cr),row.Version,row.SourceFingerprint));
        }
        return result;
    }

    private static bool ConfigurationCurrent(ReportCashflowLedgerEvidenceUpload upload,JsonElement check)
    {
        if(!TryString(check,"configuration",out var expected))return false;
        try
        {
            using var roles=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");using var columns=JsonDocument.Parse(upload.TemplateColumnsJson);
            string payload="["+PythonJson(roles.RootElement)+", "+PythonJson(columns.RootElement)+"]";
            string actual=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(payload))).ToLowerInvariant();
            return StringComparer.OrdinalIgnoreCase.Equals(actual,expected);
        }
        catch(JsonException){return false;}
    }

    private static string PythonJson(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Object=>"{"+string.Join(", ",value.EnumerateObject().OrderBy(p=>p.Name,StringComparer.Ordinal).Select(p=>PythonString(p.Name)+": "+PythonJson(p.Value)))+"}",
        JsonValueKind.Array=>"["+string.Join(", ",value.EnumerateArray().Select(PythonJson))+"]",
        JsonValueKind.String=>PythonString(value.GetString()??""),JsonValueKind.Number=>value.GetRawText(),
        JsonValueKind.True=>"true",JsonValueKind.False=>"false",_=>"null"
    };
    private static string PythonString(string value)
    {
        var output=new StringBuilder("\"");foreach(char c in value)
        {if(c=='"')output.Append("\\\"");else if(c=='\\')output.Append("\\\\");else if(c=='\b')output.Append("\\b");else if(c=='\f')output.Append("\\f");else if(c=='\n')output.Append("\\n");else if(c=='\r')output.Append("\\r");else if(c=='\t')output.Append("\\t");else if(c<0x20||c>0x7e)output.Append("\\u").Append(((int)c).ToString("x4",CultureInfo.InvariantCulture));else output.Append(c);}output.Append('"');return output.ToString();
    }
    private static string CanonicalFingerprint(IEnumerable<LedgerLine> rows)
    {
        var list=rows.OrderBy(row=>row.Id).ToArray();if(list.Any(row=>row.Fingerprint is null))return "";
        string text=string.Join(',',list.Select(row=>$"{row.Id}:{row.Version}:{row.Fingerprint}"));
        return Convert.ToHexString(MD5.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    }
    private static UploadExactAmount Sum(IEnumerable<LedgerLine> rows)=>rows.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+row.Debit-row.Credit);
    private static UploadExactAmount Amount(string value)=>string.IsNullOrWhiteSpace(value)?UploadExactAmount.Zero:UploadExactAmount.Parse(value);
    private static bool TryAmount(JsonElement root,string key,out UploadExactAmount value){value=default;if(!root.TryGetProperty(key,out var item)||item.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)return false;try{value=Amount(item.ToString());return true;}catch(FormatException){return false;}}
    private static bool TryString(JsonElement root,string key,out string value){value="";if(!root.TryGetProperty(key,out var item)||item.ValueKind!=JsonValueKind.String)return false;value=item.GetString()??"";return value.Length>0;}
    private static string Column(JsonElement row,string name){foreach(var p in row.EnumerateObject())if(StringComparer.OrdinalIgnoreCase.Equals(p.Name,name))return p.Value.ValueKind==JsonValueKind.String?p.Value.GetString()??"":p.Value.ValueKind==JsonValueKind.Null?"":p.Value.GetRawText();return "";}
    private static DateOnly ParseDate(string value,DateOnly fallback)=>DateOnly.TryParse(value,CultureInfo.InvariantCulture,out var result)?result:fallback;
    private static bool TryDate(DateOnly? value,out DateOnly result){result=value.GetValueOrDefault();return value.HasValue;}
    private static bool TryDate(string value,out DateOnly result)=>DateOnly.TryParseExact(value,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out result);
    private static string PostingKey(string date,string debit,string credit)=>$"{date}|{Amount(debit).ToFixedString()}|{Amount(credit).ToFixedString()}";
}
