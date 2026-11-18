using System.Text.Json;
using AuditPlatform.Api.AuditRules;
using AuditPlatform.Api.Data;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class AuditRuleSnapshotPublicationTests(PostgresFixture fixture):IClassFixture<PostgresFixture>
{
    [Fact]
    public async Task Global_reader_is_closed_before_category_queries_and_unchanged_publication_retains_revision()
    {
        var ct=TestContext.Current.CancellationToken;
        await using var connection=await fixture.OpenControlConnectionAsync(ct);
        async Task Sql(string sql){await using var command=new NpgsqlCommand(sql,connection);await command.ExecuteNonQueryAsync(ct);}
        await Sql("""
            CREATE TABLE public.users(id integer PRIMARY KEY);
            CREATE TABLE public.platform_tenants(id integer PRIMARY KEY);
            CREATE TABLE public.platform_tenant_client_homes(client_id integer,tenant_id integer,UNIQUE(client_id,tenant_id));
            CREATE TABLE public.master_columns(id integer,column_name text,data_type text,is_active boolean);
            CREATE TABLE public.templates(id integer,created_by text,created_date timestamptz,is_system boolean,
                has_subtotals_by_default boolean,data_purpose text,ledger_columns text,rate_source text,is_active boolean);
            CREATE TABLE public.platform_tenant_templates(template_id integer,tenant_id integer,display_name text,
                template_kind text,binding_origin text,definition_version bigint);
            CREATE TABLE public.platform_tenant_columns(column_id integer,tenant_id integer,display_name text,
                category text,binding_origin text,definition_version bigint);
            CREATE TABLE public.template_column_mappings(template_id integer,column_id integer,tenant_id integer,
                storage_key text,reconciliation_role text,compare_for_changes boolean,comparison_review_required boolean,sort_order integer);
            INSERT INTO public.users VALUES(1);INSERT INTO public.platform_tenants VALUES(61);
            """);
        DirectoryInfo? root=new(AppContext.BaseDirectory);
        while(root is not null&&!File.Exists(Path.Combine(root.FullName,"AuditPlatform.slnx")))root=root.Parent;
        Assert.NotNull(root);
        await Sql(await File.ReadAllTextAsync(Path.Combine(root.FullName,"database","migrations","0021_audit_rules.sql"),ct));
        await Sql("""
            INSERT INTO public.platform_audit_rule_state(tenant_id) VALUES(61);
            INSERT INTO public.platform_audit_rule_entities(id,tenant_id,entity_kind,definition,created_by,updated_by)
                OVERRIDING SYSTEM VALUE VALUES
                (1,61,'assertions','{"name":"Synthetic assertion","description":"source","sort_order":0}',1,1),
                (2,61,'voucher-checks','{"name":"Synthetic check","assertion_ids":[1],"sort_order":0}',1,1);
            """);
        await using var transaction=await connection.BeginTransactionAsync(ct);
        var db=new PostgresTransactionContext(connection,transaction);
        await AuditRuleService.PublishSnapshotsAsync(db,61,1,ct);
        async Task<(long Revision,string Fingerprint)> Snapshot()
        {
            await using var query=db.CreateCommand("SELECT revision,fingerprint,payload::text FROM public.platform_audit_rule_snapshots WHERE tenant_id=61");
            await using var reader=await query.ExecuteReaderAsync(ct);Assert.True(await reader.ReadAsync(ct));
            using var payload=JsonDocument.Parse(reader.GetString(2));
            Assert.Single(payload.RootElement.GetProperty("assertions").EnumerateArray());
            Assert.Single(payload.RootElement.GetProperty("voucher_checks").EnumerateArray());
            Assert.Empty(payload.RootElement.GetProperty("categories").EnumerateArray());
            return(reader.GetInt64(0),reader.GetString(1));
        }
        var first=await Snapshot();Assert.Equal(1,first.Revision);
        await AuditRuleService.PublishSnapshotsAsync(db,61,1,ct);
        Assert.Equal(first,await Snapshot());
        await transaction.RollbackAsync(ct);
    }
}
