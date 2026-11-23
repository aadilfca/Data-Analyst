using System.IO.Compression;
using System.Text.Json;

namespace AuditPlatform.Api.Tests;

internal static class NativeCompatibilityFixtureFactory
{
    private static readonly string[] Scenarios =
    [
        "p17", "p18", "p19", "p20", "p21", "p22", "p23", "p24", "p25", "p26", "p27",
        "p29", "p34", "p35", "p36"
    ];

    public static string CreateWorkspace(string scenario, string prefix)
    {
        var root = FindSolutionDirectory();
        var directory = Path.Combine(root, ".tools", $"{prefix}-{Guid.NewGuid():N}");
        ExtractScenario(scenario, directory);
        return directory;
    }

    public static void ExtractScenario(string scenario, string directory)
    {
        if (!Scenarios.Contains(scenario, StringComparer.Ordinal))
            throw new ArgumentOutOfRangeException(nameof(scenario), scenario, "Unknown native fixture scenario.");

        var destination = Path.GetFullPath(directory);
        if (Directory.Exists(destination) && Directory.EnumerateFileSystemEntries(destination).Any())
            throw new IOException("Fixture destination must be empty.");
        Directory.CreateDirectory(destination);
        var archivePath = Path.Combine(AppContext.BaseDirectory, "Fixtures", "native-compatibility-fixtures.zip");
        if (!File.Exists(archivePath))
            throw new FileNotFoundException("The native test fixture archive was not copied to the test output.", archivePath);

        using var archive = ZipFile.OpenRead(archivePath);
        var prefix = scenario + "/";
        var found = false;
        foreach (var entry in archive.Entries.Where(entry => entry.FullName.StartsWith(prefix, StringComparison.Ordinal)))
        {
            if (string.IsNullOrEmpty(entry.Name)) continue;
            var relative = entry.FullName[prefix.Length..].Replace('/', Path.DirectorySeparatorChar);
            var target = Path.GetFullPath(Path.Combine(destination, relative));
            var boundary = destination.EndsWith(Path.DirectorySeparatorChar)
                ? destination
                : destination + Path.DirectorySeparatorChar;
            if (!target.StartsWith(boundary, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Fixture archive contains a path outside its scenario folder.");

            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            using var source = entry.Open();
            using var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            source.CopyTo(output);
            found = true;
        }

        if (!found) throw new InvalidDataException($"The fixture archive has no files for scenario '{scenario}'.");
    }

    public static JsonDocument ReadOracle(string directory)
        => JsonDocument.Parse(File.ReadAllText(Path.Combine(directory, "oracle.json")));

    private static string FindSolutionDirectory()
    {
        for (var path = new DirectoryInfo(AppContext.BaseDirectory); path is not null; path = path.Parent)
            if (File.Exists(Path.Combine(path.FullName, "AuditPlatform.slnx"))) return path.FullName;
        throw new InvalidOperationException("Could not find the AuditPlatform solution directory.");
    }
}
