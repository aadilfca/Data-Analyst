using AuditPlatform.Api.AuditRules;
using AuditPlatform.Api.Data;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class SignatureSpecimenAdoptionRetentionTests(PostgresFixture fixture):IClassFixture<PostgresFixture>
{
    [Fact]
    public async Task Provenance_lookup_is_tenant_scoped_and_optional_before_migration()
    {
        var ct=TestContext.Current.CancellationToken;
        await using var connection=await fixture.OpenControlConnectionAsync(ct);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var db=new PostgresTransactionContext(connection,transaction);

        Assert.False(await AuditRuleService.HasAdoptionProvenanceAsync(db,7,3,ct));
        await using(var create=db.CreateCommand("""
            CREATE TABLE public.platform_audit_rule_specimen_adoption_map(
                tenant_id integer NOT NULL,specimen_id integer NOT NULL,PRIMARY KEY(tenant_id,specimen_id))
            """)) await create.ExecuteNonQueryAsync(ct);
        await using(var insert=db.CreateCommand("INSERT INTO public.platform_audit_rule_specimen_adoption_map VALUES(7,3)"))
            await insert.ExecuteNonQueryAsync(ct);

        Assert.True(await AuditRuleService.HasAdoptionProvenanceAsync(db,7,3,ct));
        Assert.False(await AuditRuleService.HasAdoptionProvenanceAsync(db,7,4,ct));
        Assert.False(await AuditRuleService.HasAdoptionProvenanceAsync(db,8,3,ct));
        await transaction.RollbackAsync(ct);
    }
}
