using AuditPlatform.Api.Verification;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class VerificationLegacyReadOnlyTests
{
    [Theory]
    [InlineData(53)]
    [InlineData(308)]
    public void Archived_rulebook_versions_are_immutable(int version) =>
        Assert.True(VerificationSessionService.IsReadOnlyLegacyRulebookVersion(version));

    [Theory]
    [InlineData(311)]
    [InlineData(0)]
    public void Other_rulebook_versions_keep_existing_lifecycle(int version) =>
        Assert.False(VerificationSessionService.IsReadOnlyLegacyRulebookVersion(version));

    [Fact]
    public void Missing_rulebook_version_is_not_assumed_to_be_an_archived_build() =>
        Assert.False(VerificationSessionService.IsReadOnlyLegacyRulebookVersion(null));
}
