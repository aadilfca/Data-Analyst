using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Health;
using Microsoft.Extensions.Options;

var builder = WebApplication.CreateBuilder(args);

// Console-only structured logs work on Windows and container hosts without
// requiring permission to register or write a Windows Event Log source.
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole();

builder.Services.AddControllers();

// Default configuration supports appsettings and Platform__... environment
// overrides. Validate at startup so invalid settings cannot reach a request.
builder.Services.AddOptions<PlatformOptions>()
    .Bind(builder.Configuration.GetSection(PlatformOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();

builder.Services.AddOptions<PostgresOptions>()
    .Bind(builder.Configuration.GetSection(PostgresOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();
builder.Services.AddSingleton<PostgresDataSourceFactory>();
builder.Services.AddSingleton<PostgresTransactions>();
builder.Services.AddSingleton<PostgresReadSnapshot>();
builder.Services.AddSingleton<PostgresReadinessCheck>();

var app = builder.Build();

app.MapControllers();

try
{
    app.Run();
    return 0;
}
catch (OptionsValidationException)
{
    // The host logs the validation details. Return a predictable failure code
    // instead of leaving startup failure to the operating-system crash handler.
    return 1;
}

// Expose the entry point to the in-process ASP.NET Core integration tests.
public partial class Program { }
