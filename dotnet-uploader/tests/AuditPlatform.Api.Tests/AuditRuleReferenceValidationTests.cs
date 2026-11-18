using System.Text.Json.Nodes;
using AuditPlatform.Api.AuditRules;
using AuditPlatform.Api.Data;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class AuditRuleReferenceValidationTests(PostgresFixture fixture):IClassFixture<PostgresFixture>
{
    [Fact]
    public async Task Matrix_cells_validate_without_flat_placeholders_and_all_reference_guards_remain()
    {
        var ct=TestContext.Current.CancellationToken;
        await using var connection=await fixture.OpenControlConnectionAsync(ct);
        await using(var create=new NpgsqlCommand("""
            CREATE TABLE public.platform_audit_rule_entities(id integer PRIMARY KEY,tenant_id integer,entity_kind text,definition jsonb,is_active boolean);
            INSERT INTO public.platform_audit_rule_entities VALUES
              (10,61,'documents','{"document_type":"form","form_fields":[{"field_name":"Amount"}]}',true),
              (11,61,'documents','{"document_type":"form","form_fields":[{"field_name":"Amount"}]}',true),
              (12,62,'documents','{"document_type":"form","form_fields":[{"field_name":"Amount"}]}',true);
            """,connection))await create.ExecuteNonQueryAsync(ct);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var db=new PostgresTransactionContext(connection,transaction);
        var definition=JsonNode.Parse("""
            {"name":"Synthetic matrix","document_ids":[10,11],"document_fields":{},"compare_field":"(matrix)","comparison_type":"All equal",
             "matrix":{"cross_check_id":20,"documents":[{"document_id":10},{"document_id":11}],
              "fields":[{"id":100,"operator":"=","risk":"Medium","tolerance":0,"missing_data_behavior":"not_configured","cells":{"10":"Amount","11":"Amount"}}]}}
            """)!.AsObject();
        async Task<bool> Valid(JsonObject value)=>await AuditRuleService.ValidateReferencesAsync(db,61,"cross-checks",value,ct);
        Assert.True(await Valid(definition));
        var invalid=(JsonObject)definition.DeepClone();invalid["matrix"]!["fields"]![0]!["cells"]!["10"]="Missing field";
        Assert.False(await Valid(invalid));
        invalid=(JsonObject)definition.DeepClone();invalid["matrix"]!["fields"]![0]!["operator"]="guess";
        Assert.False(await Valid(invalid));
        invalid=(JsonObject)definition.DeepClone();invalid["document_ids"]![1]=12;
        Assert.False(await Valid(invalid));
        invalid=(JsonObject)definition.DeepClone();invalid["matrix"]!["documents"]![1]!["document_id"]=12;
        Assert.False(await Valid(invalid));
        var flat=(JsonObject)definition.DeepClone();flat["matrix"]!["fields"]=new JsonArray();
        Assert.False(await Valid(flat));
        flat["compare_field"]="Amount";Assert.True(await Valid(flat));
        await using(var duplicate=db.CreateCommand("""
            INSERT INTO public.platform_audit_rule_entities VALUES(30,61,'cross-checks','{"matrix":{"fields":[{"id":100}]}}',true)
            """))await duplicate.ExecuteNonQueryAsync(ct);
        Assert.False(await Valid(definition));
        await transaction.RollbackAsync(ct);
    }
}
