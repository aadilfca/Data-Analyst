using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class CoexistenceOptions : IValidatableObject
{
    public const string SectionName = "Coexistence";
    public bool Enabled { get; set; }
    public string LegacyBaseAddress { get; set; } = "";
    public int TimeoutSeconds { get; set; } = 30;
    public int ConnectTimeoutSeconds { get; set; } = 3;
    public int MaximumConcurrentRequests { get; set; } = 4;
    public int MaximumRequestBytes { get; set; } = 8 * 1024 * 1024;
    public int MaximumResponseBytes { get; set; } = 16 * 1024 * 1024;

    public IEnumerable<ValidationResult> Validate(ValidationContext validationContext)
    {
        if (TimeoutSeconds is < 1 or > 120 || ConnectTimeoutSeconds is < 1 or > 10
            || ConnectTimeoutSeconds > TimeoutSeconds)
            yield return new("Coexistence requires bounded connection and total timeouts.");
        if (MaximumConcurrentRequests is < 1 or > 16)
            yield return new("Coexistence concurrency must be between 1 and 16.");
        if (MaximumRequestBytes is < 1 or > 32 * 1024 * 1024
            || MaximumResponseBytes is < 1 or > 64 * 1024 * 1024)
            yield return new("Coexistence payload limits are outside the supported rehearsal range.");
        if (Enabled && !TryGetTarget(out _))
            yield return new("Coexistence requires an explicit HTTP IPv4 loopback origin and nonprivileged port.");
    }

    public bool TryGetTarget(out Uri? target)
    {
        // Literal IPv4 avoids DNS/proxy surprises. This phase supports local
        // rehearsal only; remote/container topology needs a separate review.
        target = null;
        if (!Uri.TryCreate(LegacyBaseAddress, UriKind.Absolute, out var parsed)
            || parsed.Scheme != "http" || parsed.Host != "127.0.0.1"
            || parsed.Port is < 1024 or > 65535 || parsed.UserInfo.Length != 0
            || parsed.AbsolutePath != "/" || parsed.Query.Length != 0 || parsed.Fragment.Length != 0
            || LegacyBaseAddress != $"http://127.0.0.1:{parsed.Port}/") return false;
        target = parsed;
        return true;
    }
}
