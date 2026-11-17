using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Coexistence;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Health;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Middleware;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Users;
using AuditPlatform.Api.Clients;
using AuditPlatform.Api.Shell;
using AuditPlatform.Api.Settings;
using AuditPlatform.Api.Columns;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.ConfigurationBackup;
using AuditPlatform.Api.Uploads;
using AuditPlatform.Api.DataBank;
using AuditPlatform.Api.TrialBalance;
using AuditPlatform.Api.Verification;
using AuditPlatform.Api.Reconciliation;
using Microsoft.Extensions.Options;

var builder = WebApplication.CreateBuilder(args);

// Console-only structured logs work on Windows and container hosts without
// requiring permission to register or write a Windows Event Log source.
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole();

builder.Services.AddControllers();
builder.Services.AddOptions<CoexistenceOptions>()
    .Bind(builder.Configuration.GetSection(CoexistenceOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();
builder.Services.AddSingleton(provider => new CoexistenceRouteTable(provider.GetServices<CoexistenceRoute>()));
builder.Services.AddSingleton<ICoexistenceAccessGate, SessionCoexistenceGate>();
builder.Services.AddSingleton(provider => new LegacyForwarder(
    provider.GetRequiredService<IOptions<CoexistenceOptions>>(),
    provider.GetRequiredService<ILogger<LegacyForwarder>>()));

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
builder.Services.AddOptions<LogPersistenceOptions>()
    .Bind(builder.Configuration.GetSection(LogPersistenceOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();
builder.Services.AddSingleton<LogSanitizer>();
builder.Services.AddSingleton<PostgresAuditWriter>();
builder.Services.AddSingleton<DiagnosticErrorLogger>();
builder.Services.AddOptions<AuthenticationOptions>()
    .Bind(builder.Configuration.GetSection(AuthenticationOptions.SectionName))
    .ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddOptions<UserManagementOptions>()
    .Bind(builder.Configuration.GetSection(UserManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<PostgresSessionStore>();
builder.Services.AddSingleton<AuthenticationService>();
// Internal foundation only; no public route or database migration is enabled.
builder.Services.AddSingleton<TenantContextResolver>();
builder.Services.AddSingleton<EngagementAccessResolver>();
builder.Services.AddSingleton<TenantUserService>();
builder.Services.AddOptions<ClientManagementOptions>()
    .Bind(builder.Configuration.GetSection(ClientManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantClientService>();
builder.Services.AddOptions<ApplicationShellOptions>()
    .Bind(builder.Configuration.GetSection(ApplicationShellOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantSessionService>();
builder.Services.AddOptions<SettingsManagementOptions>()
    .Bind(builder.Configuration.GetSection(SettingsManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantSettingsService>();
builder.Services.AddOptions<ColumnManagementOptions>()
    .Bind(builder.Configuration.GetSection(ColumnManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantColumnService>();
builder.Services.AddOptions<TemplateManagementOptions>()
    .Bind(builder.Configuration.GetSection(TemplateManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantTemplateService>();
builder.Services.AddOptions<SavedMappingOptions>()
    .Bind(builder.Configuration.GetSection(SavedMappingOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantMappingService>();
builder.Services.AddOptions<ConfigurationBackupOptions>()
    .Bind(builder.Configuration.GetSection(ConfigurationBackupOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<ConfigurationBackupService>();
builder.Services.AddOptions<UploadStagingOptions>()
    .Bind(builder.Configuration.GetSection(UploadStagingOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<UploadStagingService>();
builder.Services.AddOptions<UploadPreparationOptions>()
    .Bind(builder.Configuration.GetSection(UploadPreparationOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<UploadPreparationService>();
builder.Services.AddOptions<UploadInputValidationOptions>()
    .Bind(builder.Configuration.GetSection(UploadInputValidationOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<UploadInputValidationService>();
builder.Services.AddOptions<UploadFootingOptions>()
    .Bind(builder.Configuration.GetSection(UploadFootingOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<UploadFootingService>();
builder.Services.AddOptions<UploadCoverageOptions>()
    .Bind(builder.Configuration.GetSection(UploadCoverageOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<UploadCoverageService>();
builder.Services.AddOptions<UploadIdentityOptions>()
    .Bind(builder.Configuration.GetSection(UploadIdentityOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<UploadIdentityService>();
builder.Services.AddOptions<UploadCorrectionOptions>()
    .Bind(builder.Configuration.GetSection(UploadCorrectionOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<UploadCorrectionService>();
builder.Services.AddOptions<UploadValidationPlanOptions>()
    .Bind(builder.Configuration.GetSection(UploadValidationPlanOptions.SectionName)).ValidateDataAnnotations()
    .Validate(o=>o.ValidKey(),"Enabled validation plans require a base64 signing key of 32–64 bytes.").ValidateOnStart();
builder.Services.AddSingleton<UploadValidationPlanStore>();
builder.Services.AddSingleton<UploadValidationPlanSigner>();
builder.Services.AddSingleton<UploadValidationPlanService>();

builder.Services.AddOptions<UploadImportOptions>()
    .Bind(builder.Configuration.GetSection(UploadImportOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<UploadImportWriter>();
builder.Services.AddSingleton<UploadImportService>();
builder.Services.AddSingleton<UploadImportReceiptService>();
builder.Services.AddOptions<UploadJobOptions>()
    .Bind(builder.Configuration.GetSection(UploadJobOptions.SectionName)).ValidateDataAnnotations()
    .Validate(o=>o.ValidStorage(),"Enabled upload jobs require private storage configuration.").ValidateOnStart();
builder.Services.AddSingleton<UploadJobStore>();
builder.Services.AddSingleton(provider=>new UploadJobSourceStore(provider.GetRequiredService<IOptions<UploadJobOptions>>(),
    new HttpClient(new HttpClientHandler{AllowAutoRedirect=false,UseCookies=false}){Timeout=Timeout.InfiniteTimeSpan}));
builder.Services.AddSingleton<UploadJobService>();
builder.Services.AddHostedService<UploadJobWorker>();
builder.Services.AddOptions<UploadUiOptions>()
    .Bind(builder.Configuration.GetSection(UploadUiOptions.SectionName)).ValidateOnStart();
builder.Services.AddOptions<DataBankOptions>()
    .Bind(builder.Configuration.GetSection(DataBankOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<DataBankService>();
builder.Services.AddOptions<TrialBalanceReviewOptions>()
    .Bind(builder.Configuration.GetSection(TrialBalanceReviewOptions.SectionName)).ValidateDataAnnotations()
    .Validate(o=>o.Valid(),"Enabled Trial Balance review requires a signing key; its UI requires review.").ValidateOnStart();
builder.Services.AddSingleton(sp=>new TrialBalanceReviewService(sp.GetRequiredService<PostgresTransactions>(),
    sp.GetRequiredService<PostgresReadSnapshot>(),sp.GetRequiredService<PostgresAuditWriter>(),sp.GetRequiredService<IOptions<TrialBalanceReviewOptions>>(),
    sp.GetRequiredService<IOptions<PlatformOptions>>().Value.DatabaseCapacityLimitBytes));
builder.Services.AddOptions<VerificationSetupOptions>()
    .Bind(builder.Configuration.GetSection(VerificationSetupOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<VerificationSetupService>();
builder.Services.AddOptions<ReconciliationSetupOptions>()
    .Bind(builder.Configuration.GetSection(ReconciliationSetupOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<ReconciliationSetupService>();
builder.Services.AddOptions<DataBankExportOptions>()
    .Bind(builder.Configuration.GetSection(DataBankExportOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<DataBankExportService>();
builder.Services.AddOptions<DataIntegrityOptions>()
    .Bind(builder.Configuration.GetSection(DataIntegrityOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<AuditPlatform.Api.DataIntegrity.DatabaseSafeguardService>();
builder.Services.AddSingleton<AuditPlatform.Api.DataIntegrity.DataIntegrityService>();
builder.Services.AddOptions<StorageCleanupOptions>()
    .Bind(builder.Configuration.GetSection(StorageCleanupOptions.SectionName)).ValidateDataAnnotations()
    .Validate(o=>o.ValidKey(),"Enabled cleanup requires a base64 signing key of 32–64 bytes.").ValidateOnStart();
builder.Services.AddSingleton<AuditPlatform.Api.StorageCleanup.StorageCleanupService>();
builder.Services.AddSingleton<AuditPlatform.Api.StorageCleanup.StoragePurgeService>();
builder.Services.AddSingleton<AuditPlatform.Api.StorageCleanup.StorageArtifactCleanupService>();
builder.Services.AddHostedService<AuditPlatform.Api.StorageCleanup.StorageCleanupWorker>();

var app = builder.Build();
if(app.Services.GetRequiredService<IOptions<TrialBalanceReviewOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
        ||!app.Services.GetRequiredService<IOptions<PostgresOptions>>().Value.Enabled))
    throw new OptionsValidationException(TrialBalanceReviewOptions.SectionName,typeof(TrialBalanceReviewOptions),["Trial Balance review requires PostgreSQL and the authenticated application shell."]);
var cleanupOptions=app.Services.GetRequiredService<IOptions<StorageCleanupOptions>>().Value;
if(cleanupOptions.Enabled&&!app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value.Enabled
    ||cleanupOptions.PurgeEnabled&&(!cleanupOptions.Enabled
        ||!app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value.HistoryEnabled
        ||!app.Services.GetRequiredService<IOptions<DataBankExportOptions>>().Value.Enabled
        ||!app.Services.GetRequiredService<IOptions<UploadStagingOptions>>().Value.Enabled)
    ||cleanupOptions.ArtifactWorkerEnabled&&(!cleanupOptions.Enabled||!cleanupOptions.PurgeEnabled))
    throw new OptionsValidationException(StorageCleanupOptions.SectionName,typeof(StorageCleanupOptions),
        ["Cleanup requires scoped Data Bank reads; purge additionally requires complete backups and owned staging. Artifact recovery requires purge."]);
if (app.Services.GetRequiredService<IOptions<DataIntegrityOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<PostgresOptions>>().Value.Enabled))
    throw new OptionsValidationException(DataIntegrityOptions.SectionName, typeof(DataIntegrityOptions),
        ["Data Integrity requires PostgreSQL and the authenticated application shell."]);
if(app.Services.GetRequiredService<IOptions<VerificationSetupOptions>>().Value.Enabled
    &&(!app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
        ||!app.Services.GetRequiredService<IOptions<PostgresOptions>>().Value.Enabled))
    throw new OptionsValidationException(VerificationSetupOptions.SectionName,typeof(VerificationSetupOptions),
        ["Verification setup requires PostgreSQL and the authenticated application shell."]);
if (app.Services.GetRequiredService<IOptions<ReconciliationSetupOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<PostgresOptions>>().Value.Enabled))
    throw new OptionsValidationException(ReconciliationSetupOptions.SectionName, typeof(ReconciliationSetupOptions),
        ["Reconciliation setup requires PostgreSQL and the authenticated application shell."]);
if ((app.Services.GetRequiredService<IOptions<DataBankExportOptions>>().Value.Enabled
        || app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value.HistoryEnabled)
    && !app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value.Enabled)
    throw new OptionsValidationException(DataBankOptions.SectionName, typeof(DataBankOptions), ["History and exports require Data Bank reads."]);
if (app.Services.GetRequiredService<IOptions<DataBankOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<TemplateManagementOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<SettingsManagementOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<PostgresOptions>>().Value.Enabled))
    throw new OptionsValidationException(DataBankOptions.SectionName, typeof(DataBankOptions),
        ["Data Bank requires PostgreSQL, the application shell, tenant templates and settings."]);
if(app.Services.GetRequiredService<IOptions<UploadUiOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<UploadJobOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled))
    throw new OptionsValidationException(UploadUiOptions.SectionName,typeof(UploadUiOptions),
        ["Native Upload UI requires the application shell, durable jobs and their prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadJobOptions>>().Value.Enabled
    &&(!app.Services.GetRequiredService<IOptions<UploadImportOptions>>().Value.Enabled
        ||app.Services.GetRequiredService<IOptions<UploadJobOptions>>().Value.LeaseSeconds
            <=app.Services.GetRequiredService<IOptions<UploadImportOptions>>().Value.TimeoutSeconds
                +2*app.Services.GetRequiredService<IOptions<UploadJobOptions>>().Value.HeartbeatSeconds))
    throw new OptionsValidationException(UploadJobOptions.SectionName,typeof(UploadJobOptions),["Jobs require atomic imports and a lease longer than the import deadline plus heartbeat margin."]);
if(app.Services.GetRequiredService<IOptions<UploadImportOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadValidationPlanOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadImportOptions.SectionName,typeof(UploadImportOptions),["Atomic imports require signed validation plans and their prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadValidationPlanOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadCorrectionOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadValidationPlanOptions.SectionName,typeof(UploadValidationPlanOptions),["Plans require source-bound correction review and its prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadCorrectionOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadIdentityOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadCorrectionOptions.SectionName,typeof(UploadCorrectionOptions),["Corrections require source-bound identity checks and their prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadIdentityOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadCoverageOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadIdentityOptions.SectionName,typeof(UploadIdentityOptions),["Identity checks require source-bound coverage and its prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadCoverageOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadFootingOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadCoverageOptions.SectionName,typeof(UploadCoverageOptions),
        ["Coverage requires Footing, input validation and their source-bound tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadFootingOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadInputValidationOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadFootingOptions.SectionName,typeof(UploadFootingOptions),
        ["Footing requires input validation and its source-bound tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadInputValidationOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadPreparationOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadInputValidationOptions.SectionName,typeof(UploadInputValidationOptions),
        ["Input validation requires source-bound upload preparation and its tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadPreparationOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<UploadStagingOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<SavedMappingOptions>>().Value.Enabled))
    throw new OptionsValidationException(UploadPreparationOptions.SectionName,typeof(UploadPreparationOptions),
        ["Upload preparation requires owned staging, saved mappings and their tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadStagingOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<TemplateManagementOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadStagingOptions.SectionName,typeof(UploadStagingOptions),["Upload staging requires native templates and tenant context."]);
if (app.Services.GetRequiredService<IOptions<ConfigurationBackupOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<SettingsManagementOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<SavedMappingOptions>>().Value.Enabled))
    throw new OptionsValidationException(ConfigurationBackupOptions.SectionName,typeof(ConfigurationBackupOptions),
        ["Configuration backup requires tenant settings, saved mappings and their prerequisites."]);
if (app.Services.GetRequiredService<IOptions<SavedMappingOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<TemplateManagementOptions>>().Value.Enabled)
    throw new OptionsValidationException(SavedMappingOptions.SectionName,typeof(SavedMappingOptions),
        ["Saved mappings require native templates and their tenant context."]);
if (app.Services.GetRequiredService<IOptions<TemplateManagementOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<ColumnManagementOptions>>().Value.Enabled))
    throw new OptionsValidationException(TemplateManagementOptions.SectionName,typeof(TemplateManagementOptions),
        ["Tenant templates require the application shell and tenant columns."]);
if (app.Services.GetRequiredService<IOptions<ColumnManagementOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled)
    throw new OptionsValidationException(ColumnManagementOptions.SectionName,typeof(ColumnManagementOptions),
        ["Tenant columns require the application shell and its session tenant context."]);
if (app.Services.GetRequiredService<IOptions<SettingsManagementOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled)
    throw new OptionsValidationException(SettingsManagementOptions.SectionName,typeof(SettingsManagementOptions),
        ["Tenant settings require the application shell and its session tenant context."]);
if (app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<ClientManagementOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<UserManagementOptions>>().Value.Enabled))
    throw new OptionsValidationException(ApplicationShellOptions.SectionName,typeof(ApplicationShellOptions),
        ["Application shell requires authentication, user management and client management."]);
// Invalid/duplicate registrations fail before listening. Production currently
// registers no bridge routes and retains P07's default access block.
_ = app.Services.GetRequiredService<CoexistenceRouteTable>();

app.UseMiddleware<ExceptionLoggingMiddleware>();
app.UseRouting();
app.UseMiddleware<SessionAuthenticationHandler>();
app.UseWhen(context => context.RequestServices.GetRequiredService<IOptions<AuthenticationOptions>>().Value.Enabled
    && (context.Request.Path.StartsWithSegments("/api/auth")
    || context.Request.Path.StartsWithSegments("/api/storage-cleanup")
    || context.Request.Path.StartsWithSegments("/api/users")
    || context.Request.Path.StartsWithSegments("/api/clients")
    || context.Request.Path.StartsWithSegments("/api/settings")
    || context.Request.Path.StartsWithSegments("/api/columns")
    || context.Request.Path.StartsWithSegments("/api/templates")
    || context.Request.Path.StartsWithSegments("/api/backup")
    || context.Request.Path.StartsWithSegments("/api/account-control-center")
    || context.Request.Path.StartsWithSegments("/api/reconciliation")
    || context.Request.Path.StartsWithSegments("/api/import-excel")
    || context.Request.Path.StartsWithSegments("/api/upload/staging")
    || context.Request.Path.StartsWithSegments("/api/upload/file")
    || context.Request.Path.StartsWithSegments("/api/upload/save-mapping")
    || context.Request.Path.StartsWithSegments("/api/upload/saved-mapping")
    || (context.RequestServices.GetRequiredService<IOptions<CoexistenceOptions>>().Value.Enabled
        && context.RequestServices.GetRequiredService<CoexistenceRouteTable>()
            .Find(context.Request.Method, context.Request.Path.Value ?? "") is not null)),
    branch => branch.UseMiddleware<CsrfMiddleware>());
app.UseMiddleware<ApplicationShellMiddleware>();
app.UseMiddleware<CoexistenceMiddleware>();
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
