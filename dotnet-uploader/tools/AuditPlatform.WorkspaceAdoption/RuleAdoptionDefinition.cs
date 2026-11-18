using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using AuditPlatform.Api.AuditRules;
using AuditPlatform.Api.Templates;

namespace AuditPlatform.WorkspaceAdoption;

public static class RuleAdoptionDefinition
{
    public static readonly Dictionary<string,string> ParentTables=new()
    { ["documents"]="audit_rule_documents",["cross-checks"]="audit_rule_cross_checks",["categories"]="audit_rule_categories",
      ["assertions"]="audit_rule_assertions",["voucher-checks"]="audit_rule_voucher_checks" };
    public static readonly string[] Tables=[..ParentTables.Values,"audit_rule_document_fields","audit_rule_document_auth_roles",
        "audit_rule_cross_check_documents","audit_rule_cross_check_fields","audit_rule_cross_check_cells",
        "audit_rule_cross_check_controls","audit_rule_cross_check_control_operands","audit_rule_category_documents",
        "audit_rule_category_cross_checks","audit_rule_category_document_assertions","audit_rule_category_cross_check_assertions",
        "audit_rule_voucher_check_assertions","audit_rule_voucher_check_auth_roles","signature_specimens","audit_rules_config_state"];
    public sealed record Entity(string Kind,int SourceId,int Id,bool Active,bool System,DateTime Created,JsonObject Definition,string SourceHash);
    public sealed record FormField(int SourceId,int DocumentId,int Id);
    public sealed record Specimen(int SourceId,int Id,int ClientId,int DocumentId,string Role,string Name,string? Authority,bool Active,
        DateTime Created,DateTime Updated,string SourceHash);
    public sealed record Review(string Kind,int SourceId,string Reason,string SourceHash);
    public sealed record Conversion(Entity[] Entities,FormField[] Fields,Specimen[] Specimens,Review[] Reviews,long Version);
    public static string Hash(string text)=>System.Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
    private static int Id(JsonObject row,string name="id")=>row[name]?.GetValue<int>()??throw new InvalidOperationException("A required rule reference is missing.");
    private static string? Text(JsonObject row,string name)=>row[name]?.GetValue<string>();
    private static bool Flag(JsonObject row,string name,bool fallback=false)=>row[name]?.GetValue<bool>()??fallback;
    private static DateTime Date(JsonObject row,string name)=>DateTime.Parse(Text(row,name)??throw new InvalidOperationException("Missing legacy timestamp."),CultureInfo.InvariantCulture,DateTimeStyles.AdjustToUniversal|DateTimeStyles.AssumeUniversal);
    private static JsonNode? Parsed(JsonObject row,string name)=>row[name] is JsonValue scalar&&scalar.TryGetValue<string>(out var text)
        ?string.IsNullOrWhiteSpace(text)?null:JsonNode.Parse(text):row[name]?.DeepClone();
    private static JsonArray Array(IEnumerable<JsonNode?> values)=>new(values.Select(value=>value?.DeepClone()).ToArray());

    public static Conversion Convert(IReadOnlyDictionary<string,JsonArray> source,IReadOnlyList<TemplateDefinition> templates)
    {
        JsonObject[] Rows(string table)=>source.TryGetValue(table,out var rows)&&rows.All(row=>row is JsonObject)
            ?rows.Cast<JsonObject>().ToArray():throw new InvalidOperationException("Missing or malformed legacy rule table.");
        foreach(var table in Tables)Rows(table);
        if(Rows("audit_rule_cross_check_controls").Length!=0||Rows("audit_rule_cross_check_control_operands").Length!=0)
            throw new InvalidOperationException("Legacy matrix controls require a separate conversion contract.");
        var identities=new Dictionary<(string,int),int>();
        foreach(var kind in AuditRuleKinds.All)
            foreach(var row in Rows(ParentTables[kind]).OrderBy(row=>Id(row)))
                if(Id(row)<=0||!identities.TryAdd((kind,Id(row)),identities.Count+1))throw new InvalidOperationException("Invalid or duplicate legacy rule identity.");
        int Map(string kind,int id)=>identities.TryGetValue((kind,id),out int mapped)?mapped:throw new InvalidOperationException("A legacy rule reference is unresolved.");
        JsonObject[] Children(string table,string key,int id)=>Rows(table).Where(row=>Id(row,key)==id)
            .OrderBy(row=>row["sort_order"]?.GetValue<int>()??0).ThenBy(row=>Id(row)).ToArray();
        var childContracts=new Dictionary<string,(string Field,string Kind)[]>
        {
            ["audit_rule_document_fields"]=[("document_id","documents")], ["audit_rule_document_auth_roles"]=[("document_id","documents")],
            ["audit_rule_cross_check_documents"]=[("cross_check_id","cross-checks"),("document_id","documents")],
            ["audit_rule_cross_check_fields"]=[("cross_check_id","cross-checks")],
            ["audit_rule_category_documents"]=[("category_id","categories"),("document_id","documents")],
            ["audit_rule_category_cross_checks"]=[("category_id","categories"),("cross_check_id","cross-checks")],
            ["audit_rule_category_document_assertions"]=[("category_id","categories"),("document_id","documents"),("assertion_id","assertions")],
            ["audit_rule_category_cross_check_assertions"]=[("category_id","categories"),("cross_check_id","cross-checks"),("assertion_id","assertions")],
            ["audit_rule_voucher_check_assertions"]=[("voucher_check_id","voucher-checks"),("assertion_id","assertions")],
            ["audit_rule_voucher_check_auth_roles"]=[("voucher_check_id","voucher-checks")], ["signature_specimens"]=[("document_id","documents")]
        };
        foreach(var (table,contract) in childContracts)foreach(var row in Rows(table))foreach(var link in contract)Map(link.Kind,Id(row,link.Field));
        var matrixFields=Rows("audit_rule_cross_check_fields").ToDictionary(row=>Id(row));
        foreach(var cell in Rows("audit_rule_cross_check_cells"))
            if(!matrixFields.TryGetValue(Id(cell,"field_id"),out var field)
                ||!Children("audit_rule_cross_check_documents","cross_check_id",Id(field,"cross_check_id")).Any(row=>Id(row,"document_id")==Id(cell,"document_id")))
                throw new InvalidOperationException("A matrix cell has no matching document/field membership.");
        var entities=new List<Entity>();var formFields=new List<FormField>();var reviews=new List<Review>();
        foreach(var kind in AuditRuleKinds.All)
        foreach(var row in Rows(ParentTables[kind]).OrderBy(row=>Id(row)))
        {
            int old=Id(row),native=Map(kind,old);
            var definition=new JsonObject { ["name"]=row["name"]?.DeepClone() };
            if(kind=="documents")
            {
                foreach(string property in new[]{"kb_note","template_id","document_type","risk_rating","transaction_level_approval"})definition[property]=row[property]?.DeepClone();
                var roles=Children("audit_rule_document_auth_roles","document_id",old);
                if(roles.Select(role=>Text(role,"role_name")).Distinct(StringComparer.Ordinal).Count()!=roles.Length)
                    throw new InvalidOperationException("Duplicate Document roles require review.");
                definition["auth_roles"]=Array(roles.Select(role=>role["role_name"]));
                definition["auth_role_authorities"]=new JsonObject(roles.Where(role=>role["required_authority"] is not null)
                    .Select(role=>new KeyValuePair<string,JsonNode?>(Text(role,"role_name")!,role["required_authority"]!.DeepClone())));
                definition["verification_field_policy"]=Parsed(row,"verification_field_policy");
                var fields=Children("audit_rule_document_fields","document_id",old);
                definition["cross_check_fields"]=Text(row,"document_type")=="form"?Array(fields.Select(field=>field["field_name"])):new JsonArray();
                var form=new JsonArray();
                if(Text(row,"document_type")=="form")foreach(var field in fields)
                {
                    int fieldId=formFields.Count+1;formFields.Add(new(Id(field),native,fieldId));
                    var item=new JsonObject { ["id"]=fieldId };
                    foreach(string property in new[]{"field_name","field_type","field_scope","linked_column","sort_order"})item[property]=field[property]?.DeepClone();
                    item["arithmetic_operands"]=Parsed(field,"arithmetic_operands")??new JsonArray();form.Add(item);
                }
                else foreach(var field in fields)
                    reviews.Add(new("document_fields",Id(field),"non_form_field_definition",Hash(field.ToJsonString())));
                definition["form_fields"]=form;
                int? templateId=row["template_id"]?.GetValue<int>();
                var template=templates.SingleOrDefault(item=>item.Id==templateId);
                if(templateId is not null&&template is null&&Flag(row,"is_active"))throw new InvalidOperationException("Active Document template ownership is unresolved.");
                if(!AuditRuleService.NormalizeVerificationFieldPolicy(definition,template))throw new InvalidOperationException("Legacy verification field policy requires review.");
            }
            else if(kind=="cross-checks")
            {
                foreach(string property in new[]{"compare_field","comparison_type","compare_to_voucher_line"})definition[property]=row[property]?.DeepClone();
                definition["shortfall_document_id"]=row["shortfall_document_id"] is null?null:Map("documents",Id(row,"shortfall_document_id"));
                var docs=Children("audit_rule_cross_check_documents","cross_check_id",old);
                if(docs.Select(doc=>Id(doc,"document_id")).Distinct().Count()!=docs.Length)throw new InvalidOperationException("Duplicate matrix documents.");
                definition["document_ids"]=new JsonArray(docs.Select(doc=>(JsonNode?)JsonValue.Create(Map("documents",Id(doc,"document_id")))).ToArray());
                definition["document_fields"]=new JsonObject(docs.Select(doc=>new KeyValuePair<string,JsonNode?>(Map("documents",Id(doc,"document_id")).ToString(CultureInfo.InvariantCulture),doc["compare_field"]?.DeepClone())));
                var fields=new JsonArray();
                foreach(var field in Children("audit_rule_cross_check_fields","cross_check_id",old))
                {
                    var item=new JsonObject();
                    foreach(string property in new[]{"id","sort_order","operator","tolerance","missing_data_behavior","blocking","risk"})item[property]=field[property]?.DeepClone();
                    var cells=Rows("audit_rule_cross_check_cells").Where(cell=>Id(cell,"field_id")==Id(field)).ToArray();
                    item["cells"]=new JsonObject(cells.Select(cell=>new KeyValuePair<string,JsonNode?>(Map("documents",Id(cell,"document_id")).ToString(CultureInfo.InvariantCulture),cell["column_name"]?.DeepClone())));
                    fields.Add(item);
                }
                definition["matrix"]=new JsonObject { ["cross_check_id"]=native,["documents"]=new JsonArray(docs.Select(doc=>(JsonNode?)new JsonObject
                    { ["document_id"]=Map("documents",Id(doc,"document_id")),["sort_order"]=doc["sort_order"]?.DeepClone(),["compare_field"]=doc["compare_field"]?.DeepClone() }).ToArray()),["fields"]=fields };
            }
            else if(kind=="categories")
            {
                foreach(string property in new[]{"kb_overview","evidence_type","matrix_version"})definition[property]=row[property]?.DeepClone();
                var docs=Children("audit_rule_category_documents","category_id",old);
                var checks=Children("audit_rule_category_cross_checks","category_id",old);
                definition["document_ids"]=new JsonArray(docs.Select(doc=>(JsonNode?)JsonValue.Create(Map("documents",Id(doc,"document_id")))).ToArray());
                definition["cross_check_ids"]=new JsonArray(checks.Select(check=>(JsonNode?)JsonValue.Create(Map("cross-checks",Id(check,"cross_check_id")))).ToArray());
                definition["assertion_ids"]=new JsonArray();
                definition["document_required"]=new JsonObject(docs.Select(doc=>new KeyValuePair<string,JsonNode?>(Map("documents",Id(doc,"document_id")).ToString(CultureInfo.InvariantCulture),JsonValue.Create(Flag(doc,"is_required",true)))));
                JsonObject Tags(string table,string field,string referencedKind)
                {
                    var result=new JsonObject();
                    foreach(var group in Children(table,"category_id",old).GroupBy(item=>Map(referencedKind,Id(item,field))))
                        result[group.Key.ToString(CultureInfo.InvariantCulture)]=new JsonArray(group.Select(item=>(JsonNode?)JsonValue.Create(Map("assertions",Id(item,"assertion_id")))).ToArray());
                    return result;
                }
                definition["document_assertions"]=Tags("audit_rule_category_document_assertions","document_id","documents");
                definition["cross_check_assertions"]=Tags("audit_rule_category_cross_check_assertions","cross_check_id","cross-checks");
                var rules=Parsed(row,"validation_rules_json")??new JsonObject();
                void Remap(JsonNode? node)
                {
                    if(node is JsonObject obj)foreach(var key in obj.Select(pair=>pair.Key).ToArray())
                    {
                        if(key is "document_id" or "left_document_id" or "right_document_id" or "shortfall_document_id")
                        {
                            if(obj[key] is null)continue;
                            string scalar=obj[key] is JsonValue value&&value.TryGetValue<string>(out var text)?text:obj[key]!.ToJsonString();
                            obj[key]=Map("documents",int.Parse(scalar,CultureInfo.InvariantCulture));
                        }
                        else if(key=="required_documents"&&obj[key] is JsonArray {Count:>0})throw new InvalidOperationException("Legacy required-document lists need explicit conversion.");
                        else Remap(obj[key]);
                    }
                    else if(node is JsonArray array)foreach(var child in array)Remap(child);
                }
                Remap(rules);definition["validation_rules_json"]=rules;
                if(!AuditRuleService.ValidateCategoryRules(definition))throw new InvalidOperationException("Category rule references require review.");
            }
            else
            {
                foreach(string property in kind=="assertions"?new[]{"description","sort_order"}:new[]{"authority","sort_order"})definition[property]=row[property]?.DeepClone();
                if(kind=="voucher-checks")
                {
                    definition["assertion_ids"]=new JsonArray(Children("audit_rule_voucher_check_assertions","voucher_check_id",old).Select(item=>(JsonNode?)JsonValue.Create(Map("assertions",Id(item,"assertion_id")))).ToArray());
                    definition["auth_roles"]=Array(Children("audit_rule_voucher_check_auth_roles","voucher_check_id",old).Select(role=>role["role_name"]));
                }
            }
            var normalized=AuditRuleService.ValidateDefinition(kind,JsonSerializer.SerializeToElement(definition),requireComplete:false)
                ??throw new InvalidOperationException("Legacy definition is incompatible with native authoring policy.");
            if(string.IsNullOrWhiteSpace(Text(normalized,"name"))||(kind=="documents"&&!AuditRuleService.ValidateFormFields(normalized["form_fields"]!.AsArray())))
                throw new InvalidOperationException("A required legacy name/Form definition is invalid.");
            entities.Add(new(kind,old,native,Flag(row,"is_active"),Flag(row,"is_system"),Date(row,"created_at"),normalized,Hash(row.ToJsonString())));
        }
        var specimens=new List<Specimen>();
        foreach(var row in Rows("signature_specimens").OrderBy(row=>Id(row)))
        {
            int document=Map("documents",Id(row,"document_id"));string role=Text(row,"role_name")??"";
            var definition=entities.Single(entity=>entity.Kind=="documents"&&entity.Id==document).Definition;
            if(!definition["auth_roles"]!.AsArray().Any(node=>node?.GetValue<string>()==role))
            {reviews.Add(new("signature_specimens",Id(row),"missing_document_role",Hash(row.ToJsonString())));continue;}
            specimens.Add(new(Id(row),specimens.Count+1,Id(row,"client_id"),document,role,Text(row,"expected_signatory_name")??throw new InvalidOperationException("Missing signatory name."),
                Text(row,"authority"),Flag(row,"is_active"),Date(row,"created_at"),Date(row,"updated_at"),Hash(row.ToJsonString())));
        }
        var states=Rows("audit_rules_config_state");
        if(states.Length!=1)throw new InvalidOperationException("Exactly one legacy Rulebook state is required.");
        long version=states[0]["config_version"]!.GetValue<long>()+1;
        if(version<2||version>int.MaxValue)throw new InvalidOperationException("Rulebook version requires explicit review.");
        return new(entities.ToArray(),formFields.ToArray(),specimens.ToArray(),reviews.ToArray(),version);
    }
}
