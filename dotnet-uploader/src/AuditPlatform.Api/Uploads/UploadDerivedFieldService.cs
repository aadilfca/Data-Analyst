using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Workbooks;
namespace AuditPlatform.Api.Uploads;

// Pure projections plus borrowed-context reads. Source numbers remain scalar
// atoms; floating helpers are compatibility/search fields, never source totals.
public static class UploadDerivedFieldService
{
    public static UploadDerivedProfile? Bind(TemplateDefinition template,IReadOnlyDictionary<string,bool>? mapped=null)
    {
        if(template.UploadBindings is not {Count:>0} bindings) return null;
        var ids=new Dictionary<string,int>(bindings,StringComparer.Ordinal);
        ids["document"]=template.Columns.SingleOrDefault(c=>c.Role=="document_id")?.Id??template.Columns.First().Id;
        var date=template.Columns.SingleOrDefault(c=>c.Role=="document_date")??template.Columns.FirstOrDefault(c=>c.Type=="Date");
        if(date is not null) ids["date"]=date.Id;
        var keys=new Dictionary<string,string>(StringComparer.Ordinal);
        foreach(var (role,id) in ids)
        {
            var column=template.Columns.SingleOrDefault(c=>c.Id==id);
            if(column is null||!column.Active||column.Review) throw new IdentityReview("derived_binding_review_required");
            if(new[]{"amount","quantity","rate","debit","credit"}.Contains(role)&&column.Type!="Numeric") throw new IdentityReview("derived_binding_type_required");
            if(new[]{"account_code","line_id"}.Contains(role)&&column.Type!="Text") throw new IdentityReview("derived_identifier_type_required");
            keys.Add(role,column.StorageKey);
        }
        if(keys.ContainsKey("account_code")!=keys.ContainsKey("debit")||keys.ContainsKey("debit")!=keys.ContainsKey("credit")) throw new IdentityReview("derived_journal_bindings_required");
        if(keys.TryGetValue("rate",out string? rateKey)&&new[]{"amount","quantity"}.Any(r=>keys.GetValueOrDefault(r)==rateKey)
            ||keys.TryGetValue("debit",out string? debitKey)&&keys.GetValueOrDefault("credit")==debitKey) throw new IdentityReview("derived_distinct_bindings_required");
        string? rate=template.RateSource;
        if(rate is not null)
        {
            if(!keys.ContainsKey("rate")) throw new IdentityReview("derived_rate_binding_required");
            if(rate=="auto") rate=mapped?.GetValueOrDefault(keys["rate"])==true?"imported":"derived";
            if(rate=="derived"&&(!keys.ContainsKey("quantity")||!keys.ContainsKey("amount"))) throw new IdentityReview("derived_rate_inputs_required");
            if(rate is not ("derived" or "imported")) throw new IdentityReview("derived_rate_mode_required");
        }
        return new(keys,ids,rate,template.ConfigurationKey,NumericKeys:template.Columns.Where(c=>c.Type=="Numeric").OrderBy(c=>c.Order).Select(c=>c.StorageKey).ToArray());
    }

    internal static async Task<UploadDerivedProfile> LoadAsync(PostgresTransactionContext db,int tenant,int client,int template,UploadDerivedProfile profile,CancellationToken ct)
    {
        string precision="0";
        if(profile.RateMode=="derived")
        {
            await using var command=db.CreateCommand("SELECT value FROM public.platform_tenant_settings WHERE tenant_id=$1 AND key='rate_precision'");
            command.Parameters.AddWithValue(tenant);precision=(string?)await command.ExecuteScalarAsync(ct)??"0";
        }
        if(!int.TryParse(precision,NumberStyles.Integer,CultureInfo.InvariantCulture,out int places)||places is not (0 or 2 or 4)) throw new IdentityReview("derived_rate_precision_review_required");
        var treasury=new List<DerivedTreasury>();var mappings=new SortedDictionary<string,int>(StringComparer.Ordinal);int? sourceTemplate=null;
        if(profile.Keys.ContainsKey("account_code"))
        {
            await using(var command=db.CreateCommand("SELECT account_code,treasury_type FROM public.account_control_cashflow_treasury_accounts WHERE client_id=$1 AND is_active IS TRUE ORDER BY account_code,treasury_type"))
            {
                command.Parameters.AddWithValue(client);await using var reader=await command.ExecuteReaderAsync(ct);
                while(await reader.ReadAsync(ct))
                {if(treasury.Count>=10000) throw new IdentityReview("derived_configuration_capacity");treasury.Add(new(reader.GetString(0),reader.GetString(1)));}
            }
            await using(var command=db.CreateCommand("SELECT source_template_id FROM public.account_control_cashflow_settings WHERE client_id=$1"))
            {command.Parameters.AddWithValue(client);sourceTemplate=await command.ExecuteScalarAsync(ct) as int?;}
            if(sourceTemplate==template)
            {
                await using var command=db.CreateCommand("SELECT role,template_column_id FROM public.account_control_cashflow_source_mappings WHERE client_id=$1 AND template_id=$2 ORDER BY role");
                command.Parameters.AddWithValue(client);command.Parameters.AddWithValue(template);await using var reader=await command.ExecuteReaderAsync(ct);
                while(await reader.ReadAsync(ct))
                {if(mappings.Count>=32||!mappings.TryAdd(reader.GetString(0),reader.GetInt32(1))) throw new IdentityReview("derived_cashflow_binding_review_required");}
                foreach(var (role,binding) in new[]{("voucher_id","document"),("date","date"),("account_code","account_code"),("debit","debit"),("credit","credit")})
                    if(!mappings.TryGetValue(role,out int id)||profile.ColumnIds.GetValueOrDefault(binding)!=id) throw new IdentityReview("derived_cashflow_binding_review_required");
            }
        }
        string key=UploadIdentityValue.Hash(JsonSerializer.Serialize(new {Policy="native-derived-v1",tenant,client,template,profile.TemplateKey,
            Keys=profile.Keys.OrderBy(p=>p.Key,StringComparer.Ordinal),profile.RateMode,places,treasury,sourceTemplate,mappings}));
        return profile with {Precision=places,Treasury=treasury,ContextKey=key,CashflowSource=sourceTemplate==template};
    }

    private static string Token(IdentityAtom atom)
    {
        if(UploadIdentityValue.Blank(atom)) return "0";
        if(atom.Kind is not ("number" or "text")) throw new IdentityReview("derived_numeric_review_required");
        var check=UploadNumericParser.Check(new(1,1,atom.Kind=="number"?WorkbookCellKind.Number:WorkbookCellKind.Text,Text:atom.Text,NumberToken:atom.Text));
        if(!check.Valid||check.Token is null) throw new IdentityReview("derived_numeric_review_required");
        try {return UploadExactAmount.Parse(check.Token).ToFixedString();} catch(FormatException) {throw new IdentityReview("derived_precision_review_required");}
    }
    private static (BigInteger N,BigInteger D) Fraction(string token)
    {
        bool negative=token.StartsWith('-');string raw=token.TrimStart('+','-');int point=raw.IndexOf('.');int scale=point<0?0:raw.Length-point-1;
        var n=BigInteger.Parse(raw.Replace(".","",StringComparison.Ordinal),CultureInfo.InvariantCulture);
        return (negative?-n:n,BigInteger.Pow(10,scale));
    }
    private static BigInteger Round(BigInteger n,BigInteger d)
    {
        if(d.Sign<0) {n=-n;d=-d;}int sign=n.Sign;n=BigInteger.Abs(n);var quotient=BigInteger.DivRem(n,d,out var remainder);
        if(remainder*2>d||remainder*2==d&&!quotient.IsEven) quotient++;return sign<0?-quotient:quotient;
    }
    private static (BigInteger N,BigInteger D) Binary(double value)
    {
        if(!double.IsFinite(value)) throw new IdentityReview("derived_precision_review_required");
        long bits=BitConverter.DoubleToInt64Bits(value);int e=(int)((bits>>52)&2047);long mantissa=bits&0xfffffffffffffL;
        if(e!=0) mantissa|=1L<<52;int shift=e==0?-1074:e-1023-52;
        BigInteger n=bits<0?-new BigInteger(mantissa):new BigInteger(mantissa);return shift<0?(n,BigInteger.One<<-shift):(n<<shift,BigInteger.One);
    }
    public static IdentityAtom Rate(IdentityAtom amount,IdentityAtom quantity,int precision)
    {
        if(precision is not (0 or 2 or 4)) throw new IdentityReview("derived_rate_precision_review_required");
        var q=Fraction(Token(quantity));if(q.N.IsZero) return new("blank");var a=Fraction(Token(amount));var scale=BigInteger.Pow(10,precision);
        var exact=Round(a.N*q.D*scale,a.D*q.N);
        // Compare exact arithmetic with Python's binary64 division/rounding.
        // Disagreement requires review rather than changing accepted accounting.
        double legacy=double.Parse(Token(amount),CultureInfo.InvariantCulture)/double.Parse(Token(quantity),CultureInfo.InvariantCulture);
        var binary=Binary(legacy);if(exact!=Round(binary.N*scale,binary.D)) throw new IdentityReview("derived_rate_precision_review_required");
        string digits=BigInteger.Abs(exact).ToString(CultureInfo.InvariantCulture).PadLeft(precision+1,'0');
        if(precision>0) digits=digits.Insert(digits.Length-precision,".");return new("number",(exact.Sign<0?"-":"")+digits);
    }
    public static IReadOnlyDictionary<string,IdentityAtom> Prepare(UploadDerivedProfile profile,IReadOnlyDictionary<string,IdentityAtom> raw)
    {
        var result=new Dictionary<string,IdentityAtom>(raw,StringComparer.Ordinal);
        if(profile.RateMode=="derived") result[profile.Keys["rate"]]=Rate(raw.GetValueOrDefault(profile.Keys["amount"],new("blank")),raw.GetValueOrDefault(profile.Keys["quantity"],new("blank")),profile.Precision);
        return result;
    }
    public static string? AccountCode(UploadDerivedProfile profile,IReadOnlyDictionary<string,IdentityAtom> raw)
    {
        if(!profile.Keys.TryGetValue("account_code",out string? key)) return null;
        var atom=raw.GetValueOrDefault(key,new("blank"));
        if(UploadIdentityValue.Blank(atom)) return null;
        if(atom.Kind!="text") throw new IdentityReview("derived_identifier_review_required");
        return UploadNumericParser.Trim(atom.Text!);
    }
    public static (string? Type,string? TreasuryAccount,string Status) ClassifyTreasury(UploadDerivedProfile profile,string? account)
    {
        var types=(profile.Treasury??[]).Where(t=>t.Account==account&&t.Type is "cash" or "bank").Select(t=>t.Type).Distinct(StringComparer.Ordinal).ToArray();
        if(types.Length==1) return (types[0],account,types[0]);
        if(account is null||types.Length>1) return (null,null,"unmapped");
        return (null,null,"non_treasury");
    }
    public static UploadDerivedValues Project(UploadDerivedProfile profile,IReadOnlyDictionary<string,IdentityAtom> raw,string? date)
    {
        IdentityAtom Atom(string role)=>profile.Keys.TryGetValue(role,out var key)?raw.GetValueOrDefault(key,new("blank")):new("blank");
        string? Text(string role)
        {var atom=Atom(role);if(UploadIdentityValue.Blank(atom)) return null;if(atom.Kind!="text") throw new IdentityReview("derived_identifier_review_required");return UploadNumericParser.Trim(atom.Text!);}
        double Numeric(IdentityAtom atom)
        {
            string token=Token(atom);double value=double.Parse(token,CultureInfo.InvariantCulture);
            if(!double.IsFinite(value)||UploadExactAmount.Parse(token).CompareTo(UploadExactAmount.Parse(value.ToString("R",CultureInfo.InvariantCulture)))!=0)
                throw new IdentityReview("derived_precision_review_required");return value;
        }
        double Number(string role)=>Numeric(Atom(role));
        string? account=AccountCode(profile,raw),voucher=Text("document"),name=Text("account_name");
        var classification=ClassifyTreasury(profile,account);
        double? amount=null;
        if(profile.Keys.ContainsKey("amount")) {double n=Number("amount");if(n!=0) amount=n;}
        if(amount is null)
        {
            if(profile.NumericKeys is not null) foreach(string key in profile.NumericKeys)
            {double n=Numeric(raw.GetValueOrDefault(key,new("blank")));if(n!=0) {amount=n;break;}}
            else if(profile.Keys.ContainsKey("debit")) {double n=Number("debit");if(n==0) n=Number("credit");if(n!=0) amount=n;}
        }
        if(amount is {} helper&&(!float.IsFinite((float)helper)||helper!=0&&(float)helper==0)) throw new IdentityReview("derived_helper_range_review_required");
        DateOnly? day=date is null?null:DateOnly.ParseExact(date,"yyyy-MM-dd",CultureInfo.InvariantCulture);
        string? month=day?.ToString("yyyy-MM",CultureInfo.InvariantCulture);
        return new(voucher,account,name,date,month,day is null?null:month+(day.Value.Day<=14?"-F1":"-F2"),Number("debit"),Number("credit"),amount,
            classification.Type,classification.TreasuryAccount,classification.Status);
    }
    private static string Normal(string text)
    {
        if(text.Any(c=>c>127)) throw new IdentityReview("derived_voucher_case_review_required");
        return string.Concat(text.Where(c=>!char.IsWhiteSpace(c))).ToUpperInvariant();
    }
    public static string Fingerprint(IEnumerable<UploadDerivedValues> values)
    {
        // Strip trailing zeros only in fractional values, preserving e.g. 100.
        string Number(double n) {string s=UploadExactAmount.Parse(n.ToString("R",CultureInfo.InvariantCulture)).ToFixedString();return s.Contains('.')?s.TrimEnd('0').TrimEnd('.'):s;}
        var lines=values.Select(v=>new SortedDictionary<string,string>(StringComparer.Ordinal){{"account_code",Normal(v.Account??"")},{"credit",Number(v.Credit)},{"date",v.Date??""},{"debit",Number(v.Debit)}})
            .OrderBy(v=>v["date"],StringComparer.Ordinal).ThenBy(v=>v["account_code"],StringComparer.Ordinal).ThenBy(v=>v["debit"],StringComparer.Ordinal).ThenBy(v=>v["credit"],StringComparer.Ordinal).ToArray();
        return UploadIdentityValue.Hash(JsonSerializer.Serialize(new {fingerprint_version=1,lines},new JsonSerializerOptions{Encoder=JavaScriptEncoder.UnsafeRelaxedJsonEscaping}));
    }
    public static UploadVoucherDecision Occurrence(IReadOnlyList<UploadDerivedValues> rows,int upload,string sequence)
    {
        string voucher=rows[0].Voucher??"";string id=Normal(voucher);var dates=rows.Select(r=>r.Date).Distinct(StringComparer.Ordinal).ToArray();
        if(id.Length==0||dates.Length!=1||dates[0] is null) throw new IdentityReview("derived_voucher_membership_review_required");
        return new(voucher,id,$"upload:{upload}:voucher:{id}",Fingerprint(rows),dates[0],rows.Count,upload,sequence);
    }
    public static IReadOnlyList<UploadVoucherDecision> Resolve(IEnumerable<UploadVoucherDecision> source)
    {
        var result=new List<UploadVoucherDecision>();
        foreach(var group in source.GroupBy(r=>r.Identity,StringComparer.Ordinal))
        {
            UploadExactAmount? Priority(UploadVoucherDecision row) {try {return UploadExactAmount.Parse(row.Sequence);}catch(FormatException) {return null;}}
            var ordered=group.Select(r=>(Row:r,Priority:Priority(r))).OrderBy(r=>r.Priority.HasValue).ThenBy(r=>r.Priority)
                .ThenBy(r=>r.Priority is null?r.Row.Sequence:"",StringComparer.Ordinal).ThenBy(r=>r.Row.Upload).ThenBy(r=>r.Row.Key,StringComparer.Ordinal).Select(r=>r.Row).ToArray();var current=ordered[^1];bool revised=ordered.Select(r=>r.Fingerprint).Distinct(StringComparer.Ordinal).Skip(1).Any();
            foreach(var row in ordered) result.Add(row with {Canonical=current.Key,Status=row.Key==current.Key?(revised?"current_revised":"current"):row.Fingerprint==current.Fingerprint?"exact_duplicate":"superseded",
                Reason=row.Key==current.Key?(revised?"newer_source_version_replaces_prior_fingerprint":"first_or_latest_exact_voucher_version"):row.Fingerprint==current.Fingerprint?"same_voucher_identity_and_fingerprint":"older_voucher_fingerprint_replaced_by_newer_source"});
        }
        return result;
    }
}
