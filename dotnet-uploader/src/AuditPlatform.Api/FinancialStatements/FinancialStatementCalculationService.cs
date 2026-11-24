using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Reports;
using AuditPlatform.Api.TrialBalance;
using Amount = AuditPlatform.Api.Uploads.UploadExactAmount;

namespace AuditPlatform.Api.FinancialStatements;

/// <summary>Builds ERP-only statements from one complete P53A input snapshot.</summary>
internal sealed class FinancialStatementCalculationService
{
    private static readonly Amount Tolerance = Amount.Parse("0.01");

    private sealed class CategoryState(ReportCategory source)
    {
        public ReportCategory Source { get; } = source;
        public Amount Raw;
        public int AccountCount;
        public Amount? Value;
        public Amount? ErpValue;
        public Amount? LinkedProfit;
        public string? EffectiveRole;
        public string? EffectiveIncomeRole;
    }

    private sealed class FormulaState(ReportStatementCalculation source)
    {
        public ReportStatementCalculation Source { get; } = source;
        public Amount? Value;
        public List<FinancialStatementFormulaComponent> Components { get; set; } = [];
        public HashSet<int> Members { get; set; } = [];
        public Dictionary<int, int> Coefficients { get; set; } = [];
    }

    private sealed record BuildState(FinancialStatementView View,
        Dictionary<int, CategoryState> Categories, Dictionary<int, FormulaState> Formulas,
        List<FinancialStatementOpeningIncomeItem> OpeningItems, Amount OpeningIncomeNet,
        Amount PeriodIncomeNet, Amount IncomeNet, Amount TotalDebit, Amount TotalCredit,
        int IncomeAccountCount, int UnmappedNonzeroCount, int UnmappedCount, Amount UnmappedNet,
        int LeafCount, IReadOnlyList<FinancialStatementDiagnostic> Diagnostics);

    private sealed record TransferSide(int? Id, string Label, Dictionary<int, int> Coefficients);

    public FinancialStatementResult Calculate(ReportInputSnapshot snapshot, FinancialStatementSection section,
        CancellationToken cancellationToken = default)
    {
        try{return CalculateCore(snapshot,section,cancellationToken);}
        catch(FormatException)
        {throw new FinancialStatementInputException("Financial Statement amounts cannot be evaluated safely; review source precision and values.");}
    }

    private FinancialStatementResult CalculateCore(ReportInputSnapshot snapshot, FinancialStatementSection section,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        if (snapshot.Scope.Kind != ReportInputKind.FinancialStatements)
            throw new FinancialStatementInputException("Financial Statement inputs are unavailable for this report scope.");
        Require(snapshot.TrialBalanceUploads, "selected Trial Balance");
        Require(snapshot.TrialBalanceAccounts, "Trial Balance accounts");
        Require(snapshot.TrialBalanceAdjustments, "Trial Balance adjustments");
        Require(snapshot.AccountAssignments, "account assignments");
        Require(snapshot.Categories, "Financial Statement categories");
        Require(snapshot.StatementCalculations, "statement calculations");
        Require(snapshot.StatementComponents, "statement calculation components");
        Require(snapshot.ProfitLinks, "profit-link configuration");
        Require(snapshot.ProfitStatuses, "Trial Balance closing confirmation");

        var upload = snapshot.TrialBalanceUploads.Items.SingleOrDefault()
            ?? throw new FinancialStatementInputException("The selected Trial Balance is unavailable for this client.");
        if (upload.Id != snapshot.Scope.TrialBalanceUploadId)
            throw new FinancialStatementInputException("The selected Trial Balance does not match the captured report scope.");
        var accounts = snapshot.TrialBalanceAccounts.Items;
        var assignments = snapshot.AccountAssignments.Items;
        var categories = snapshot.Categories.Items
            .Where(row => row.CategoryType == "financial_statement")
            .ToArray();
        var calculations = snapshot.StatementCalculations.Items;
        var components = snapshot.StatementComponents.Items;
        var profitLink = snapshot.ProfitLinks.Items.SingleOrDefault();
        var controls = ReadControls(upload.ValidationControlsJson);
        var status = snapshot.ProfitStatuses.Items.SingleOrDefault(row => row.UploadId == upload.Id);
        string contextKey = FinancialStatementContextKey(accounts, assignments, categories, calculations,
            components, profitLink);
        string closingStatus = StringComparer.Ordinal.Equals(contextKey, status?.ContextKey)
            ? status?.ClosingStatus ?? "not_confirmed" : "not_confirmed";

        var income = Build(accounts, assignments, categories, calculations, components, controls,
            snapshot.TrialBalanceAdjustments.Items.Count, "income_statement", "closing", null, cancellationToken);
        var profitInput = new ProfitConfirmationInput(
            profitLink is not null,
            profitLink?.SourceCalculationId,
            profitLink?.TargetCategoryId,
            IsActiveEquityDestination(categories, profitLink?.TargetCategoryId),
            income.Formulas.Values.OrderBy(row => row.Source.SortOrder).ThenBy(row => row.Source.Id)
                .Select(row => new ProfitConfirmationCalculation(row.Source.Id,row.Source.CalculationType,
                    Text(row.Value),row.Members.Order().ToArray())).ToArray(),
            income.Categories.Values.Select(row => new ProfitConfirmationCategory(row.Source.Id,
                row.Source.RowType,Text(row.Raw))).ToArray(),
            Text(income.IncomeNet),income.UnmappedNonzeroCount, status?.ClosingStatus,
            contextKey,status?.ContextKey);
        var profitResult = ProfitConfirmationContext.Evaluate(profitInput);
        var profit = ToResult(profitResult);
        if (section == FinancialStatementSection.IncomeStatement)
        {
            var view = AddProfitDiagnostic(income.View,profit,null);
            return new(snapshot.Scope.ClientId,upload.Id,contextKey,profit.ClosingStatus,view,profit,null,
                null,null,null);
        }

        Amount? linkedProfit = profit.Applied && profit.FinalProfit is not null
            ? Amount.Parse(profit.FinalProfit) : null;
        int? targetCategoryId = profit.Applied ? profit.TargetCategoryId : null;
        var current = Build(accounts,assignments,categories,calculations,components,controls,
            snapshot.TrialBalanceAdjustments.Items.Count,"balance_sheet","closing",
            linkedProfit is not null && targetCategoryId is not null ? (targetCategoryId.Value,linkedProfit.Value) : null,
            cancellationToken);
        var currentView = AddProfitDiagnostic(current.View,profit,null);
        if (current.OpeningItems.Count > 0 && profit.Applied)
            currentView = ReplaceProfitDiagnostic(currentView,
                "Closing-balance profit/loss (including opening I/S) linked once to capital: " + profit.FinalProfit);
        var currentTransfer = BuildTransfer(current,income,opening:false,profit,cancellationToken);

        Amount openingNet=current.OpeningIncomeNet;
        bool validDestination=IsActiveEquityDestination(categories,profitLink?.TargetCategoryId);
        var comparative=Build(accounts,assignments,categories,calculations,components,controls,
            snapshot.TrialBalanceAdjustments.Items.Count,"balance_sheet","opening",
            current.OpeningItems.Count>0 && validDestination && profitLink?.TargetCategoryId is { } target
                ? (target,openingNet) : null,cancellationToken);
        int? openingTarget=validDestination?profitLink?.TargetCategoryId:null;
        var openingReview=new FinancialStatementOpeningReview(Text(openingNet)!,current.OpeningItems.Count,
            openingTarget,current.OpeningItems.Count>0&&validDestination,current.OpeningItems);
        var comparativeView=RemoveDiagnostic(comparative.View,"unclosed_profit_loss");
        if(current.OpeningItems.Count>0)
            comparativeView=AppendDiagnostic(comparativeView,new FinancialStatementDiagnostic("opening_income_review",
                validDestination
                    ? "Opening I/S items need review; the separate draft equity line is included once."
                    : "Opening I/S items are not included in totals: select an active equity destination in ACC."));
        var openingTransfer=BuildTransfer(comparative,income,opening:true,profit,cancellationToken);
        var bridgeMovement=current.IncomeNet-openingNet;
        var erpPeriod=current.PeriodIncomeNet;
        var bridgeDifference=bridgeMovement-erpPeriod;
        var bridge=new FinancialStatementProfitBridge(Text(openingNet)!,Text(bridgeMovement)!,Text(erpPeriod)!,
            Text(current.IncomeNet)!,Text(bridgeDifference)!,bridgeDifference.Abs().CompareTo(Tolerance)<=0);
        currentTransfer=currentTransfer with { ProfitBridge=bridge,OpeningReviewRequired=current.OpeningItems.Count>0 };
        openingTransfer=openingTransfer with { OpeningReviewRequired=current.OpeningItems.Count>0 };
        if(current.OpeningItems.Count>0)
        {
            currentTransfer=AppendOpeningReviewLabel(currentTransfer);
            openingTransfer=AppendOpeningReviewLabel(openingTransfer);
        }
        return new(snapshot.Scope.ClientId,upload.Id,contextKey,profit.ClosingStatus,currentView,profit,
            currentTransfer,comparativeView,openingReview,bridge);
    }

    internal static string FinancialStatementContextKey(
        IReadOnlyList<ReportTrialBalanceAccount> accounts,
        IReadOnlyList<ReportAccountAssignment> assignments,
        IReadOnlyList<ReportCategory> categories,
        IReadOnlyList<ReportStatementCalculation> calculations,
        IReadOnlyList<ReportStatementComponent> components,
        ReportProfitLink? profitLink)
    {
        var groups=new List<IReadOnlyList<SortedDictionary<string,object?>>>
        {
            accounts.Select(row=>Object(
                ("account_code",(object?)row.AccountCode),("classification",row.Classification),
                ("manually_overridden",row.ManuallyOverridden),
                ("opening_dr",RawAmount(row.OpeningDebit)),("opening_cr",RawAmount(row.OpeningCredit)),
                ("period_dr",RawAmount(row.PeriodDebit)),("period_cr",RawAmount(row.PeriodCredit)),
                ("closing_dr",RawAmount(row.ClosingDebit)),("closing_cr",RawAmount(row.ClosingCredit)))).ToList(),
            assignments.Select(row=>Object(("account_code",(object?)row.AccountCode),
                ("financial_statement_category_id",row.FinancialStatementCategoryId))).ToList(),
            categories.Select(row=>Object(("id",(object?)row.Id),("label",row.Label),
                ("statement_section",row.StatementSection),("row_type",row.RowType),
                ("parent_category_id",row.ParentCategoryId),("presentation_role",row.PresentationRole),
                ("sort_order",row.SortOrder),("is_active",row.IsActive))).ToList(),
            calculations.Select(row=>Object(("id",(object?)row.Id),("label",row.Label),
                ("statement_section",row.StatementSection),("calculation_type",row.CalculationType),
                ("parent_heading_id",row.ParentHeadingId),("sort_order",row.SortOrder))).ToList(),
            components.Select(row=>Object(("calculation_id",(object?)row.CalculationId),
                ("source_category_id",row.SourceCategoryId),("source_calculation_id",row.SourceCalculationId),
                ("operator",row.Operator),("sort_order",row.SortOrder))).ToList()
        };
        var ordered=new List<IReadOnlyList<SortedDictionary<string,object?>>>(groups.Count);
        foreach(var group in groups)
            ordered.Add(group.OrderBy(row=>CanonicalJson(row),StringComparer.Ordinal).ToArray());
        var payload=new List<object?>
        {
            ordered[0],ordered[1],ordered[2],ordered[3],ordered[4],
            profitLink is null ? new SortedDictionary<string,object?>(StringComparer.Ordinal)
                : Object(("source_calculation_id",(object?)profitLink.SourceCalculationId),
                    ("target_category_id",profitLink.TargetCategoryId))
        };
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(CanonicalJson(payload)))).ToLowerInvariant();
    }

    private static SortedDictionary<string,object?> Object(params (string Key,object? Value)[] values)
    {
        var row=new SortedDictionary<string,object?>(StringComparer.Ordinal);
        foreach(var (key,value) in values)row.Add(key,value);
        return row;
    }

    private sealed record RawJsonNumber(string Value);
    private static object? RawAmount(ReportAmountValue? value)
        =>value is null?null:new RawJsonNumber(value.StorageType=="real"
            ?FormatStoredNumber(value.RawValue):value.RawValue);

    // Stored fingerprints use floating-point values, including integral REAL values
    // such as 1.0 rather than 1.
    private static string FormatStoredNumber(string token)
    {
        double value=double.Parse(token,NumberStyles.Float,CultureInfo.InvariantCulture);
        if(!double.IsFinite(value))throw new FormatException("Non-finite report amount.");
        bool negative=BitConverter.DoubleToInt64Bits(value)<0;
        double absolute=Math.Abs(value);
        if(absolute==0)return negative?"-0.0":"0.0";
        string shortest=absolute.ToString("R",CultureInfo.InvariantCulture);
        int exponentMarker=shortest.IndexOfAny(['e','E']);
        string mantissa=exponentMarker<0?shortest:shortest[..exponentMarker];
        int explicitExponent=exponentMarker<0?0:int.Parse(shortest[(exponentMarker+1)..],CultureInfo.InvariantCulture);
        int decimalPoint=mantissa.IndexOf('.');
        if(decimalPoint<0)decimalPoint=mantissa.Length;
        string rawDigits=mantissa.Replace(".",string.Empty,StringComparison.Ordinal);
        int leadingZeros=rawDigits.Length-rawDigits.TrimStart('0').Length;
        string digits=rawDigits.TrimStart('0').TrimEnd('0');
        if(digits.Length==0)digits="0";
        int power=explicitExponent+decimalPoint-leadingZeros-1;
        string sign=negative?"-":string.Empty;
        if(power< -4||power>=16)
        {
            string fractional=digits.Length>1?"."+digits[1..]:string.Empty;
            return sign+digits[..1]+fractional+"e"+(power>=0?"+":"-")+Math.Abs(power).ToString("D2",CultureInfo.InvariantCulture);
        }
        int point=power+1;
        string fixedValue=point<=0?"0."+new string('0',-point)+digits
            :point>=digits.Length?digits+new string('0',point-digits.Length)+".0"
            :digits.Insert(point,".");
        return sign+fixedValue;
    }

    private static string CanonicalJson(object? value)
    {
        var output=new StringBuilder();WriteCanonicalJson(output,value);return output.ToString();
    }

    private static void WriteCanonicalJson(StringBuilder output,object? value)
    {
        switch(value)
        {
            case null: output.Append("null");break;
            case RawJsonNumber number:
                _=Amount.Parse(number.Value);output.Append(number.Value);break;
            case string text: AppendCanonicalString(output,text);break;
            case bool boolean: output.Append(boolean?"true":"false");break;
            case int integer: output.Append(integer.ToString(CultureInfo.InvariantCulture));break;
            case long longInteger: output.Append(longInteger.ToString(CultureInfo.InvariantCulture));break;
            case SortedDictionary<string,object?> map:
                output.Append('{');bool first=true;
                foreach(var pair in map)
                {if(!first)output.Append(',');first=false;AppendCanonicalString(output,pair.Key);output.Append(':');WriteCanonicalJson(output,pair.Value);}
                output.Append('}');break;
            case System.Collections.IEnumerable sequence when value is not string:
                output.Append('[');bool firstItem=true;
                foreach(var item in sequence)
                {if(!firstItem)output.Append(',');firstItem=false;WriteCanonicalJson(output,item);}
                output.Append(']');break;
            default: throw new InvalidOperationException("Unsupported value in Financial Statement context identity.");
        }
    }

    private static void AppendCanonicalString(StringBuilder output,string value)
    {
        output.Append('"');
        foreach(char ch in value)
        {
            switch(ch)
            {
                case '"': output.Append("\\\"");break;
                case '\\': output.Append("\\\\");break;
                case '\b': output.Append("\\b");break;
                case '\f': output.Append("\\f");break;
                case '\n': output.Append("\\n");break;
                case '\r': output.Append("\\r");break;
                case '\t': output.Append("\\t");break;
                default:
                    if(ch<0x20||ch>0x7e)output.Append("\\u").Append(((int)ch).ToString("x4",CultureInfo.InvariantCulture));
                    else output.Append(ch);
                    break;
            }
        }
        output.Append('"');
    }

    private BuildState Build(IReadOnlyList<ReportTrialBalanceAccount> accounts,
        IReadOnlyList<ReportAccountAssignment> assignments,IReadOnlyList<ReportCategory> allCategories,
        IReadOnlyList<ReportStatementCalculation> calculations,IReadOnlyList<ReportStatementComponent> components,
        JsonElement controls,int adjustmentCount,string section,string basis,(int CategoryId,Amount Amount)? linkedProfit,
        CancellationToken ct)
    {
        var hierarchy=TrialBalanceValidation.Hierarchy(accounts.Select(row=>row.AccountCode),ct);
        var accountByCode=new HashSet<string>(StringComparer.Ordinal);
        var assignmentByCode=new Dictionary<string,int?>(StringComparer.Ordinal);
        foreach(var row in assignments)
            if(!assignmentByCode.TryAdd(row.AccountCode.Trim(),row.FinancialStatementCategoryId))
                throw new FinancialStatementInputException("Duplicate Trial Balance account mappings require review.");
        var categories=new Dictionary<int,CategoryState>();
        foreach(var row in allCategories.Where(row=>row.StatementSection==section)
                    .OrderBy(row=>row.SortOrder).ThenBy(row=>row.Label,StringComparer.OrdinalIgnoreCase)
                    .ThenBy(row=>row.CategoryKey,StringComparer.Ordinal))
            if(!categories.TryAdd(row.Id,new CategoryState(row)))
                throw new FinancialStatementInputException("Duplicate Financial Statement category IDs require review.");
        var byAllCategoryId=allCategories.ToDictionary(row=>row.Id);
        var unmapped=new List<string>();var inactive=new List<string>();var missingRoles=new List<string>();
        var abnormal=new List<string>();var openingItems=new List<FinancialStatementOpeningIncomeItem>();
        var totalDebit=Amount.Zero;var totalCredit=Amount.Zero;var incomeNet=Amount.Zero;
        var openingIncomeNet=Amount.Zero;var periodIncomeNet=Amount.Zero;var unmappedNet=Amount.Zero;
        var incomeAccountCount=0;var unmappedNonzeroCount=0;var leafCount=0;var rollforwardCount=0;
        var currentClassifications=new List<(string Code,string Classification)>();
        foreach(var account in accounts)
        {
            ct.ThrowIfCancellationRequested();string code=account.AccountCode.Trim();
            if(code.Length==0||!accountByCode.Add(code))
                throw new FinancialStatementInputException("Duplicate or blank Trial Balance account codes require review.");
            string classification=account.ManuallyOverridden?account.Classification:hierarchy[code].Classification;
            currentClassifications.Add((code,classification));
            if(classification!="leaf")continue;
            leafCount++;
            Amount debit=ReadAmount(basis switch{"opening"=>account.OpeningDebit,"closing"=>account.ClosingDebit,_=>account.PeriodDebit});
            Amount credit=ReadAmount(basis switch{"opening"=>account.OpeningCredit,"closing"=>account.ClosingCredit,_=>account.PeriodCredit});
            Amount net=debit-credit;totalDebit+=debit;totalCredit+=credit;
            Amount expected=ReadAmount(account.OpeningDebit)-ReadAmount(account.OpeningCredit)
                +ReadAmount(account.PeriodDebit)-ReadAmount(account.PeriodCredit);
            if(basis=="closing"&&(expected-net).Abs().CompareTo(Tolerance)>0)rollforwardCount++;
            int? categoryId=assignmentByCode.GetValueOrDefault(code);
            var category=categoryId is { } id?byAllCategoryId.GetValueOrDefault(id):null;
            if(category is null||category.RowType!="category"||category.StatementSection is not("balance_sheet" or "income_statement"))
            {
                unmapped.Add(code);unmappedNet+=net;
                if(net.CompareTo(Amount.Zero)!=0)unmappedNonzeroCount++;
                continue;
            }
            if(!category.IsActive)inactive.Add(code);
            if(category.StatementSection=="income_statement")
            {
                incomeAccountCount++;incomeNet-=net;
                Amount openingDebit=ReadAmount(account.OpeningDebit),openingCredit=ReadAmount(account.OpeningCredit);
                openingIncomeNet+=openingCredit-openingDebit;
                periodIncomeNet+=ReadAmount(account.PeriodCredit)-ReadAmount(account.PeriodDebit);
                if((openingDebit-openingCredit).CompareTo(Amount.Zero)!=0)
                    openingItems.Add(new(code,category.Label,openingDebit.ToFixedString(),openingCredit.ToFixedString(),(openingCredit-openingDebit).ToFixedString()));
            }
            if(categories.TryGetValue(category.Id,out var state))
            {state.Raw+=net;state.AccountCount++;}
        }
        var categoryValues=new Dictionary<int,Amount?>();
        foreach(var state in categories.Values)
        {
            ct.ThrowIfCancellationRequested();var row=state.Source;
            if(row.RowType!="category")
            {
                state.Value=null;state.ErpValue=null;
                continue;
            }
            ReportCategory? parent=row.ParentCategoryId is { } parentId?byAllCategoryId.GetValueOrDefault(parentId):null;
            if(parent is not null&&parent.RowType=="heading"&&parent.StatementSection=="balance_sheet")
                state.EffectiveRole=parent.PresentationRole;
            else if(row.ParentCategoryId is null)state.EffectiveRole=row.PresentationRole;
            if(section=="income_statement")
            {
                state.EffectiveIncomeRole=row.IncomeStatementRole;
                if(state.EffectiveIncomeRole is null&&parent is not null&&parent.RowType=="heading"&&parent.StatementSection=="income_statement")
                    state.EffectiveIncomeRole=parent.IncomeStatementRole;
            }
            Amount? value=section=="income_statement"?Amount.Zero-state.Raw:
                state.EffectiveRole is "asset" or "liability" or "equity"
                    ?state.EffectiveRole=="asset"?state.Raw:Amount.Zero-state.Raw:null;
            state.ErpValue=value;
            if(linkedProfit is { } link&&link.CategoryId==row.Id&&value is not null)
            {value+=link.Amount;state.LinkedProfit=link.Amount;}
            state.Value=value;categoryValues[row.Id]=value;
            if(value is null)missingRoles.Add(row.Label);
            else if(value.Value.CompareTo(Amount.Zero)<0)abnormal.Add(row.Label);
        }
        foreach(var heading in allCategories.Where(row=>row.StatementSection=="balance_sheet"&&row.RowType=="heading"))
        {
            foreach(var child in categories.Values.Where(value=>value.Source.ParentCategoryId==heading.Id))
                child.EffectiveRole=heading.PresentationRole;
        }
        var formulaStates=calculations.Where(row=>row.StatementSection==section)
            .OrderBy(row=>row.SortOrder).ThenBy(row=>row.Id)
            .ToDictionary(row=>row.Id,row=>new FormulaState(row));
        var saved=components.GroupBy(row=>row.CalculationId).ToDictionary(group=>group.Key,group=>group
            .OrderBy(row=>row.SortOrder).ToArray());
        var computed=new HashSet<int>();var visiting=new HashSet<int>();var problems=new List<string>();
        var subtractionReviews=new HashSet<string>(StringComparer.Ordinal);
        Amount? Evaluate(int id)
        {
            ct.ThrowIfCancellationRequested();
            if(!formulaStates.TryGetValue(id,out var formula))
                throw new FinancialStatementInputException($"{Name(section)}: a total has a missing source.");
            if(computed.Contains(id))return formula.Value;
            if(!visiting.Add(id))throw new FinancialStatementInputException($"{Name(section)}: a total has a circular source.");
            var parts=saved.GetValueOrDefault(id,[]).ToList();
            if(formula.Source.CalculationType=="subtotal")
            {
                var operators=parts.Where(part=>part.SourceCategoryId is not null)
                    .GroupBy(part=>part.SourceCategoryId!.Value).ToDictionary(group=>group.Key,group=>group.Last().Operator);
                parts=categories.Values.Where(row=>row.Source.RowType=="category"&&row.Source.ParentCategoryId==formula.Source.ParentHeadingId)
                    .Select(row=>new ReportStatementComponent(id,row.Source.Id,null,
                        operators.GetValueOrDefault(row.Source.Id,"add"),row.Source.SortOrder)).ToList();
            }
            var outputParts=new List<FinancialStatementFormulaComponent>();var covered=new HashSet<int>();
            var coefficients=new Dictionary<int,int>();Amount sum=Amount.Zero;bool complete=parts.Count>0;
            foreach(var part in parts)
            {
                ct.ThrowIfCancellationRequested();
                if((part.SourceCategoryId is null)==(part.SourceCalculationId is null)||part.Operator is not("add" or "subtract"))
                    throw new FinancialStatementInputException($"{Name(section)}: a total has an invalid source or operator.");
                Amount? sourceValue;HashSet<int> members;Dictionary<int,int> sourceWeights;
                if(part.SourceCategoryId is { } categoryId)
                {
                    if(!categoryValues.TryGetValue(categoryId,out sourceValue))
                        throw new FinancialStatementInputException($"{Name(section)}: a total references an unavailable category.");
                    members=[categoryId];sourceWeights=new(){{categoryId,1}};
                    if(sourceValue is { } categoryAmount&&categoryAmount.CompareTo(Amount.Zero)<0&&part.Operator=="subtract")
                        subtractionReviews.Add(byAllCategoryId[categoryId].Label);
                }
                else
                {
                    int sourceId=part.SourceCalculationId!.Value;
                    sourceValue=Evaluate(sourceId);
                    if(!formulaStates.TryGetValue(sourceId,out var sourceFormula))
                        throw new FinancialStatementInputException($"{Name(section)}: a total references an unavailable calculation.");
                    members=sourceFormula.Members;sourceWeights=sourceFormula.Coefficients;
                }
                if(covered.Overlaps(members))
                    throw new FinancialStatementInputException($"{Name(section)}: a total includes an account category more than once.");
                covered.UnionWith(members);int factor=part.Operator=="subtract"?-1:1;
                foreach(var pair in sourceWeights)coefficients[pair.Key]=pair.Value*factor;
                outputParts.Add(new(part.SourceCategoryId,part.SourceCalculationId,part.Operator));
                if(sourceValue is null)complete=false;
                else sum+=factor<0?Amount.Zero-sourceValue.Value:sourceValue.Value;
            }
            visiting.Remove(id);computed.Add(id);formula.Value=complete?sum:null;formula.Members=covered;
            formula.Coefficients=coefficients;formula.Components=outputParts;
            if(!complete)problems.Add(formula.Source.Label);
            return formula.Value;
        }
        foreach(var formula in formulaStates.Values)formula.Value=Evaluate(formula.Source.Id);

        var diagnostics=new List<FinancialStatementDiagnostic>();
        if(leafCount==0)
        {
            diagnostics.Add(new("no_leaf_accounts","The selected Trial Balance has no leaf accounts; amounts are unavailable."));
            foreach(var state in categories.Values)state.Value=null;
            foreach(var state in formulaStates.Values)state.Value=null;
        }
        if(unmapped.Count>0)diagnostics.Add(new("unmapped_accounts",$"{unmapped.Count} leaf accounts have no usable Financial Statement assignment. Review them in ACC.",unmapped.Count));
        if(inactive.Count>0)diagnostics.Add(new("inactive_assignments",$"{inactive.Count} accounts use inactive categories; their balances remain included for review.",inactive.Count));
        if(missingRoles.Count>0)
        {
            string names=string.Join("; ",missingRoles.Take(20));
            diagnostics.Add(new("missing_presentation","Set Asset, Liability or Equity in ACC for: "+names,missingRoles.Count));
        }
        Amount basisDifference=totalDebit-totalCredit;
        if(basisDifference.Abs().CompareTo(Tolerance)>0)
            diagnostics.Add(new("source_imbalance",$"ERP {basis} debits and credits do not balance. Difference: {basisDifference.ToFixedString()}"));
        int globalCount=GlobalAdjustmentCount(controls);
        if(basis=="closing"&&(adjustmentCount>0||globalCount>0||rollforwardCount>0))
            diagnostics.Add(new("excluded_differences",$"Automatic TB differences are excluded: {adjustmentCount} saved leaf entries, {globalCount} global entries; {rollforwardCount} current leaf roll-forward differences require review."));
        if(ControlsAreStale(controls,currentClassifications))
            diagnostics.Add(new("stale_controls","Stored TB validation controls are stale; revalidate the source."));
        if(section=="balance_sheet"&&incomeNet.CompareTo(Amount.Zero)!=0)
            diagnostics.Add(new("unclosed_profit_loss","Income Statement account net (credit-positive): "+incomeNet.ToFixedString()+". Review closing status; this amount is not automatically added to equity."));
        if(section=="balance_sheet"&&abnormal.Count>0)
            diagnostics.Add(new("negative_presented_balances","Negative presented balances retained for review: "+string.Join("; ",abnormal.Take(20)),abnormal.Count));
        if(subtractionReviews.Count>0)
            diagnostics.Add(new("subtract_negative","ACC subtracts already-negative categories; review these signs: "+string.Join("; ",subtractionReviews.Order(StringComparer.Ordinal).Take(20))));
        if(section=="income_statement")
        {
            int openingCount=accounts.Count(account=>IsLeaf(account,hierarchy)&&
                assignmentByCode.TryGetValue(account.AccountCode.Trim(),out var id)&&id is { } categoryId&&
                byAllCategoryId.TryGetValue(categoryId,out var category)&&category.StatementSection=="income_statement"&&
                (ReadAmount(account.OpeningDebit)-ReadAmount(account.OpeningCredit)).CompareTo(Amount.Zero)!=0);
            if(openingCount>0)diagnostics.Add(new("opening_profit_balances",$"{openingCount} P&L accounts have nonzero opening balances. Closing-balance profit may include earlier periods; review the reporting period.",openingCount));
        }
        if(problems.Count>0)diagnostics.Add(new("incomplete_totals","Some totals are unavailable because their sources are incomplete or unconfigured.",problems.Count));
        var categoryOutput=categories.Values.Select(state=>new FinancialStatementCategory(state.Source.Id,
            state.Source.Label,state.Source.StatementSection!,state.Source.RowType,state.Source.ParentCategoryId,
            state.Source.SortOrder,state.Source.IsActive,state.Source.CategoryKey,state.Source.PresentationRole,
            state.Source.IncomeStatementRole,state.Source.RowType=="category"?Text(state.Raw):null,Text(state.Value),state.AccountCount,
            state.EffectiveRole,state.EffectiveIncomeRole,Text(state.ErpValue),Text(state.LinkedProfit))).ToArray();
        var formulaOutput=formulaStates.Values.OrderBy(row=>row.Source.SortOrder).ThenBy(row=>row.Source.Id)
            .Select(state=>new FinancialStatementFormula(state.Source.Id,state.Source.Label,state.Source.StatementSection,
                state.Source.CalculationType,state.Source.ParentHeadingId,state.Source.SortOrder,Text(state.Value),
                state.Components,state.Members.Order().ToArray(),state.Coefficients.OrderBy(pair=>pair.Key)
                    .ToDictionary(pair=>pair.Key,pair=>pair.Value))).ToArray();
        string selectedBasis=section=="income_statement"?"closing":basis;
        var controlOutput=new FinancialStatementControls("erp_"+selectedBasis+"_before_audit_adjustments",
            totalDebit.ToFixedString(),totalCredit.ToFixedString(),basisDifference.ToFixedString(),leafCount,
            openingIncomeNet.ToFixedString(),periodIncomeNet.ToFixedString(),unmappedNet.ToFixedString(),
            incomeAccountCount,unmapped.Count,unmappedNonzeroCount,unmapped.Take(50).ToArray(),
            incomeNet.ToFixedString(),"0");
        var view=new FinancialStatementView("erp_"+basis+"_before_audit_adjustments",section,
            categoryOutput,formulaOutput,diagnostics,openingItems,controlOutput);
        return new(view,categories,formulaStates,openingItems,openingIncomeNet,periodIncomeNet,incomeNet,
            totalDebit,totalCredit,incomeAccountCount,unmappedNonzeroCount,unmapped.Count,unmappedNet,
            leafCount,diagnostics);
    }

    private static bool IsLeaf(ReportTrialBalanceAccount account,
        IReadOnlyDictionary<string,(string Classification,string? Parent)> hierarchy)
        =>(account.ManuallyOverridden?account.Classification:hierarchy[account.AccountCode.Trim()].Classification)=="leaf";

    private static void Require<T>(ReportInputDataset<T> dataset,string label)
    {if(!dataset.Requested||!dataset.Complete)throw new FinancialStatementInputException($"The complete {label} snapshot is unavailable.");}

    private static JsonElement ReadControls(string? json)
    {
        if(string.IsNullOrWhiteSpace(json))return JsonDocument.Parse("{}").RootElement.Clone();
        try
        {
            using var document=JsonDocument.Parse(json);
            if(document.RootElement.ValueKind!=JsonValueKind.Object)throw new JsonException();
            return document.RootElement.Clone();
        }
        catch(JsonException){throw new FinancialStatementInputException("Trial Balance validation controls cannot be read; review the source.");}
    }

    private static int GlobalAdjustmentCount(JsonElement controls)
    {
        if(!controls.TryGetProperty("global_adjustments",out var rows)||rows.ValueKind==JsonValueKind.Null)return 0;
        if(rows.ValueKind!=JsonValueKind.Array)throw new FinancialStatementInputException("Trial Balance validation controls cannot be read; review the source.");
        return rows.GetArrayLength();
    }

    private static bool ControlsAreStale(JsonElement controls,IReadOnlyList<(string Code,string Classification)> current)
    {
        if(controls.TryGetProperty("stale",out var stale)&&stale.ValueKind==JsonValueKind.True)return true;
        if(!controls.TryGetProperty("account_classifications",out var saved)||saved.ValueKind!=JsonValueKind.Array||saved.GetArrayLength()==0)return false;
        var expected=current.OrderBy(row=>row.Code,StringComparer.Ordinal).ToArray();
        if(saved.GetArrayLength()!=expected.Length)return true;
        int index=0;
        foreach(var row in saved.EnumerateArray())
        {
            if(row.ValueKind!=JsonValueKind.Array||row.GetArrayLength()!=2)return true;
            var cells=row.EnumerateArray().ToArray();
            if(cells[0].ValueKind!=JsonValueKind.String||cells[1].ValueKind!=JsonValueKind.String
                ||cells[0].GetString()!=expected[index].Code||cells[1].GetString()!=expected[index].Classification)return true;
            index++;
        }
        return false;
    }

    private static bool IsActiveEquityDestination(IReadOnlyList<ReportCategory> categories,int? id)
    {
        if(id is null)return false;
        var byId=categories.ToDictionary(row=>row.Id);
        if(!byId.TryGetValue(id.Value,out var row)||row.StatementSection!="balance_sheet"||row.RowType!="category"||!row.IsActive)return false;
        ReportCategory? parent=row.ParentCategoryId is { } parentId?byId.GetValueOrDefault(parentId):null;
        if(row.ParentCategoryId is not null&&(parent is null||parent.RowType!="heading"||parent.StatementSection!="balance_sheet"||!parent.IsActive))return false;
        return (parent?.PresentationRole??row.PresentationRole)=="equity";
    }

    private static FinancialStatementProfitLink ToResult(ProfitConfirmationResult result)
        =>new(result.Configured,result.SourceCalculationId,result.TargetCategoryId,result.FinalProfit,
            result.TrialBalanceProfit,result.Difference,result.Reconciled,result.ClosingStatus,result.Applied,result.Reasons);

    private static FinancialStatementView AddProfitDiagnostic(FinancialStatementView view,
        FinancialStatementProfitLink profit,string? overrideMessage)
    {
        var kept=view.Diagnostics.Where(row=>row.Code!="unclosed_profit_loss").ToList();
        string message=overrideMessage??(profit.Applied
            ?"Current-period profit/loss linked once to capital: "+profit.FinalProfit
            :string.Join(" ",profit.Reasons));
        kept.Add(new("profit_link",message));
        return view with {Diagnostics=kept};
    }

    private static FinancialStatementView ReplaceProfitDiagnostic(FinancialStatementView view,string message)
        =>view with {Diagnostics=view.Diagnostics.Select(row=>row.Code=="profit_link"?row with {Message=message}:row).ToArray()};

    private static FinancialStatementView RemoveDiagnostic(FinancialStatementView view,string code)
        =>view with {Diagnostics=view.Diagnostics.Where(row=>row.Code!=code).ToArray()};

    private static FinancialStatementView AppendDiagnostic(FinancialStatementView view,FinancialStatementDiagnostic diagnostic)
        =>view with {Diagnostics=view.Diagnostics.Append(diagnostic).ToArray()};

    private static FinancialStatementTransferReconciliation BuildTransfer(BuildState balance,BuildState? income,
        bool opening,FinancialStatementProfitLink profit,CancellationToken ct)
    {
        var categoryRows=balance.Categories.Where(pair=>pair.Value.Source.RowType=="category")
            .ToDictionary(pair=>pair.Key,pair=>pair.Value);
        var roleIds=new Dictionary<string,HashSet<int>>(StringComparer.Ordinal)
        {
            ["asset"]=categoryRows.Where(pair=>pair.Value.EffectiveRole=="asset").Select(pair=>pair.Key).ToHashSet(),
            ["liability"]=categoryRows.Where(pair=>pair.Value.EffectiveRole=="liability").Select(pair=>pair.Key).ToHashSet(),
            ["equity"]=categoryRows.Where(pair=>pair.Value.EffectiveRole=="equity").Select(pair=>pair.Key).ToHashSet()
        };
        var issues=new List<string>();
        Amount SumRaw(IEnumerable<int> ids)=>ids.Aggregate(Amount.Zero,(sum,id)=>sum+categoryRows[id].Raw);
        var tb=new Dictionary<string,Amount>(StringComparer.Ordinal)
        {
            ["asset"]=SumRaw(roleIds["asset"]),["liability"]=SumRaw(roleIds["liability"]),
            ["equity"]=SumRaw(roleIds["equity"])
        };
        var recognized=roleIds.Values.SelectMany(set=>set).ToHashSet();
        var unknown=categoryRows.Keys.Where(id=>!recognized.Contains(id)).ToHashSet();
        int unknownCount=unknown.Sum(id=>categoryRows[id].AccountCount);
        tb["unmapped"]=balance.UnmappedNet+SumRaw(unknown);
        tb["profit"]=Amount.Zero-balance.IncomeNet;
        Amount sourceNet=tb.Values.Aggregate(Amount.Zero,(sum,value)=>sum+value);
        if(sourceNet.CompareTo(balance.TotalDebit-balance.TotalCredit)!=0)
            issues.Add("The source account breakdown does not match the selected TB balance control.");
        if(balance.UnmappedCount>0||unknownCount>0)
            issues.Add("Some leaf accounts are unmapped, unclassified or missing a presentation role, even if their net amount is zero.");
        if(balance.LeafCount==0)issues.Add("The selected TB has no leaf accounts.");

        TransferSide? SelectTotal(IEnumerable<string> roles,string label)
        {
            var allowed=roles.SelectMany(role=>roleIds[role]).ToHashSet();
            var required=allowed.Where(id=>categoryRows[id].AccountCount>0
                ||categoryRows[id].Raw.CompareTo(Amount.Zero)!=0
                ||(categoryRows[id].LinkedProfit?.CompareTo(Amount.Zero)??0)!=0).ToHashSet();
            var expected=required.Count>0?required:allowed;
            var candidates=balance.Formulas.Values.Where(formula=>formula.Source.CalculationType=="total"
                &&formula.Value is not null&&formula.Coefficients.Count>0
                &&formula.Coefficients.Keys.All(allowed.Contains)&&expected.IsSubsetOf(formula.Coefficients.Keys))
                .Select(formula=>new TransferSide(formula.Source.Id,formula.Source.Label,formula.Coefficients))
                .ToArray();
            var signatures=candidates.Select(candidate=>Signature(candidate.Coefficients)+"|"+
                Text(balance.Formulas[candidate.Id!.Value].Value)).Distinct(StringComparer.Ordinal).ToArray();
            if(expected.Count==0||candidates.Length==0)
            {issues.Add($"No complete final {label} total can be identified from ACC category coverage.");return null;}
            if(signatures.Length!=1)
            {issues.Add($"The final {label} total is ambiguous; review the competing ACC total formulas.");return null;}
            var selected=candidates[^1];
            if(selected.Coefficients.Values.Any(weight=>weight!=1))
                issues.Add($"The final {label} total subtracts or reverses a category; each signed category must contribute once.");
            Amount? expectedAmount=Amount.Zero;
            foreach(var pair in selected.Coefficients)
            {
                if(categoryRows[pair.Key].Value is not { } value){expectedAmount=null;break;}
                Amount contribution=pair.Value<0?Amount.Zero-value:value;
                expectedAmount+=contribution;
            }
            var reported=balance.Formulas[selected.Id!.Value].Value;
            if(expectedAmount is null||reported is null||expectedAmount.Value.CompareTo(reported.Value)!=0)
                issues.Add($"The reported {label} total differs from its category contributions.");
            return selected;
        }

        var assets=SelectTotal(["asset"],"Assets");
        var equityLiabilities=SelectTotal(["liability","equity"],"Equity and Liabilities");
        var statement=new Dictionary<string,Amount?>(StringComparer.Ordinal)
        { ["asset"]=null,["liability"]=null,["equity"]=null,["profit"]=null,["unmapped"]=null };
        if(assets is not null)
            statement["asset"]=WeightedValue(assets.Coefficients,categoryRows);
        if(equityLiabilities is not null)
        {
            var weights=equityLiabilities.Coefficients;
            statement["liability"]=WeightedValue(weights.Where(pair=>roleIds["liability"].Contains(pair.Key))
                .ToDictionary(pair=>pair.Key,pair=>pair.Value),categoryRows,reverse:true);
            statement["equity"]=WeightedValue(weights.Where(pair=>roleIds["equity"].Contains(pair.Key))
                .ToDictionary(pair=>pair.Key,pair=>pair.Value),categoryRows,reverse:true,excludeLinkedProfit:true);
            Amount profitAmount=Amount.Zero;
            foreach(var pair in weights.Where(pair=>roleIds["equity"].Contains(pair.Key)))
            {
                Amount linkAmount=categoryRows[pair.Key].LinkedProfit??Amount.Zero;
                profitAmount+=(pair.Value<0?Amount.Zero-linkAmount:linkAmount);
            }
            statement["profit"]=Amount.Zero-profitAmount;
        }

        if(opening)
        {
            bool included=balance.OpeningItems.Count>0&&balance.Categories.Values.Any(row=>row.LinkedProfit is not null);
            if(balance.OpeningItems.Count>0&&!included)
                issues.Add("Select an active Balance Sheet equity destination in ACC for the opening I/S review line.");
        }
        else if(balance.IncomeAccountCount>0)
        {
            if(income is not null&&profit.Reconciled)
            {
                var final=income.Formulas.GetValueOrDefault(profit.SourceCalculationId??-1);
                var required=income.Categories.Values.Where(row=>row.Source.RowType=="category"&&row.AccountCount>0)
                    .Select(row=>row.Source.Id).ToHashSet();
                var weights=final?.Coefficients??[];
                if(!required.IsSubsetOf(weights.Keys)||weights.Values.Any(weight=>weight!=1))
                    issues.Add("The final Income Statement result omits or reverses mapped P&L category contributions.");
                if(!profit.Applied&&!(profit.ClosingStatus=="already_in_capital"&&tb["profit"].CompareTo(Amount.Zero)==0))
                    issues.Add("Current-period profit treatment is incomplete or the closing confirmation is missing/stale.");
            }
            else issues.Add("The final Income Statement result is not configured and reconciled for the selected TB.");
        }
        foreach(string role in new[]{"asset","liability","equity","profit"})
            if(statement[role] is { } amount&&(amount-tb[role]).Abs().CompareTo(Tolerance)>0)
                issues.Add($"The {CultureInfo.InvariantCulture.TextInfo.ToTitleCase(role)} transfer differs from its original TB balance.");
        if(categoryRows.Values.Any(row=>row.Value is null))
            issues.Add("Some statement category amounts are unavailable; review ACC presentation settings.");
        if(balance.Diagnostics.Any(row=>row.Code=="stale_controls"))
            issues.Add("Stored TB validation controls are stale; revalidate before certifying completeness.");

        bool available=new[]{"asset","liability","equity","profit"}.All(role=>statement[role] is not null);
        Amount? statementNet=available?statement.Values.Where(value=>value is not null)
            .Select(value=>value!.Value).Aggregate(Amount.Zero,(sum,value)=>sum+value):null;
        Amount? unexplained=statementNet is { } total?total-sourceNet:null;
        if(unexplained is { } gap&&gap.Abs().CompareTo(Tolerance)>0)
            issues.Add("An unexplained difference remains between the statement and the original TB.");
        bool reconciled=available&&unexplained is { } difference&&difference.Abs().CompareTo(Tolerance)<=0&&issues.Count==0;
        bool sourceImbalance=sourceNet.Abs().CompareTo(Tolerance)>0;
        string status=reconciled?(sourceImbalance?"reconciled_to_tb":"reconciled"):"not_reconciled";
        string label=status switch
        {
            "reconciled_to_tb"=>"Reconciled to TB — source imbalance remains",
            "reconciled"=>"Reconciled",
            _=>"Not reconciled"
        };
        var rows=new List<FinancialStatementTransferRow>();
        foreach(var row in new[]{
            ("asset","Assets"),("liability","Liabilities"),("equity","Capital, excluding separately shown profit"),
            ("profit",opening?"Opening I/S items — need review":"Current-period profit/(loss)"),
            ("unmapped","Unmapped/unclassified balances")})
            rows.Add(new(row.Item1,row.Item2,Text(tb[row.Item1]),Text(statement[row.Item1])));
        rows.Add(new("net","Net difference",Text(sourceNet),Text(statementNet)));
        rows.Add(new("source","Original TB imbalance (reversed)",Text(Amount.Zero-sourceNet),Text(Amount.Zero-sourceNet)));
        rows.Add(new("unexplained","Unexplained transfer difference","0",Text(unexplained)));
        var totals=new Dictionary<string,FinancialStatementTransferTotal?>(StringComparer.Ordinal)
        {
            ["assets"]=assets is null?null:new(assets.Id!.Value,assets.Label,assets.Coefficients),
            ["equity_liabilities"]=equityLiabilities is null?null:new(equityLiabilities.Id!.Value,equityLiabilities.Label,equityLiabilities.Coefficients)
        };
        return new(status,label,reconciled,available&&statementNet is { } net&&net.Abs().CompareTo(Tolerance)<=0,
            Text(sourceNet)!,Text(statementNet),Text(unexplained),Tolerance.ToFixedString(),rows,
            issues.Distinct(StringComparer.Ordinal).ToArray(),totals,opening);
    }

    private static Amount? WeightedValue(IReadOnlyDictionary<int,int> weights,
        IReadOnlyDictionary<int,CategoryState> categories,bool reverse=false,bool excludeLinkedProfit=false)
    {
        Amount result=Amount.Zero;
        foreach(var pair in weights)
        {
            var row=categories[pair.Key];if(row.Value is not { } value)return null;
            if(excludeLinkedProfit)value-=row.LinkedProfit??Amount.Zero;
            Amount contribution=pair.Value<0?Amount.Zero-value:value;result+=contribution;
        }
        return reverse?Amount.Zero-result:result;
    }

    private static string Signature(IReadOnlyDictionary<int,int> weights)
        =>string.Join(",",weights.OrderBy(pair=>pair.Key).Select(pair=>$"{pair.Key}:{pair.Value}"));

    private static FinancialStatementTransferReconciliation AppendOpeningReviewLabel(FinancialStatementTransferReconciliation value)
        =>value.Reconciled?value with {Label=value.Label+" — opening I/S items need review"}:value;

    private static Amount ReadAmount(ReportAmountValue? value)
        =>value is null?Amount.Zero:value.Amount;
    private static string? Text(Amount? value)=>value?.ToFixedString();
    private static string? Text(Amount value)=>value.ToFixedString();
    private static string Name(string section)=>section=="income_statement"?"Income Statement":"Balance Sheet";
}
