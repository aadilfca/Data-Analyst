using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Workbooks;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Uploads;

// Read-only source proof. No stored transaction, import plan or durable
// acknowledgement is created; P24 must bind this evidence to an import plan.
public sealed class UploadFootingService(UploadPreparationService preparation,IOptions<UploadFootingOptions> options,
    IOptions<UploadInputValidationOptions> inputOptions)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    private static readonly UploadExactAmount Noise=UploadExactAmount.Parse("0.01");
    private static readonly UploadExactAmount Limit=UploadExactAmount.Parse("1.00");
    public async Task<FootingResult> CheckAsync(VerifiedSession? session,int file,FootingRequest input,CancellationToken ct=default)
    {
        if(!options.Value.Enabled || !inputOptions.Value.Enabled) return new("unavailable");
        if(input.Validation is null || !UploadInputValidationService.ValidRequest(input.Validation,inputOptions.Value)
            || input.ExpectedCheckKey is not null && input.ExpectedCheckKey.Length!=64) return new("invalid",Code:"invalid_footing_request");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var caller=ct;using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var validation=input.Validation with {Source=input.Validation.Source with {Operation="rows"}};
            var result=await preparation.ReadBoundAsync(session,file,validation.Source,(path,hash,template,saved,limits,token)=>
            {
                using var rows=WorkbookReader.Read(path,".xls",validation.Source.Sheet,limits,token).GetEnumerator();
                if(!rows.MoveNext()) throw new WorkbookReadException("Workbook has no header row.");
                var source=UploadPreparationService.ResolveSource(rows.Current.Cells,hash,file,template,saved,validation.Source);
                IEnumerable<WorkbookRow> Remaining() {while(rows.MoveNext()) yield return rows.Current;}
                var output=Evaluate(template,source.Mappings,source.Context,input with {Validation=validation},Remaining(),
                    options.Value,inputOptions.Value,token);
                output=output with {Warnings=source.Warnings.Concat(output.Warnings).Distinct().ToArray()};
                UploadPreparationService.JsonSize(output,options.Value.MaximumResponseBytes);
                return Task.FromResult(output);
            },deadline.Token);
            if(result.Status!="success") return new(result.Status,Code:result.Code);
            var value=result.Value!;
            return value.Status=="confirmation_changed"?new("conflict",value,"footing_check_changed")
                :value.Status=="invalid_confirmation"?new("invalid",value,"variance_confirmation_not_applicable"):new("success",value);
        }
        catch(OperationCanceledException) when(!caller.IsCancellationRequested && deadline.IsCancellationRequested)
        {return new("timeout",Code:"footing_timed_out");}
        catch(Exception error) when(error is WorkbookReadException or UploadPreparationService.InvalidPreparation or IOException
            or System.Xml.XmlException or ExcelDataReader.Exceptions.HeaderException or ExcelDataReader.Exceptions.InvalidPasswordException)
        {return new("invalid",Code:"invalid_footing_source");}
        finally {requests.Release();}
    }

    // A single P19 classification pass supplies full detail and the eligible
    // selected subset. No second set of transaction/control heuristics exists.
    public static FootingOutput Evaluate(TemplateDefinition template,IReadOnlyList<PreparedMapping> mappings,
        PreparationContext context,FootingRequest input,IEnumerable<WorkbookRow> rows,UploadFootingOptions limits,
        UploadInputValidationOptions inputLimits,CancellationToken ct=default)
    {
        var footings=mappings.Where(m=>m.Role=="footing").ToArray();
        var document=mappings.SingleOrDefault(m=>m.Role=="document_id")??mappings.FirstOrDefault();
        var full=footings.ToDictionary(m=>m.ColumnId,_=>UploadExactAmount.Zero);
        var selected=footings.ToDictionary(m=>m.ColumnId,_=>UploadExactAmount.Zero);
        var reported=new Dictionary<int,UploadExactAmount>();var fullDocuments=new HashSet<string>(StringComparer.Ordinal);
        var selectedDocuments=new HashSet<string>(StringComparer.Ordinal);
        var issues=new List<FootingIssue>();int issueCount=0;long detailBytes=0;
        var incomplete=new HashSet<int>();bool selectedComplete=true;
        int fullRows=0,selectedRows=0,missingFull=0,missingSelected=0;bool selectedRowSeen=false,precisionRisk=false,invalid=false,unknownFormula=false;
        void Issue(int row,PreparedMapping? mapping,string kind,string message)
        {
            issueCount++;
            if(issues.Count>=limits.MaximumIssueDetails) return;
            var issue=new FootingIssue(row,mapping?.ColumnId,mapping?.SourcePosition,kind,message);
            try {detailBytes+=UploadPreparationService.JsonSize(issue,limits.MaximumResponseBytes/4-detailBytes);issues.Add(issue);}
            catch(UploadPreparationService.InvalidPreparation) { }
        }
        void Invalid(int row,PreparedMapping? mapping,string message)
        {invalid=true;Issue(row,mapping,"invalid_total",message);}
        if(footings.Length==0 && (input.Validation.ControlTotalRow is not null||input.ControlTotalConfirmed||input.ConfirmVariance||input.ExpectedCheckKey is not null))
            Invalid(1,null,"Template Footing settings changed. Review the control choices.");
        if(footings.Length>0 && (!input.ControlTotalConfirmed || input.Validation.ControlTotalRow is null))
            Invalid(1,null,"Explicitly confirm a valid Excel control-total row (header is row 1).");
        foreach(var mapping in footings.Where(m=>m.Type!="Numeric"||m.SourcePosition is null))
            Invalid(1,mapping,"Map each Numeric Footing field before checking totals.");
        void Observe(CheckedSourceRow observed)
        {
            ct.ThrowIfCancellationRequested();var row=observed.Row;
            WorkbookCell Cell(PreparedMapping? m)=>m?.SourcePosition is >0 and var p && p<=row.Cells.Count
                ?row.Cells[p-1]:new(row.Number,m?.SourcePosition??0,WorkbookCellKind.Blank);
            bool control=observed.Kind=="selected_control";
            if(control) selectedRowSeen=true;
            if(!control && observed.Kind!="transaction") return;
            if(!control)
            {
                fullRows++;if(observed.Eligible) selectedRows++;
                var identity=Cell(document);
                if(UploadInputValidationService.Blank(identity)) {missingFull++;if(observed.Eligible) missingSelected++;}
                else
                {
                    string text=identity.Kind switch {WorkbookCellKind.Text=>identity.Text??"",WorkbookCellKind.Number=>identity.NumberToken??"",
                        WorkbookCellKind.Boolean=>identity.Boolean==true?"True":"False",_=>identity.DateTime?.ToString("yyyy-MM-dd HH:mm:ss",CultureInfo.InvariantCulture)??identity.Time?.ToString()??""};
                    text=UploadNumericParser.Trim(text);fullDocuments.Add(text);if(observed.Eligible) selectedDocuments.Add(text);
                }
            }
            foreach(var mapping in footings.Where(m=>m.SourcePosition is not null))
            {
                var cell=Cell(mapping);unknownFormula|=cell.FormulaState==WorkbookFormulaState.Unknown;
                try
                {
                    // Preserve Python's inferred missing values in optional detail
                    // only. Source faults still block through P19 and this parser.
                    var value=UploadExactAmount.FromCell(!control&&UploadInputValidationService.Blank(cell)&&!UploadNumericParser.Check(cell).NonFinite
                        && cell.FormulaState!=WorkbookFormulaState.MissingResult?cell with {Kind=WorkbookCellKind.Blank}:cell,control);
                    if(!UploadInputValidationService.Blank(cell) && value.HasLegacyConversionRisk(cell))
                    {precisionRisk=true;Issue(row.Number,mapping,"precision_review_required","Source precision may change through a legacy float-based reader. Review numeric compatibility.");}
                    if(control) reported[mapping.ColumnId]=value;
                    else {full[mapping.ColumnId]+=value;if(observed.Eligible) selected[mapping.ColumnId]+=value;}
                }
                catch(FormatException error)
                {
                    if(!control) {incomplete.Add(mapping.ColumnId);if(observed.Eligible) selectedComplete=false;}
                    Invalid(row.Number,mapping,error.Message);
                }
            }
        }
        var validation=UploadInputValidationService.CheckRows(template,mappings,context,input.Validation,rows,inputLimits,ct,Observe);
        if(footings.Length>0 && !selectedRowSeen) Invalid(input.Validation.ControlTotalRow??1,null,"Choose an existing source control-total row.");
        var checks=new List<FootingColumnCheck>();bool confirmable=false;
        foreach(var mapping in footings)
        {
            if(incomplete.Contains(mapping.ColumnId)||!reported.TryGetValue(mapping.ColumnId,out var excel)) continue;
            var actual=full[mapping.ColumnId];var difference=actual-excel;var magnitude=difference.Abs();
            string status=difference.IsZero?"matched":magnitude.CompareTo(Noise)<0?"rounding_tolerated"
                :magnitude.CompareTo(Limit)<=0?"confirmation_required":"mismatch";
            confirmable|=status=="confirmation_required";
            checks.Add(new(mapping.ColumnId,mapping.StorageKey,mapping.Name,mapping.SourcePosition!.Value,
                excel.ToFixedString(),actual.ToFixedString(),difference.ToFixedString(),status));
        }
        var fullSummary=new FootingSummary(fullRows,fullDocuments.Count,missingFull,full.ToDictionary(k=>k.Key,v=>v.Value.ToFixedString()),incomplete.Count==0);
        var selectedSummary=new FootingSummary(selectedRows,selectedDocuments.Count,missingSelected,selected.ToDictionary(k=>k.Key,v=>v.Value.ToFixedString()),selectedComplete);
        // Deliberately exclude the acknowledgement itself. Every source/choice,
        // policy and computed total is included so a stale review cannot apply.
        string key=Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(new {
            Policy="Footing-v1-exact",Scope=new {context.FileId,context.ClientId,context.TemplateId,context.Sheet,
                context.Sha256,context.ConfigurationKey,context.MappingRevision,context.SelectionKey},validation.ValidationKey,input.ControlTotalConfirmed,
            Full=fullSummary,Selected=selectedSummary,Checks=checks})));
        bool acknowledged=input.ConfirmVariance && input.ExpectedCheckKey==key && confirmable
            && !invalid && validation.InputChecksPassed && !precisionRisk;
        string overall=invalid?"invalid_source":!validation.InputChecksPassed?"input_checks_failed":footings.Length==0?"not_configured"
            :checks.Any(c=>c.Status=="mismatch")?"mismatch":precisionRisk?"precision_review_required"
            :confirmable?(acknowledged?"confirmed_variance":"confirmation_required")
            :checks.Any(c=>c.Status=="rounding_tolerated")?"rounding_tolerated":"matched";
        if(input.ConfirmVariance && !invalid && validation.InputChecksPassed && overall!="mismatch" && overall!="precision_review_required")
            overall=!confirmable?"invalid_confirmation":input.ExpectedCheckKey!=key?"confirmation_changed":overall;
        if(acknowledged) checks=checks.Select(c=>c.Status=="confirmation_required"?c with {Status="confirmed_variance"}:c).ToList();
        var warnings=new List<string> {"Footing evidence does not authorize import or certify report coverage or stored SQL values."};
        if(unknownFormula) warnings.Add("Binary XLS formula presence is unknown; no formula-cache completeness certification is given.");
        return new(overall,context,input.Validation.ControlTotalRow,key,
            overall is "matched" or "rounding_tolerated" or "confirmed_variance",false,fullSummary,selectedSummary,
            checks,validation,issueCount,issues,issueCount>issues.Count,acknowledged,warnings);
    }
}
