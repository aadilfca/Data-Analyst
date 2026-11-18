using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Uploads;
using AuditPlatform.WorkspaceAdoption;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class UploadBindingAdoptionTests
{
    private static TemplateColumn Column(int id,string key,string type,string? role=null)=>new(id,key,key,type,"General",role,null,false,id,1,true);
    private static TemplateDefinition Template()=>new(21,"Synthetic",null!,DateTime.UnixEpoch,false,false,"standard",[],"auto","standard","native",1,
        [Column(1,"Document","Text","document_id"),Column(2,"Date","Date","document_date"),Column(3,"Amount","Numeric"),
         Column(4,"Quantity","Numeric"),Column(5,"Rate","Numeric")]);
    private static UploadBindingAdoption.Plan Plan()=>new(1,61,[new(21,[new("amount",3,"Amount"),new("quantity",4,"Quantity"),new("rate",5,"Rate")])]);

    [Fact]
    public void Explicit_ids_and_storage_keys_survive_display_label_changes()
    {
        var original=Template();
        var renamed=original with {Columns=original.Columns.Select(column=>column with {Name="Renamed "+column.Id}).ToArray()};
        var result=Assert.Single(UploadBindingAdoption.Validate(Plan(),61,[renamed]));
        Assert.Null(result.Review);
        Assert.Equal("Rate",UploadDerivedFieldService.Bind(renamed with {UploadBindings=result.Bindings})!.Keys["rate"]);
        Assert.Equal(5,result.Bindings["rate"]);
    }

    [Fact]
    public void Foreign_missing_duplicate_and_already_adopted_scopes_are_rejected()
    {
        var plan=Plan();var template=Template();
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.Validate(plan,99,[template]));
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.Validate(plan,61,[]));
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.Validate(plan with {Templates=[plan.Templates[0],plan.Templates[0]]},61,[template]));
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.Validate(plan,61,[template with {UploadBindings=new(){{"rate",5}}}]));
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.Validate(plan,61,[template,template with {Id=22}]));
    }

    [Theory]
    [InlineData("amount",3,"Old Amount")]
    [InlineData("amount",1,"Document")]
    [InlineData("unknown",3,"Amount")]
    [InlineData("rate",3,"Amount")]
    public void Changed_keys_wrong_types_unknown_roles_and_overlapping_rate_are_rejected(string role,int id,string key)
    {
        var plan=Plan();
        var fields=plan.Templates[0].Fields.Where(field=>field.Role!=role).Append(new(role,id,key)).ToArray();
        var changed=plan with {Templates=[plan.Templates[0] with {Fields=fields}]};
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.Validate(changed,61,[Template()]));
    }

    [Fact]
    public void Missing_rate_quantity_is_deferred_only_when_explicit_and_actual()
    {
        var plan=Plan();var template=Template();
        var missing=template with {Columns=template.Columns.Where(column=>column.Id!=4).ToArray()};
        var selection=plan.Templates[0] with {Fields=plan.Templates[0].Fields.Where(field=>field.Role!="quantity").ToArray()};
        Assert.Throws<IdentityReview>(()=>UploadBindingAdoption.Validate(plan with {Templates=[selection]},61,[missing]));
        var deferred=plan with {Templates=[selection with {Review="rate_quantity_missing"}]};
        Assert.Equal("rate_quantity_missing",Assert.Single(UploadBindingAdoption.Validate(deferred,61,[missing])).Review);
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.Validate(plan with {Templates=[plan.Templates[0] with {Review="rate_quantity_missing"}]},61,[template]));
        Assert.Throws<IdentityReview>(()=>UploadBindingAdoption.Validate(plan with {Templates=[selection with {Review="skip"}]},61,[missing]));
    }

    [Fact]
    public void Imported_rates_do_not_invent_quantity_and_ledgers_are_a_separate_stage()
    {
        var original=Template();var imported=original with {RateSource="imported",Columns=original.Columns.Where(column=>column.Id!=4).ToArray()};
        var plan=Plan();plan=plan with {Templates=[plan.Templates[0] with {Fields=plan.Templates[0].Fields.Where(field=>field.Role!="quantity").ToArray()}]};
        var ledger=original with {Id=99,Purpose="account_ledger"};
        var result=Assert.Single(UploadBindingAdoption.Validate(plan,61,[imported,ledger]));
        Assert.False(result.Bindings.ContainsKey("quantity"));
        Assert.Equal("imported",UploadDerivedFieldService.Bind(imported with {UploadBindings=result.Bindings})!.RateMode);
    }

    [Fact]
    public void Plan_path_cannot_escape_its_owned_directory()
    {
        var owner=Path.Combine(Path.GetTempPath(),"owned-"+Guid.NewGuid().ToString("N"));
        Assert.Equal(Path.Combine(owner,"plan.json"),UploadBindingAdoption.PlanPath(owner,"plan.json"));
        Assert.Throws<InvalidOperationException>(()=>UploadBindingAdoption.PlanPath(owner,"../other/plan.json"));
    }
}
