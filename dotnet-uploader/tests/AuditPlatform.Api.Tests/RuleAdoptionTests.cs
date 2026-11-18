using System.Text.Json;
using System.Text.Json.Nodes;
using AuditPlatform.WorkspaceAdoption;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class RuleAdoptionTests
{
    private static JsonObject Row(string json)=>JsonNode.Parse(json)!.AsObject();
    private static Dictionary<string,JsonArray> Source()
    {
        var source=RuleAdoptionDefinition.Tables.ToDictionary(table=>table,_=>new JsonArray());
        source["audit_rule_documents"].Add(Row("""
            {"id":10,"name":"Original Form","kb_note":"source note","template_id":null,"is_active":true,
            "document_type":"form","created_at":"2026-01-01T00:00:00Z","risk_rating":"Medium","transaction_level_approval":false,"is_system":false,"verification_field_policy":null}
            """));
        source["audit_rule_document_fields"].Add(Row("""
            {"id":100,"document_id":10,"field_name":"Reference","sort_order":0,"master_column_id":null,
            "field_type":"text","field_scope":"header","linked_column":null,"arithmetic_operands":"[]"}
            """));
        source["audit_rule_document_auth_roles"].Add(Row("""{"id":1,"document_id":10,"role_name":"Prepared By","sort_order":0,"required_authority":null}"""));
        source["audit_rule_cross_checks"].Add(Row("""
            {"id":10,"name":"Comparison","compare_field":"Reference","comparison_type":"All equal","is_active":false,
             "created_at":"2026-01-01T00:00:00Z","compare_to_voucher_line":false,"shortfall_document_id":10}
            """));
        source["audit_rule_cross_check_documents"].Add(Row("""{"id":1,"cross_check_id":10,"document_id":10,"sort_order":0,"compare_field":"Reference"}"""));
        source["audit_rule_cross_check_fields"].Add(Row("""{"id":200,"cross_check_id":10,"sort_order":0,"operator":"=","tolerance":0,"missing_data_behavior":"not_configured","blocking":true,"risk":"High"}"""));
        source["audit_rule_cross_check_cells"].Add(Row("""{"id":1,"field_id":200,"document_id":10,"column_name":"Reference"}"""));
        source["audit_rule_categories"].Add(Row("""
            {"id":10,"name":"Category","kb_overview":"original overview","is_active":true,"evidence_type":null,
             "created_at":"2026-01-01T00:00:00Z","matrix_version":7,
             "validation_rules_json":{"procedure_evidence":{"checks":[{"document_id":10,"condition":"required_document","risk":"High"}]},"mathematical_cross_checks":[]}}
            """));
        source["audit_rule_category_documents"].Add(Row("""{"id":1,"category_id":10,"document_id":10,"sort_order":0,"is_required":false}"""));
        source["audit_rule_category_cross_checks"].Add(Row("""{"id":1,"category_id":10,"cross_check_id":10,"sort_order":0}"""));
        source["audit_rule_assertions"].Add(Row("""{"id":10,"name":"Assertion","description":"original wording","sort_order":2,"is_active":false,"created_at":"2026-01-01T00:00:00Z"}"""));
        source["audit_rule_category_document_assertions"].Add(Row("""{"id":1,"category_id":10,"document_id":10,"assertion_id":10,"created_at":"2026-01-01T00:00:00Z"}"""));
        source["audit_rule_voucher_checks"].Add(Row("""{"id":10,"name":"Voucher check","sort_order":3,"is_active":true,"created_at":"2026-01-01T00:00:00Z","authority":"original authority"}"""));
        source["audit_rule_voucher_check_assertions"].Add(Row("""{"id":1,"voucher_check_id":10,"assertion_id":10,"created_at":"2026-01-01T00:00:00Z"}"""));
        source["audit_rules_config_state"].Add(Row("""{"id":1,"config_version":311,"updated_at":"2026-01-01T00:00:00Z"}"""));
        return source;
    }

    [Fact]
    public void Kind_qualified_ids_and_every_nested_reference_are_remapped_without_collisions()
    {
        var converted=RuleAdoptionDefinition.Convert(Source(),[]);
        Assert.Equal(5,converted.Entities.Select(entity=>entity.Id).Distinct().Count());
        var doc=Assert.Single(converted.Entities,entity=>entity.Kind=="documents");
        var category=Assert.Single(converted.Entities,entity=>entity.Kind=="categories");
        var check=Assert.Single(converted.Entities,entity=>entity.Kind=="cross-checks");
        var assertion=Assert.Single(converted.Entities,entity=>entity.Kind=="assertions");
        Assert.NotEqual(doc.Id,category.Id);Assert.All(converted.Entities,entity=>Assert.Equal(10,entity.SourceId));
        Assert.Equal(doc.Id,category.Definition["document_ids"]![0]!.GetValue<int>());
        Assert.Equal(check.Id,category.Definition["cross_check_ids"]![0]!.GetValue<int>());
        Assert.Equal(doc.Id,category.Definition["validation_rules_json"]!["procedure_evidence"]!["checks"]![0]!["document_id"]!.GetValue<int>());
        Assert.False(category.Definition["document_required"]![doc.Id.ToString()]!.GetValue<bool>());
        Assert.Equal(assertion.Id,category.Definition["document_assertions"]![doc.Id.ToString()]![0]!.GetValue<int>());
        Assert.Equal(check.Id,check.Definition["matrix"]!["cross_check_id"]!.GetValue<int>());
        Assert.Equal("Reference",check.Definition["matrix"]!["fields"]![0]!["cells"]![doc.Id.ToString()]!.GetValue<string>());
        Assert.False(check.Active);Assert.False(assertion.Active);Assert.Equal(312,converted.Version);
    }

    [Fact]
    public void Original_nodes_wording_roles_and_timestamps_are_preserved_and_form_ids_are_mapped()
    {
        var source=Source();string before=JsonSerializer.Serialize(source);
        var converted=RuleAdoptionDefinition.Convert(source,[]);
        Assert.Equal(before,JsonSerializer.Serialize(source));
        var doc=Assert.Single(converted.Entities,entity=>entity.Kind=="documents");
        Assert.Equal("source note",doc.Definition["kb_note"]!.GetValue<string>());
        Assert.Equal("Prepared By",doc.Definition["auth_roles"]![0]!.GetValue<string>());
        Assert.Equal(DateTime.UnixEpoch.AddYears(56),doc.Created);
        var field=Assert.Single(converted.Fields);Assert.Equal(100,field.SourceId);Assert.Equal(doc.Id,field.DocumentId);
        Assert.Equal(field.Id,doc.Definition["form_fields"]![0]!["id"]!.GetValue<int>());
    }

    [Fact]
    public void Missing_signature_role_is_retained_for_review_while_valid_specimen_is_preserved()
    {
        var source=Source();
        source["signature_specimens"].Add(Row("""{"id":1,"client_id":50,"document_id":10,"role_name":"Prepared By","expected_signatory_name":"Synthetic Person","authority":null,"is_active":true,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-02T00:00:00Z"}"""));
        source["signature_specimens"].Add(Row("""{"id":2,"client_id":50,"document_id":10,"role_name":"Removed role","expected_signatory_name":"Other Synthetic Person","is_active":true}"""));
        var converted=RuleAdoptionDefinition.Convert(source,[]);
        Assert.Equal("Synthetic Person",Assert.Single(converted.Specimens).Name);
        var review=Assert.Single(converted.Reviews);Assert.Equal(2,review.SourceId);Assert.Equal("missing_document_role",review.Reason);
        Assert.Equal(2,source["signature_specimens"].Count);
    }

    [Theory]
    [InlineData("audit_rule_document_fields","document_id",999)]
    [InlineData("audit_rule_cross_check_cells","field_id",999)]
    [InlineData("audit_rule_category_documents","document_id",999)]
    [InlineData("audit_rule_voucher_check_assertions","assertion_id",999)]
    public void Dangling_references_block_conversion(string table,string field,int id)
    {
        var source=Source();source[table][0]![field]=id;
        Assert.Throws<InvalidOperationException>(()=>RuleAdoptionDefinition.Convert(source,[]));
    }

    [Fact]
    public void Non_form_field_declarations_are_retained_without_becoming_live_form_fields()
    {
        var source=Source();source["audit_rule_documents"][0]!["document_type"]="linked_template";
        source["audit_rule_documents"][0]!["is_active"]=false;
        var converted=RuleAdoptionDefinition.Convert(source,[]);
        Assert.Empty(converted.Fields);
        var review=Assert.Single(converted.Reviews);Assert.Equal("document_fields",review.Kind);
        Assert.Equal(100,review.SourceId);Assert.Equal("non_form_field_definition",review.Reason);
        Assert.Empty(Assert.Single(converted.Entities,entity=>entity.Kind=="documents").Definition["form_fields"]!.AsArray());
        Assert.Single(source["audit_rule_document_fields"]);
    }

    [Fact]
    public void Unsupported_controls_invalid_forms_and_unknown_comparisons_are_not_silently_dropped()
    {
        var source=Source();source["audit_rule_cross_check_controls"].Add(Row("""{"id":1}"""));
        Assert.Throws<InvalidOperationException>(()=>RuleAdoptionDefinition.Convert(source,[]));
        source=Source();source["audit_rule_document_fields"][0]!["field_type"]="unknown";
        Assert.Throws<InvalidOperationException>(()=>RuleAdoptionDefinition.Convert(source,[]));
        source=Source();source["audit_rule_cross_checks"][0]!["comparison_type"]="Guess";
        Assert.Throws<InvalidOperationException>(()=>RuleAdoptionDefinition.Convert(source,[]));
    }
}
