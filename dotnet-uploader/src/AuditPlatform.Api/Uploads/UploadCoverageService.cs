using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Workbooks;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Uploads;

// Source coverage never authorizes replacement. Stored identities, correction
// matching, ledger balance proofs and signed plans retain their later gates.
public sealed class UploadCoverageService(UploadPreparationService preparation,PostgresTransactions transactions,
    IOptions<UploadCoverageOptions> options,IOptions<UploadFootingOptions> footingOptions,
    IOptions<UploadInputValidationOptions> inputOptions)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    private sealed class ReferenceChanged : Exception;
    private sealed class InvalidReference : Exception;
    private sealed class UnavailableReference : Exception;
    public async Task<CoverageResult> CheckAsync(VerifiedSession? session,int file,CoverageRequest input,CancellationToken ct=default)
    {
        if(!options.Value.Enabled||!footingOptions.Value.Enabled||!inputOptions.Value.Enabled) return new("unavailable");
        if(input.Footing?.Validation is null||!UploadInputValidationService.ValidRequest(input.Footing.Validation,inputOptions.Value)
            || input.ExpectedCoverageKey is not null && input.ExpectedCoverageKey.Length!=64
            || input.Footing.ExpectedCheckKey is not null && input.Footing.ExpectedCheckKey.Length!=64
            || new[]{input.CoverageStart,input.CoverageEnd,input.LedgerPeriodStart,input.LedgerPeriodEnd}.Any(s=>s is {Length:>10})
            || input.LedgerAccountCode is {Length:>120} || input.LedgerAccountCode?.Any(char.IsControl)==true)
            return new("invalid",Code:"invalid_coverage_request");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var caller=ct;using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var validation=input.Footing.Validation with {Source=input.Footing.Validation.Source with {Operation="rows"}};
            input=input with {Footing=input.Footing with {Validation=validation}};
            var result=await preparation.ReadBoundAsync(session,file,validation.Source,async(path,hash,template,saved,limits,token)=>
            {
                // Authorization precedes these reads. Each single SQL statement
                // selects latest TB and account together; parsing holds no DB lock.
                CoverageLedger? ledger=template.Purpose=="account_ledger"
                    ?await ReadLedgerAsync(session!,validation.Source.ClientId,input.LedgerAccountCode,token):null;
                using var rows=WorkbookReader.Read(path,".xls",validation.Source.Sheet,limits,token).GetEnumerator();
                if(!rows.MoveNext()) throw new WorkbookReadException("Workbook has no header row.");
                var source=UploadPreparationService.ResolveSource(rows.Current.Cells,hash,file,template,saved,validation.Source);
                IEnumerable<WorkbookRow> Remaining() {while(rows.MoveNext()) yield return rows.Current;}
                var output=Evaluate(template,source.Mappings,source.Context,input,Remaining(),options.Value,footingOptions.Value,inputOptions.Value,ledger,token);
                if(ledger is not null && ledger!=await ReadLedgerAsync(session!,validation.Source.ClientId,input.LedgerAccountCode,token)) throw new ReferenceChanged();
                output=output with {Warnings=source.Warnings.Concat(output.Warnings).Distinct().ToArray()};
                UploadPreparationService.JsonSize(output,options.Value.MaximumResponseBytes);
                return output;
            },deadline.Token);
            if(result.Status!="success") return new(result.Status,Code:result.Code);
            var value=result.Value!;
            return value.Status=="confirmation_changed"?new("conflict",value,"coverage_check_changed")
                :value.Footing.Status=="invalid_confirmation"?new("invalid",value,"variance_confirmation_not_applicable"):new("success",value);
        }
        catch(ReferenceChanged) {return new("conflict",Code:"ledger_reference_changed");}
        catch(InvalidReference) {return new("review",Code:"ledger_account_review_required");}
        catch(UnavailableReference) {return new("review",Code:"ledger_reference_unavailable");}
        catch(OperationCanceledException) when(!caller.IsCancellationRequested && deadline.IsCancellationRequested)
        {return new("timeout",Code:"coverage_timed_out");}
        catch(Exception error) when(error is WorkbookReadException or UploadPreparationService.InvalidPreparation or IOException
            or System.Xml.XmlException or ExcelDataReader.Exceptions.HeaderException or ExcelDataReader.Exceptions.InvalidPasswordException)
        {return new("invalid",Code:"invalid_coverage_source");}
        finally {requests.Release();}
    }

    private async Task<CoverageLedger> ReadLedgerAsync(VerifiedSession session,int client,string? code,CancellationToken ct)
    {
        code=code?.Trim();if(string.IsNullOrEmpty(code)) throw new InvalidReference();
        try {return await transactions.ExecuteAsync(async(db,token)=>
        {
            await using var command=db.CreateCommand("""
                WITH latest AS (
                    SELECT u.id,u.period_end,u.uploaded_at FROM public.trial_balance_uploads u
                    WHERE u.client_id=$1 AND u.status='parsed' AND EXISTS (
                        SELECT 1 FROM public.platform_tenant_client_homes h WHERE h.client_id=u.client_id AND h.tenant_id=$2)
                    ORDER BY (u.period_end IS NULL),u.period_end DESC,u.uploaded_at DESC LIMIT 1)
                SELECT l.id,l.period_end::text,l.uploaded_at::text,a.account_code,a.account_name
                FROM latest l JOIN public.trial_balance_accounts a ON a.upload_id=l.id
                WHERE a.classification='leaf' AND a.account_code=$3 LIMIT 2
                """);
            command.Parameters.AddWithValue(client);command.Parameters.AddWithValue(session.TenantId!.Value);command.Parameters.AddWithValue(code);
            await using var reader=await command.ExecuteReaderAsync(token);
            if(!await reader.ReadAsync(token)) throw new InvalidReference();
            int upload=reader.GetInt32(0);string? period=reader.IsDBNull(1)?null:reader.GetString(1);
            string? stamp=reader.IsDBNull(2)?null:reader.GetString(2);string account=reader.GetString(3),name=reader.IsDBNull(4)?"":reader.GetString(4);
            if(await reader.ReadAsync(token)) throw new InvalidReference();
            return new CoverageLedger(upload,account,name,Key(new {upload,period,stamp,account,name}));
        },ct);}
        catch(PostgresException error) when(error.SqlState is "42P01" or "42703") {throw new UnavailableReference();}
    }

    private static string Key<T>(T value)=>Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(value)));
    private static CoveragePeriod? Period(string? start,string? end)=>DateOnly.TryParseExact(start,"yyyy-MM-dd",CultureInfo.InvariantCulture,
        DateTimeStyles.None,out var first)&&DateOnly.TryParseExact(end,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var last)&&first<=last
        ?new(start!,end!):null;

    public static CoverageOutput Evaluate(TemplateDefinition template,IReadOnlyList<PreparedMapping> mappings,PreparationContext context,
        CoverageRequest input,IEnumerable<WorkbookRow> rows,UploadCoverageOptions limits,UploadFootingOptions footingLimits,
        UploadInputValidationOptions inputLimits,CoverageLedger? ledger=null,CancellationToken ct=default)
    {
        bool complete=input.Footing.Validation.ComparisonMode=="replace_range",isLedger=template.Purpose=="account_ledger";
        var issues=new List<CoverageIssue>();int issueCount=0;long issueBytes=0;
        void Issue(int row,string kind,string message)
        {
            issueCount++;if(issues.Count>=limits.MaximumIssueDetails) return;
            var issue=new CoverageIssue(row,kind,message);
            try {issueBytes+=UploadPreparationService.JsonSize(issue,limits.MaximumResponseBytes/4-issueBytes);issues.Add(issue);}
            catch(UploadPreparationService.InvalidPreparation) { }
        }
        var date=isLedger?mappings.SingleOrDefault(m=>m.ColumnId==template.Ledger.GetValueOrDefault("date"))
            :mappings.SingleOrDefault(m=>m.Role=="document_date");
        var declared=complete?Period(input.CoverageStart??(isLedger?input.LedgerPeriodStart:null),input.CoverageEnd??(isLedger?input.LedgerPeriodEnd:null)):null;
        var ledgerPeriod=isLedger?Period(input.LedgerPeriodStart,input.LedgerPeriodEnd):null;
        if(complete)
        {
            if(!input.ConfirmReportCoverage) Issue(1,"confirmation_required","Confirm that the declared dates cover the full ERP report period.");
            if(declared is null) Issue(1,"invalid_period","Enter ordered coverage dates as YYYY-MM-DD.");
            if(date?.SourcePosition is null) Issue(1,"document_date_required","Configure and map the Document Date before checking a Complete report.");
        }
        if(isLedger)
        {
            if(input.Footing.Validation.Source.ForceIncludeRows?.Count>0)
                Issue(1,"ledger_override_not_allowed","Correct rejected or blank ledger rows in Excel; ledger overrides are not allowed.");
            if(ledger is null||ledger.AccountCode!=input.LedgerAccountCode?.Trim()) Issue(1,"ledger_account_required","Choose one posting account from the client's latest parsed Trial Balance.");
            if(ledgerPeriod is null) Issue(1,"invalid_ledger_period","Enter ordered ledger dates as YYYY-MM-DD.");
            if(complete && declared!=ledgerPeriod) Issue(1,"ledger_period_mismatch","Complete coverage must match the declared ledger period.");
        }
        else if(input.LedgerAccountCode is not null||input.LedgerPeriodStart is not null||input.LedgerPeriodEnd is not null)
            Issue(1,"ledger_template_required","Account scope requires an Account Ledger template.");
        var activePeriod=isLedger?ledgerPeriod:declared;
        DateOnly? first=null,last=null;int dates=0,selectedDates=0,missing=0,invalid=0,outside=0;
        string delimiter=input.Footing.Validation.Source.DateDelimiter??"/",order=(input.Footing.Validation.Source.DateOrder??"DMY").ToUpperInvariant();
        void Observe(CheckedSourceRow source)
        {
            if(source.Kind!="transaction"||date?.SourcePosition is not >0) return;
            var row=source.Row;var cell=date.SourcePosition<=row.Cells.Count?row.Cells[date.SourcePosition.Value-1]:new(row.Number,date.SourcePosition.Value,WorkbookCellKind.Blank);
            if(UploadInputValidationService.Blank(cell)) {missing++;return;}
            DateTime? parsed;
            try {parsed=UploadDateParser.Parse(cell,delimiter,order);} catch(FormatException) {invalid++;return;}
            if(parsed is null) {missing++;return;}
            var day=DateOnly.FromDateTime(parsed.Value);dates++;if(source.Eligible) selectedDates++;
            first=first is null||day<first?day:first;last=last is null||day>last?day:last;
            // Complete source evidence precedes selection. Partial ledgers check
            // only included transactions, retaining their existing exclusion rule.
            if(activePeriod is not null && (complete||isLedger&&!source.Excluded)
                && (string.CompareOrdinal(day.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),activePeriod.Start)<0
                    || string.CompareOrdinal(day.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),activePeriod.End)>0))
            {outside++;Issue(row.Number,"outside_coverage","This transaction date is outside the declared period. Widen coverage or correct Excel.");}
        }
        var footing=UploadFootingService.Evaluate(template,mappings,context,input.Footing,rows,footingLimits,inputLimits,ct,Observe);
        bool footingReady=footing.InputValidation.InputChecksPassed && (footing.Status=="not_configured"||footing.FootingPassed);
        // No exclusions/rejections/errors may manufacture an empty replacement.
        bool genuinelyEmpty=footing.InputValidation.Counts.TransactionRows==0;
        if(complete && genuinelyEmpty && !input.ConfirmEmptyPeriod) Issue(1,"empty_confirmation_required","Confirm the empty report separately before clearing this period.");
        if(complete && !genuinelyEmpty && input.ConfirmEmptyPeriod) Issue(1,"report_not_empty","This report has transaction rows. Uncheck Empty report.");
        var suggestion=date is null?null:new CoverageDateSummary(!input.Footing.ControlTotalConfirmed&&mappings.Any(m=>m.Role=="footing")
            ?"await_footing_validation":date.SourcePosition is null?"map_document_date":!footingReady?"source_review_required":"ready",date.ColumnId,date.SourcePosition,
            first?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),last?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),dates,selectedDates,missing,invalid,outside);
        // Exclude variance acknowledgement and correlation ID, but bind every
        // declared coverage choice and current reference to the underlying proof.
        string key=Key(new {Policy="Coverage-v1",context.SelectionKey,footing.CheckKey,input.CoverageStart,input.CoverageEnd,
            input.ConfirmReportCoverage,input.ConfirmEmptyPeriod,input.LedgerAccountCode,input.LedgerPeriodStart,input.LedgerPeriodEnd,
            Ledger=ledger,Date=new {date?.ColumnId,date?.SourcePosition,first,last,dates,selectedDates,missing,invalid,outside},
            Transactions=footing.InputValidation.Counts.TransactionRows});
        bool stale=input.ExpectedCoverageKey is not null&&input.ExpectedCoverageKey!=key
            || input.Footing.ConfirmVariance&&input.ExpectedCoverageKey is null||footing.Status=="confirmation_changed";
        string status=stale?"confirmation_changed":!footingReady?"source_checks_failed":issueCount>0?"coverage_checks_failed"
            :complete?(genuinelyEmpty?"empty_complete_period":"complete_period"):"partial_extract";
        bool passed=status is "complete_period" or "empty_complete_period" or "partial_extract";
        var warnings=new List<string> {"Coverage evidence is not import authorization, stored-data proof or ledger balance reconciliation."};
        if(!complete) warnings.Add("Partial extracts never certify a complete period or authorize removal of absent transactions.");
        warnings.AddRange(footing.Warnings);
        if(suggestion is not null && suggestion.Status!="ready") suggestion=suggestion with {Start=null,End=null};
        return new(status,context,key,input.Footing.Validation.ComparisonMode,activePeriod,ledger,suggestion,passed,
            passed&&complete,passed&&complete&&genuinelyEmpty&&input.ConfirmEmptyPeriod,false,footing,issueCount,issues,issueCount>issues.Count,warnings);
    }
}
