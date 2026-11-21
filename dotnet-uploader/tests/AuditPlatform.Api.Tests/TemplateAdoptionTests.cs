using AuditPlatform.Api.Mappings;
using AuditPlatform.WorkspaceAdoption;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TemplateAdoptionTests
{
    [Fact]
    public void Preview_resolves_only_one_active_workspace_unless_explicitly_selected()
    {
        Assert.Equal(61,TemplateAdoption.ResolveTenantId([61],null));
        Assert.Equal(62,TemplateAdoption.ResolveTenantId([61,62],62));
        Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.ResolveTenantId([],null));
        Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.ResolveTenantId([61,62],null));
        Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.ResolveTenantId([61],62));
    }

    [Fact]
    public void Read_only_preview_reports_missing_apply_migrations()
    {
        Assert.Equal(["0010_tenant_saved_mappings.sql","0024_saved_mapping_adoption_review.sql","0026_legacy_template_promotion.sql"],
            TemplateAdoption.RequiredMigrationsForApply(false,false,false));
        Assert.Equal(["0024_saved_mapping_adoption_review.sql","0026_legacy_template_promotion.sql"],
            TemplateAdoption.RequiredMigrationsForApply(true,false,false));
        Assert.Equal(["0026_legacy_template_promotion.sql"],
            TemplateAdoption.RequiredMigrationsForApply(true,true,false));
        Assert.Empty(TemplateAdoption.RequiredMigrationsForApply(true,true,true));
    }

    [Fact]
    public void Supabase_apply_requires_preview_identity_and_explicit_retention_choice()
    {
        var preview=new Dictionary<string,string>{{"target","supabase-development"},{"stage","templates"}};
        SupabaseDevelopmentAdoption.ValidateTemplateOptions(preview);
        Assert.Throws<InvalidOperationException>(()=>SupabaseDevelopmentAdoption.ValidateTemplateOptions(
            new Dictionary<string,string>(preview){{"apply","true"}}));
        Assert.Throws<InvalidOperationException>(()=>SupabaseDevelopmentAdoption.ValidateTemplateOptions(
            new Dictionary<string,string>(preview){{"expected",new string('a',64)}}));

        var apply=new Dictionary<string,string>(preview)
        {
            ["apply"]="true",["retain-unresolved"]="true",["operation"]="9c9632b6-9f47-42d8-a73c-cfcbad9f45f0",
            ["expected"]=new string('a',64)
        };
        SupabaseDevelopmentAdoption.ValidateTemplateOptions(apply);
        Assert.Throws<InvalidOperationException>(()=>SupabaseDevelopmentAdoption.ValidateTemplateOptions(
            new Dictionary<string,string>(apply){{"workspace-name","unexpected"}}));
    }

    private static TemplateAdoption.Source Example() => new(
        [new(11,"Reference","Text","General",null,true)],
        [new(21,"Synthetic",null,new DateTime(2026,1,1,0,0,0,DateTimeKind.Utc),false,false,"standard",[],null,true)],
        [new(31,21,11,0,"document_id",null,false,null,null)],
        [new(41,51,21,11," ERP reference ",null)],
        [new(51,true,61)], [new(21,["Reference"])],0);

    [Fact]
    public void Existing_ids_keys_and_literal_headers_are_preserved()
    {
        var source=Example();
        var result=TemplateAdoption.Validate(source,61);
        Assert.Equal("Reference",result[21].Columns[0].StorageKey);
        Assert.Equal(11,result[21].Columns[0].Id);
        Assert.Equal(" ERP reference ",source.Headers[0].HeaderValue);
    }

    [Fact]
    public void Exact_existing_tenant_mapping_is_accepted_but_changed_key_is_rejected()
    {
        var source=Example();
        var adopted=source with {Mappings=[source.Mappings[0] with {Tenant=61,ExistingKey="Reference"}]};
        Assert.Equal("Reference",TemplateAdoption.Validate(adopted,61)[21].Columns[0].StorageKey);
        Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.Validate(
            adopted with {Mappings=[adopted.Mappings[0] with {ExistingKey="Renamed"}]},61));
        Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.Validate(
            adopted with {Mappings=[adopted.Mappings[0] with {Tenant=62}]},61));
    }

    [Theory]
    [InlineData("standard","Ledger Source Notes")]
    [InlineData("account_ledger","ledger source notes")]
    [InlineData("standard","Old reference caption")]
    public void Unknown_historical_keys_are_never_guessed(string purpose,string key)
        =>Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.ValidateStoredKeys(purpose,["Reference"],[key]));

    [Fact]
    public void Canonical_ledger_source_notes_remain_separate_metadata()
        =>TemplateAdoption.ValidateStoredKeys("account_ledger",["Reference"],["Reference","Ledger Source Notes"]);

    [Fact]
    public void Foreign_bindings_block_adoption_and_inactive_clients_are_retained_for_review()
    {
        var source=Example();
        Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.Validate(source with {Mappings=[source.Mappings[0] with {Tenant=99}]},61));
        var inactive=source with {Clients=[source.Clients[0] with {Active=false}]};
        var definitions=TemplateAdoption.Validate(inactive,61);
        Assert.Equal("inactive_client",TemplateAdoption.ReviewReason(inactive.Headers[0],inactive,definitions,61));
        var obsolete=source with {Headers=[source.Headers[0] with {ColumnId=999}]};
        Assert.Equal("missing_column_binding",TemplateAdoption.ReviewReason(obsolete.Headers[0],obsolete,TemplateAdoption.Validate(obsolete,61),61));
        Assert.Throws<InvalidOperationException>(()=>TemplateAdoption.Validate(source with {Headers=[source.Headers[0],source.Headers[0]]},61));
    }

    [Fact]
    public void Shared_runtime_review_rules_reject_changed_keys_or_policy()
    {
        var template=TemplateAdoption.Validate(Example(),61)[21];
        var binding=SavedMappingDefinition.BindingKey(template);
        SavedHeader[] entries=[new(11," ERP reference ","Reference")];
        Assert.False(SavedMappingDefinition.NeedsReview(template,binding,entries));
        Assert.True(SavedMappingDefinition.NeedsReview(template,binding,[new(11," ERP reference ","Old key")]));
        Assert.True(SavedMappingDefinition.NeedsReview(template with {Columns=[template.Columns[0] with {Review=true}]},binding,entries));
        Assert.True(SavedMappingDefinition.NeedsReview(template,binding,[],pendingAdoption:true));
    }
}
