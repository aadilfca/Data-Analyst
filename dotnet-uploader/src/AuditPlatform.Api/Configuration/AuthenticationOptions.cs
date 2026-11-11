using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

// The externally visible origin is configured by the operator, never inferred
// from an untrusted Host or forwarded header.
public sealed class AuthenticationOptions : IValidatableObject
{
    public const string SectionName = "Authentication";
    public bool Enabled { get; set; }
    public string PublicOrigin { get; set; } = "";

    public IEnumerable<ValidationResult> Validate(ValidationContext validationContext)
    {
        if (PublicOrigin.Length == 0 && !Enabled) yield break;
        if (!Uri.TryCreate(PublicOrigin, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttps && uri.Scheme != Uri.UriSchemeHttp)
            || (uri.Scheme == Uri.UriSchemeHttp && uri.Host != "127.0.0.1" && uri.Host != "localhost")
            || uri.UserInfo.Length != 0 || uri.AbsolutePath != "/"
            || uri.Query.Length != 0 || uri.Fragment.Length != 0
            || PublicOrigin != uri.GetLeftPart(UriPartial.Authority))
            yield return new ValidationResult("Authentication requires a canonical public origin.");
    }
}
