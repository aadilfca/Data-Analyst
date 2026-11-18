using System.Net;
using Microsoft.AspNetCore.Mvc.Testing;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class ErrorLogManagementTests
{
    [Theory]
    [InlineData("/api/dashboard/stats")]
    [InlineData("/api/dashboard/kpi-stats")]
    [InlineData("/api/dashboard/db-size")]
    [InlineData("/api/dashboard/overdue-uploads")]
    [InlineData("/api/error-logs")]
    [InlineData("/api/error-logs/stats")]
    [InlineData("/api/error-logs/1")]
    public async Task P57_routes_remain_unavailable_by_default(string path)
    {
        await using var app=new WebApplicationFactory<Program>();using var client=app.CreateClient();
        using var result=await client.GetAsync(path,TestContext.Current.CancellationToken);
        Assert.Equal(HttpStatusCode.ServiceUnavailable,result.StatusCode);
        Assert.Equal("no-store",result.Headers.CacheControl?.ToString());
    }
}
