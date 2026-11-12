using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Workbooks;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Uploads;

public sealed class UploadInputValidationService(UploadPreparationService preparation,IOptions<UploadInputValidationOptions> options)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    // Match pandas' default NA text coercion for validation decisions only.
    // Raw P17 cells remain unchanged; nonfinite numeric text is checked first.
    private static readonly HashSet<string> MissingText=["","#N/A","#N/A N/A","#NA","-1.#IND","-1.#QNAN","-NaN","-nan",
        "1.#IND","1.#QNAN","<NA>","N/A","NA","NULL","NaN","None","n/a","nan","null"];
    public static bool Blank(WorkbookCell cell)=>cell.Kind==WorkbookCellKind.Blank || cell.Kind==WorkbookCellKind.Text
        && (UploadNumericParser.Trim(cell.Text??"").Length==0 || MissingText.Contains(cell.Text??""));
    private static bool Marker(WorkbookCell cell)=>cell.Kind==WorkbookCellKind.Text && Regex.IsMatch(
        UploadNumericParser.Trim(cell.Text??""),@"^(?:grand\s*)?(?:sub\s*)?total\s*:?$",RegexOptions.IgnoreCase|RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100));

    public async Task<InputValidationResult> ValidateAsync(VerifiedSession? session,int file,InputValidationRequest input,CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(input.Source is null || input.ComparisonMode is not ("correction" or "replace_range")
            || input.ControlTotalRow is not (null or >=2 and <=1048576) || input.IssuePage is <1 or >1048576
            || input.IssuePageSize<1 || input.IssuePageSize>options.Value.MaximumIssueRows) return new("invalid",Code:"invalid_input_validation");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var caller=ct;using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            var sourceRequest=input.Source with {Operation="rows"};
            var result=await preparation.ReadBoundAsync(session,file,sourceRequest,(path,hash,template,saved,limits,token)=>
            {
                using var rows=WorkbookReader.Read(path,".xls",sourceRequest.Sheet,limits,token).GetEnumerator();
                if(!rows.MoveNext()) throw new WorkbookReadException("Workbook has no header row.");
                var source=UploadPreparationService.ResolveSource(rows.Current.Cells,hash,file,template,saved,sourceRequest);
                IEnumerable<WorkbookRow> Remaining() {while(rows.MoveNext()) yield return rows.Current;}
                var output=CheckRows(template,source.Mappings,source.Context,input with {Source=sourceRequest},Remaining(),options.Value,token);
                output=output with {Warnings=source.Warnings.Concat(output.Warnings).Distinct().ToArray()};
                UploadPreparationService.JsonSize(output,options.Value.MaximumResponseBytes);
                return Task.FromResult(output);
            },deadline.Token);
            return new(result.Status=="success"?(result.Value!.InputChecksPassed?"input_checks_passed":"input_checks_failed"):result.Status,result.Value,result.Code);
        }
        catch(OperationCanceledException) when(!caller.IsCancellationRequested && deadline.IsCancellationRequested)
        {return new("timeout",Code:"input_validation_timed_out");}
        catch(Exception error) when(error is WorkbookReadException or UploadPreparationService.InvalidPreparation or IOException
            or System.Xml.XmlException or ExcelDataReader.Exceptions.HeaderException or ExcelDataReader.Exceptions.InvalidPasswordException)
        {return new("invalid",Code:"invalid_validation_source");}
        finally {requests.Release();}
    }

    // Pure streaming checks, also used by golden tests. The authorized wrapper
    // above always supplies the live template/mappings and checks the source twice.
    public static InputValidationOutput CheckRows(TemplateDefinition template,IReadOnlyList<PreparedMapping> mappings,
        PreparationContext context,InputValidationRequest input,IEnumerable<WorkbookRow> rows,
        UploadInputValidationOptions limits,CancellationToken ct=default)
    {
        var issues=new List<InputRowIssue>();long detailBytes=0;int issueRows=0;
        bool blocking=false;int sourceRows=0,transactions=0,controls=0,notes=0,excludedCount=0,errorRows=0,rejected=0,forcedCount=0,eligible=0,last=1;
        bool ledger=template.Purpose=="account_ledger",complete=input.ComparisonMode=="replace_range",footing=mappings.Any(m=>m.Role=="footing");
        bool unknownFormula=false;var controlKinds=new HashSet<string>();
        var excluded=(input.Source.ExcludedRows??[]).ToHashSet();var forced=(input.Source.ForceIncludeRows??[]).ToHashSet();
        string delimiter=input.Source.DateDelimiter??"/",order=(input.Source.DateOrder??"DMY").ToUpperInvariant();
        var document=mappings.SingleOrDefault(m=>m.Role=="document_id")??mappings.FirstOrDefault();
        var date=mappings.SingleOrDefault(m=>m.Role=="document_date")??mappings.FirstOrDefault(m=>m.Type=="Date");
        var amount=mappings.FirstOrDefault(m=>m.Type=="Numeric");
        PreparedMapping? Role(string role)=>mappings.SingleOrDefault(m=>m.ColumnId==template.Ledger.GetValueOrDefault(role));
        if(ledger) {(document,date,amount)=(Role("voucher"),Role("date"),Role("debit"));}
        var mapped=mappings.Where(m=>m.SourcePosition is not null).ToArray();
        InputCellIssue Issue(PreparedMapping? m,string kind,string message)=>new(m?.ColumnId,m?.Name,m?.StorageKey,m?.SourcePosition,kind,message);
        void Report(InputRowIssue row)
        {
            issueRows++;
            long offset=(long)(input.IssuePage-1)*input.IssuePageSize;
            if(issueRows<=offset || issueRows>offset+input.IssuePageSize) return;
            try {detailBytes+=UploadPreparationService.JsonSize(row,limits.MaximumResponseBytes/2-detailBytes);issues.Add(row);}
            catch(UploadPreparationService.InvalidPreparation) { /* Full counts continue when detail bytes are exhausted. */ }
        }
        void Policy(int row,string kind,string message)
        {blocking=true;Report(new(row,"policy",[Issue(null,kind,message)],[]));}
        if(TenantTemplateService.Validate(template.Columns,template.Purpose,template.Ledger) is {} configurationError)
            Policy(1,"template_review_required",configurationError);
        var required=ledger?mappings.Where(m=>template.Ledger.ContainsValue(m.ColumnId))
            :new[]{document,date,amount}.Where(m=>m is not null).Select(m=>m!);
        foreach(var mapping in required.DistinctBy(m=>m.ColumnId).Where(m=>m.SourcePosition is null))
        {blocking=true;Report(new(1,"mapping",[Issue(mapping,"mapping_required","Map this required field before validating.")],[]));}
        if(input.ControlTotalRow is not null && !footing) Policy(1,"control_settings_changed","Review the template Footing settings and control-row choice.");
        int? parent=null;int previous=1;
        foreach(var row in rows)
        {
            ct.ThrowIfCancellationRequested();if(row.Number<=last) throw new WorkbookReadException("Source row positions disagree.");
            last=row.Number;sourceRows++;
            WorkbookCell Cell(PreparedMapping? m)=>m?.SourcePosition is >0 and var p && p<=row.Cells.Count
                ?row.Cells[p-1]:new(row.Number,m?.SourcePosition??0,WorkbookCellKind.Blank);
            bool isExcluded=excluded.Contains(row.Number);var errors=new List<InputCellIssue>();var missing=new List<InputCellIssue>();
            // Inspect original source types even on controls. Partial, uncontrolled
            // extracts alone may omit an explicitly excluded source-error row.
            if(!isExcluded || complete || footing)
            foreach(var m in mapped)
            {
                var cell=Cell(m);unknownFormula|=cell.FormulaState==WorkbookFormulaState.Unknown;
                if(cell.Kind==WorkbookCellKind.Error) errors.Add(Issue(m,cell.Error=="invalid_number"?"data_type":"excel_error",
                    cell.Error=="invalid_number"?"Enter a valid number.":"Fix the Excel formula error."));
                else if(footing && cell.FormulaState==WorkbookFormulaState.MissingResult)
                    errors.Add(Issue(m,"formula_result_missing","Recalculate and save this formula in Excel, then upload again."));
                else if(m.Type=="Numeric" && UploadNumericParser.Check(cell).NonFinite)
                    errors.Add(Issue(m,"data_type","Enter a valid number."));
            }
            bool sourceFault=errors.Count>0;
            // Pandas exposes unmapped Excel errors as empty values. Mapped
            // errors have already been recorded and can never disappear here.
            var populated=row.Cells.Where(c=>!Blank(c) && c.Kind!=WorkbookCellKind.Error).ToArray();
            bool summary=populated.Any(Marker) && (Blank(Cell(document))||Marker(Cell(document)))
                && mapped.Where(m=>m.Type=="Date"||m.Name.Contains("account",StringComparison.OrdinalIgnoreCase))
                    .All(m=>Blank(Cell(m))||Marker(Cell(m)))
                && populated.Where(c=>!Marker(c)).All(c=>c.Kind!=WorkbookCellKind.Boolean && UploadNumericParser.Check(
                    c.Kind==WorkbookCellKind.Text?c with {Text=c.Text!.Replace(",","")}:c).Valid);
            string? control=populated.Length==0?"blank_control":summary?"total_control":null;
            if(input.ControlTotalRow==row.Number && footing) control="selected_control";
            if(ledger && Blank(Cell(document)))
            {
                string label=Regex.Replace(Cell(Role("description")).Text?.ToLowerInvariant()??"","[^a-z]","");
                string? kind=label is "openingbalance" or "balanceforward" or "balancebroughtforward"?"opening":label=="closingbalance"?"closing":null;
                if(kind is not null)
                {
                    control="ledger_"+kind;
                    if(!controlKinds.Add(kind)) errors.Add(Issue(Role("balance"),"duplicate_control","Only one "+kind+" balance row is allowed."));
                    var check=UploadNumericParser.Check(Cell(Role("balance")),true,true);
                    if(!Blank(Cell(Role("balance")))&&!check.Valid) errors.Add(Issue(Role("balance"),"data_type",check.Error!));
                }
            }
            if(control is not null)
            {
                controls++;parent=null;previous=row.Number;
                if(control=="selected_control") foreach(var m in mapped.Where(m=>m.Role=="footing"))
                {var check=UploadNumericParser.Check(Cell(m));if(!Blank(Cell(m))&&!check.Valid&&!errors.Any(e=>e.ColumnId==m.ColumnId)) errors.Add(Issue(m,"data_type",check.Error!));}
                if(errors.Count>0) {blocking=true;errorRows++;}
                Report(new(row.Number,control,errors,[]));continue;
            }
            if(ledger && Cell(Role("description")).Kind==WorkbookCellKind.Text && !Blank(Cell(Role("description"))))
            {
                int description=Role("description")?.SourcePosition??0;
                if(row.Cells.Where(c=>c.Column!=description).All(c=>Blank(c)||c.Kind==WorkbookCellKind.Error))
                {
                    notes++;
                    if(parent is null || previous!=row.Number-1 || isExcluded) errors.Add(Issue(Role("description"),"ledger_note","Keep this note directly after an included transaction."));
                    if(errors.Count>0) {blocking=true;errorRows++;}
                    Report(new(row.Number,"continuation",errors,[],ParentRow:parent));previous=row.Number;continue;
                }
            }
            transactions++;
            if(isExcluded)
            {
                excludedCount++;parent=null;previous=row.Number;
                if(complete) errors.Add(Issue(null,"selection_not_allowed","Complete reports cannot exclude transaction rows."));
                if(errors.Count>0) {blocking=true;errorRows++;Report(new(row.Number,"excluded",errors,[]));}
                continue;
            }
            foreach(var m in mapped)
            {
                var cell=Cell(m);if(Blank(cell)||errors.Any(e=>e.ColumnId==m.ColumnId)) continue;
                bool ledgerNumber=ledger && new[]{"debit","credit","balance"}.Any(r=>Role(r)?.ColumnId==m.ColumnId);
                if(m.Type=="Numeric"||ledgerNumber)
                {
                    var check=UploadNumericParser.Check(cell,ledgerNumber,ledgerNumber&&Role("balance")?.ColumnId==m.ColumnId);
                    if(!check.Valid) errors.Add(Issue(m,"data_type",check.Error!));
                }
                else if(m.Type=="Date")
                    try {UploadDateParser.Parse(cell,delimiter,order);} catch(FormatException) {errors.Add(Issue(m,"data_type","Enter a valid date in the selected format."));}
            }
            foreach(var (field,label) in new[]{(document,"Document ID"),(date,"Date"),(amount,"Amount")})
            {
                if(sourceFault) continue; // Source faults are not blank-field exceptions.
                if(field?.SourcePosition is null) continue;
                bool blank=label=="Amount" && ledger?Blank(Cell(Role("debit")))&&Blank(Cell(Role("credit"))):Blank(Cell(field));
                if(blank) missing.Add(Issue(field,"critical_blank",ledger&&label=="Amount"?"Both Debit and Credit are blank.":"Missing "+label+"."));
            }
            bool force=missing.Count>0 && forced.Contains(row.Number) && !complete && !ledger && errors.Count==0;
            if(missing.Count>0) {rejected++;if(complete||ledger) blocking=true;}
            if(errors.Count>0) {errorRows++;blocking=true;}
            if(force) forcedCount++;
            if(errors.Count==0 && (missing.Count==0 || force)) eligible++;
            if(errors.Count>0||missing.Count>0) Report(new(row.Number,"transaction",errors,missing,force));
            parent=errors.Count==0 && missing.Count==0?row.Number:null;previous=row.Number;
        }
        if(excluded.Any(n=>n>last)||forced.Any(n=>n>last)||input.ControlTotalRow>last) Policy(1,"invalid_row_selection","Choose existing source row numbers.");
        if(complete && forced.Count>0) Policy(1,"override_not_allowed","Correct complete-report errors in the source workbook.");
        if(excluded.Overlaps(forced)) Policy(1,"conflicting_row_selection","A row cannot be both excluded and force-included.");
        string key=Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(new {context.SelectionKey,input.ComparisonMode,input.ControlTotalRow})));
        var warnings=new List<string> {"Input checks do not approve import, Footing, coverage, duplicate identity or ledger balance reconciliation."};
        if(unknownFormula) warnings.Add("Binary XLS formula presence is unknown; input checks do not attest formula-cache completeness.");
        return new(context,input.ComparisonMode,input.ControlTotalRow,key,true,!blocking,false,
            new(sourceRows,transactions,controls,notes,excludedCount,errorRows,rejected,forcedCount,eligible),
            issueRows,input.IssuePage,input.IssuePageSize,issues,issueRows>issues.Count,warnings);
    }
}
