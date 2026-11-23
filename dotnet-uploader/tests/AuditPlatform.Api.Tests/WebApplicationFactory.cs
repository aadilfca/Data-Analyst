using Microsoft.AspNetCore.Hosting;

namespace AuditPlatform.Api.Tests;

// Keep test hosts independent of a developer's Development environment and user secrets.
public sealed class WebApplicationFactory<TEntryPoint>
    : Microsoft.AspNetCore.Mvc.Testing.WebApplicationFactory<TEntryPoint>
    where TEntryPoint : class
{
    private readonly Action<IWebHostBuilder>[] _configuration;

    public WebApplicationFactory() : this([]) { }

    private WebApplicationFactory(Action<IWebHostBuilder>[] configuration)
    {
        _configuration = configuration;
    }

    public new WebApplicationFactory<TEntryPoint> WithWebHostBuilder(Action<IWebHostBuilder> configuration) =>
        new(_configuration.Append(configuration).ToArray());

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        base.ConfigureWebHost(builder);
        builder.UseEnvironment("Testing");
        foreach (var configure in _configuration) configure(builder);
    }
}
