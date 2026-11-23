"""Bounded, opt-in comparison of Python and C# PostgreSQL row fetching.

The live mode compares the same selected Client Data rows and SELECT using
read-only transactions. Evidence contains counts, timings, and hashes only.
It does not start either application or run report calculations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import struct
import subprocess
import sys
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CSHARP_PROJECT = ROOT / "dotnet-uploader" / "tools" / "AuditPlatform.FetchBenchmark" / "AuditPlatform.FetchBenchmark.csproj"
CSHARP_DLL = ROOT / "dotnet-uploader" / "tools" / "AuditPlatform.FetchBenchmark" / "bin" / "Release" / "net10.0" / "AuditPlatform.FetchBenchmark.dll"
DOTNET = ROOT / "dotnet-uploader" / ".dotnet" / "dotnet.exe"
MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024
CURRENT_STAGE = "argument validation"

PYTHON_SQL = """
SELECT cd.id, cd.template_id, cd.upload_id, cd.version, cd.source_fingerprint,
       cd.extract_date::text, cd.extract_amount::double precision::text, cd.row_data::text
FROM public.client_data cd
WHERE cd.client_id=%s AND cd.is_deleted IS FALSE AND cd.upload_id=ANY(%s)
  AND cd.extract_date >= %s::date AND cd.extract_date < %s::date
ORDER BY cd.extract_date NULLS LAST, cd.id
LIMIT %s
""".strip()


class BenchmarkError(RuntimeError):
    """A safe message suitable for display; never include SQL or credentials."""


def validate_args(args: argparse.Namespace) -> tuple[str, str, Path]:
    if not args.live or args.client_id < 1 or args.template_id < 1:
        raise BenchmarkError("Live mode requires positive client and template IDs.")
    if not 2 <= args.repeats <= 3:
        raise BenchmarkError("Choose two or three comparison rounds.")
    try:
        from datetime import date
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        raise BenchmarkError("Use valid ISO dates (YYYY-MM-DD).") from exc
    if start > end or (end - start).days > 366:
        raise BenchmarkError("Choose an ordered period no longer than one year.")
    output = Path(args.output).expanduser().resolve()
    if output.is_relative_to(ROOT) or output.exists() or not output.parent.is_dir():
        raise BenchmarkError("Evidence needs a new file outside the repository.")
    if not DOTNET.is_file() or not CSHARP_PROJECT.is_file():
        raise BenchmarkError("The repository-local .NET SDK or benchmark project is unavailable.")
    return start.isoformat(), end.isoformat(), output


def row_digest(rows: list[dict[str, Any]]) -> tuple[str, int]:
    """Hash the fixed eight-column projection without retaining row data."""
    names = ("id", "template_id", "upload_id", "version", "source_fingerprint",
             "extract_date", "extract_amount", "row_data")
    digest = hashlib.sha256()
    data_bytes = 0
    for row in rows:
        for name in names:
            value = row[name]
            text = None if value is None else str(value)
            encoded = b"" if text is None else text.encode("utf-8")
            data_bytes += len(encoded)
            digest.update(struct.pack("<I", 0xFFFFFFFF if text is None else len(encoded)))
            digest.update(encoded)
    return digest.hexdigest(), data_bytes


def assert_comparable(left: dict[str, Any], right: dict[str, Any]) -> None:
    if any(left[key] != right[key] for key in ("rows", "data_bytes", "sha256")):
        raise BenchmarkError("Python and C# returned different rows; timings were discarded.")


def normalize_csharp_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "success":
        reason = result.get("reason", "unknown")
        if not isinstance(reason, str) or not reason.isidentifier():
            reason = "unknown"
        raise BenchmarkError(f"The C# read-only query failed ({reason}).")
    try:
        return {"rows": result["rows"], "data_bytes": result["dataBytes"],
                "fetch_seconds": result["fetchSeconds"], "hash_seconds": result["hashSeconds"],
                "sha256": result["sha256"]}
    except KeyError as exc:
        raise BenchmarkError("The C# reader returned an incomplete result.") from exc


def run_python_fetch(client_id: int, upload_ids: list[int], start: str, end: str) -> dict[str, Any]:
    import psycopg2.extras
    import database
    from datetime import date, timedelta

    end_exclusive = (date.fromisoformat(end) + timedelta(days=1)).isoformat()

    def read_snapshot(conn):
        started = perf_counter()
        rows = []
        data_bytes_guard = 0
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(PYTHON_SQL, (client_id, upload_ids, start, end_exclusive, MAX_ROWS + 1))
            while True:
                batch = cursor.fetchmany(512)
                if not batch:
                    break
                for source_row in batch:
                    row = dict(source_row)
                    data_bytes_guard += sum(len(str(value).encode("utf-8")) for value in row.values()
                                            if value is not None)
                    if len(rows) >= MAX_ROWS or data_bytes_guard > MAX_BYTES:
                        raise BenchmarkError("The selected period exceeds the benchmark safety limits.")
                    rows.append(row)
        fetch_seconds = perf_counter() - started
        started = perf_counter()
        digest, data_bytes = row_digest(rows)
        hash_seconds = perf_counter() - started
        return {"rows": len(rows), "data_bytes": data_bytes, "fetch_seconds": fetch_seconds,
                "hash_seconds": hash_seconds, "sha256": digest}

    return database.run_read_snapshot_pg(read_snapshot)


def minimal_child_environment(connection_string: str) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "DOTNET_ROOT", "NUGET_PACKAGES")
    env = {key: os.environ[key] for key in keep if os.environ.get(key)}
    env["DOTNET_ROOT"] = str(DOTNET.parent)
    env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
    profile = env.get("USERPROFILE", str(Path.home()))
    env["DOTNET_CLI_HOME"] = profile
    env.setdefault("NUGET_PACKAGES", str(Path(profile) / ".nuget" / "packages"))
    env["AUDIT_FETCH_BENCH_CONNECTION_STRING"] = connection_string
    return env


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    secret_markers = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "ACCESS_KEY",
                      "PRIVATE_KEY", "DATABASE_URL", "SUPABASE", "CONNECTION_STRING",
                      "PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD")
    for key in list(env):
        if any(marker in key.upper() for marker in secret_markers):
            env.pop(key, None)
    env["DOTNET_ROOT"] = str(DOTNET.parent)
    env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
    profile = env.get("USERPROFILE", str(Path.home()))
    env["DOTNET_CLI_HOME"] = profile
    env.setdefault("NUGET_PACKAGES", str(Path(profile) / ".nuget" / "packages"))
    return env


class CSharpReader:
    def __init__(self, connection_string: str):
        env = minimal_child_environment(connection_string)
        build_env = build_environment()
        build = subprocess.run([str(DOTNET), "build", str(CSHARP_PROJECT), "--configuration", "Release",
                                "--nologo", "--verbosity", "quiet", "-p:NuGetAudit=false"], cwd=ROOT, env=build_env,
                               capture_output=True, text=True, timeout=180, check=False)
        if build.returncode != 0 or not CSHARP_DLL.is_file():
            raise BenchmarkError("The local C# benchmark tool did not build; no comparison was run.")
        self.process = subprocess.Popen([str(DOTNET), str(CSHARP_DLL)], cwd=ROOT, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1)

    def fetch(self, client_id: int, upload_ids: list[int], start: str, end: str) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        request = {"clientId": client_id, "uploadIds": upload_ids, "periodStart": start,
                   "periodEnd": end, "maximumRows": MAX_ROWS}
        self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise BenchmarkError("The C# reader ended before returning a result.")
        try:
            result = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError("The C# reader returned an invalid result.") from exc
        return normalize_csharp_result(result)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def live_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    global CURRENT_STAGE
    CURRENT_STAGE = "argument validation"
    start, end, output = validate_args(args)
    # Importing the existing DB module reads its ignored local .env; the URL is
    # passed only to the child process environment and is never logged or saved.
    os.environ["PG_POOL_MIN"] = "1"
    os.environ["PG_POOL_MAX"] = "1"
    CURRENT_STAGE = "Python database connection"
    import database
    from services.ledger_report_source import select_latest_template_upload_ids

    database.get_pg_pool()
    CURRENT_STAGE = "read-only upload selection"
    upload_ids, _ = select_latest_template_upload_ids(
        args.client_id, args.template_id, database.execute_query_pg)
    if not upload_ids:
        raise BenchmarkError("No active uploads match the selected client and template.")
    if len(upload_ids) > 10_000:
        raise BenchmarkError("The selection exceeds the 10,000-upload safety limit.")
    python_connection = os.environ.get("DATABASE_URL", "")
    csharp_connection = os.environ.get("AUDIT_FETCH_BENCH_CSHARP_CONNECTION_STRING", "")
    if not python_connection:
        raise BenchmarkError("The existing Python database configuration is unavailable.")

    CURRENT_STAGE = "C# benchmark build and startup"
    reader = CSharpReader(csharp_connection or python_connection)
    runs: list[dict[str, Any]] = []
    try:
        for round_number in range(1, args.repeats + 1):
            order = ("python", "csharp") if round_number % 2 else ("csharp", "python")
            paired: dict[str, Any] = {}
            for method in order:
                CURRENT_STAGE = f"round {round_number} {method} read"
                result = run_python_fetch(args.client_id, upload_ids, start, end) if method == "python" \
                    else reader.fetch(args.client_id, upload_ids, start, end)
                paired[method] = result
            left, right = paired["python"], paired["csharp"]
            assert_comparable(left, right)
            for method in order:
                result = paired[method]
                runs.append({"round": round_number, "method": method,
                             "rows": result["rows"], "data_bytes": result["data_bytes"],
                             "fetch_seconds": result["fetch_seconds"],
                             "hash_seconds": result["hash_seconds"], "sha256": result["sha256"]})
    finally:
        reader.close()

    python_times = [run["fetch_seconds"] for run in runs if run["method"] == "python"]
    csharp_times = [run["fetch_seconds"] for run in runs if run["method"] == "csharp"]
    python_median, csharp_median = statistics.median(python_times), statistics.median(csharp_times)
    summary = {
        "python_median_fetch_seconds": python_median,
        "csharp_median_fetch_seconds": csharp_median,
        "csharp_improvement_percent": 100 * (python_median - csharp_median) / python_median
            if python_median else 0.0,
        "csharp_faster_each_round": all(
            next(run["fetch_seconds"] for run in runs if run["round"] == number and run["method"] == "csharp")
            < next(run["fetch_seconds"] for run in runs if run["round"] == number and run["method"] == "python")
            for number in range(1, args.repeats + 1)),
    }
    evidence = {
        "version": 1,
        "scope": {"client_id": args.client_id, "template_id": args.template_id,
                  "period_start": start, "period_end": end,
                  "selected_upload_count": len(upload_ids), "maximum_rows": MAX_ROWS},
        "comparison": "Same selected uploads, parameterized Client Data SELECT, and PostgreSQL source; alternating warm reads.",
        "result_match": True,
        "timing_scope": "Database query, transfer, and row decoding; excludes upload selection, authentication, calculations, UI, connection startup, and hashing.",
        "runs": runs,
        "summary": summary,
        "limits": "Three laptop rounds show this run only; results are not a cloud or service-level guarantee.",
    }
    CURRENT_STAGE = "evidence write"
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Compare guarded Python/C# PostgreSQL row fetches.")
    result.add_argument("--live", action="store_true", help="Explicitly permit the read-only database comparison.")
    result.add_argument("--client-id", type=int, required=True)
    result.add_argument("--template-id", type=int, required=True)
    result.add_argument("--start", required=True, help="ISO start date, inclusive")
    result.add_argument("--end", required=True, help="ISO end date, inclusive")
    result.add_argument("--repeats", type=int, default=3)
    result.add_argument("--output", required=True, help="New evidence JSON path outside the repository")
    return result


def main() -> int:
    global CURRENT_STAGE
    try:
        evidence = live_benchmark(parser().parse_args())
    except BenchmarkError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"The guarded comparison failed during {CURRENT_STAGE} ({type(exc).__name__}); no evidence was written.",
              file=sys.stderr)
        return 1
    print(json.dumps({"result_match": evidence["result_match"],
                      "rows": evidence["runs"][0]["rows"],
                      "summary": evidence["summary"],
                      "evidence_path": "written outside the repository"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
