"""Cross-process regressions for cached suffix-list grouping and CLI reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from tldextract.cache import DiskCache

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SEPARATOR = "// ===BEGIN PRIVATE DOMAINS==="
CACHE_FILE_SUFFIX = ".tldextract.json"

PYTHON_EXIT_SUFFIX_NOT_FOUND = 23


@dataclass(frozen=True)
class SourceRules:
    """A distinguishable sample suffix list and the hosts read through it."""

    name: str
    public: tuple[str, ...]
    private: tuple[str, ...]
    hosts: tuple[str, ...]

    @property
    def content(self) -> str:
        """Render the sample as a PSL-shaped document."""
        lines = [*self.public, PRIVATE_SEPARATOR, *self.private, ""]
        return "\n".join(lines)


SOURCE_A = SourceRules(
    name="A",
    public=("alpha", "service.alpha"),
    private=("tenant.alpha",),
    hosts=(
        "www.example.alpha",
        "api.service.alpha",
        "deep.shop.tenant.alpha",
    ),
)
SOURCE_B = SourceRules(
    name="B",
    public=("bravo", "service.bravo"),
    private=("tenant.bravo",),
    hosts=(
        "www.example.bravo",
        "api.service.bravo",
        "deep.shop.tenant.bravo",
    ),
)
SNAPSHOT_BLOGSPOT = SourceRules(
    name="snapshot-blogspot",
    public=("com",),
    private=("blogspot.com",),
    hosts=("waiterrant.blogspot.com",),
)


def _matching_rule(
    labels: Sequence[str], rules: Sequence[tuple[str, bool]]
) -> tuple[str, bool] | None:
    best: tuple[str, bool] | None = None
    for rule, is_private in rules:
        rule_labels = rule.split(".")
        if len(rule_labels) > len(labels):
            continue
        if list(labels[-len(rule_labels) :]) != rule_labels:
            continue
        if best is None or len(rule_labels) > len(best[0].split(".")):
            best = (rule, is_private)
    return best


def expected_fields(
    host: str, source: SourceRules, include_psl_private_domains: bool
) -> dict[str, object]:
    """Compute expected extraction fields independently from the sample rules."""
    labels = host.split(".")
    rules = [(rule, False) for rule in source.public]
    if include_psl_private_domains:
        rules.extend((rule, True) for rule in source.private)

    match = _matching_rule(labels, rules)
    if match is None:
        subdomain = ".".join(labels[:-1])
        domain = labels[-1]
        suffix = ""
        is_private = False
        registry_suffix = ""
    else:
        suffix, is_private = match
        suffix_labels = suffix.split(".")
        suffix_index = len(labels) - len(suffix_labels)
        subdomain = ".".join(labels[: suffix_index - 1]) if suffix_index >= 2 else ""
        domain = labels[suffix_index - 1] if suffix_index > 0 else ""
        registry_suffix = suffix
        if is_private:
            public_match = _matching_rule(
                labels, [(rule, False) for rule in source.public]
            )
            registry_suffix = public_match[0] if public_match else ""

    fqdn = (
        ".".join(part for part in (subdomain, domain, suffix) if part)
        if suffix and (domain or is_private)
        else ""
    )
    top_public = f"{domain}.{suffix}" if suffix and domain else ""
    top_registry = top_public
    if top_public and is_private and registry_suffix:
        num_labels = registry_suffix.count(".") + 2
        top_registry = ".".join(top_public.split(".")[-num_labels:])
    elif top_public and is_private:
        top_registry = ""

    reverse_parts = [part for part in (suffix, domain) if part]
    if subdomain:
        reverse_parts.extend(reversed(subdomain.split(".")))

    return {
        "subdomain": subdomain,
        "domain": domain,
        "suffix": suffix,
        "is_private": is_private,
        "registry_suffix": registry_suffix,
        "fqdn": fqdn,
        "ipv4": "",
        "ipv6": "",
        "registered_domain": top_public,
        "reverse_domain_name": ".".join(reverse_parts),
        "top_domain_under_public_suffix": top_public,
        "top_domain_under_registry_suffix": top_registry,
    }


class _OverlapGate:
    """Block good-endpoint responses until both endpoints have been requested."""

    def __init__(self, endpoints: frozenset[str], timeout: float) -> None:
        self._condition = threading.Condition()
        self._endpoints = endpoints
        self._timeout = timeout
        self._seen: set[str] = set()
        self.timed_out = False

    def wait_for_overlap(self, endpoint: str) -> bool:
        with self._condition:
            if self.timed_out:
                return False
            self._seen.add(endpoint)
            self._condition.notify_all()
            deadline = time.monotonic() + self._timeout
            while not self._endpoints <= self._seen:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.timed_out = True
                    self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            return True


class _SuffixServer(ThreadingHTTPServer):
    """Local HTTP server that serves configured bodies and counts requests."""

    daemon_threads = True

    def __init__(
        self,
        routes: Mapping[str, tuple[int, str]],
        gate: _OverlapGate | None,
    ) -> None:
        self.routes = dict(routes)
        self.gate = gate
        self._count_lock = threading.Lock()
        self._counts = dict.fromkeys(self.routes, 0)
        super().__init__(("127.0.0.1", 0), _SuffixHandler)

    @property
    def base_url(self) -> str:
        """Return the loopback base URL for this server."""
        return f"http://127.0.0.1:{self.server_address[1]}"

    @property
    def counts(self) -> dict[str, int]:
        """Return a consistent snapshot of per-path request counts."""
        with self._count_lock:
            return dict(self._counts)

    def respond(self, path: str) -> tuple[int, str]:
        """Count and answer a request, enforcing the overlap gate if configured."""
        with self._count_lock:
            self._counts[path] = self._counts.get(path, 0) + 1

        status, body = self.routes.get(path, (404, "not found"))
        if status == 200 and self.gate is not None:
            if not self.gate.wait_for_overlap(path):
                return 500, f"overlap gate timed out for {path}"
        return status, body


class _SuffixHandler(BaseHTTPRequestHandler):
    """Serve the configured suffix-list and failure endpoints."""

    def do_GET(self) -> None:
        """Answer one request from the test server's route table."""
        server = cast(_SuffixServer, self.server)
        path = self.path.partition("?")[0]
        status, body = server.respond(path)
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep the test output free of per-request HTTP logs."""


@contextmanager
def _running_server(
    routes: Mapping[str, tuple[int, str]], gate: _OverlapGate | None = None
) -> Iterator[_SuffixServer]:
    try:
        server = _SuffixServer(routes, gate)
    except PermissionError as exc:
        pytest.skip(f"loopback HTTP server unavailable in this environment: {exc}")
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass(frozen=True)
class ProcessResult:
    """Captured result of one real subprocess."""

    returncode: int
    stdout: str
    stderr: str


def _clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "TLDEXTRACT_CACHE",
        "TLDEXTRACT_CACHE_TIMEOUT",
        "TLDEXTRACT_DEFAULT_FETCH_TIMEOUT",
        "TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS",
    ):
        env.pop(name, None)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if not existing_pythonpath
        else str(REPO_ROOT) + os.pathsep + existing_pythonpath
    )
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def _run_processes(
    commands: Sequence[tuple[str, Sequence[str]]],
    env: Mapping[str, str],
    timeout: float = 60,
) -> dict[str, ProcessResult]:
    """Start every command before waiting, then collect all results."""
    processes: list[tuple[str, subprocess.Popen[str]]] = []
    for label, command in commands:
        processes.append(
            (
                label,
                subprocess.Popen(
                    list(command),
                    cwd=REPO_ROOT,
                    env=dict(env),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
        )

    results: dict[str, ProcessResult] = {}
    for label, process in processes:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"{label} timed out: stdout={stdout!r} stderr={stderr!r}"
            ) from None
        results[label] = ProcessResult(process.returncode, stdout, stderr)
    return results


def _cli_command(
    cache_dir: Path,
    urls: Sequence[str],
    hosts: Sequence[str],
    include_psl_private_domains: bool,
    fallback_to_snapshot: bool = True,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "tldextract",
        "--json",
        "--cache_dir",
        str(cache_dir),
    ]
    if include_psl_private_domains:
        command.append("--include_psl_private_domains")
    if not fallback_to_snapshot:
        command.append("--no_fallback_to_snapshot")
    for url in urls:
        command.extend(("--suffix_list_url", url))
    command.extend(hosts)
    return command


PYTHON_EXTRACT_SCRIPT = r"""
import dataclasses
import json
import sys

import tldextract

cache_dir = sys.argv[1]
urls = tuple(json.loads(sys.argv[2]))
hosts = tuple(sys.argv[3:])
properties = (
    "fqdn",
    "ipv4",
    "ipv6",
    "registered_domain",
    "reverse_domain_name",
    "top_domain_under_public_suffix",
    "top_domain_under_registry_suffix",
)
extractor = tldextract.TLDExtract(cache_dir=cache_dir, suffix_list_urls=urls)
records = []
for host in hosts:
    for include_private in (False, True):
        ext = extractor(host, include_psl_private_domains=include_private)
        record = {
            **dataclasses.asdict(ext),
            **{prop: getattr(ext, prop) for prop in properties},
            "host": host,
            "include_psl_private_domains": include_private,
        }
        records.append(record)
print(json.dumps(records, sort_keys=True))
"""

PYTHON_NO_FALLBACK_SCRIPT = r"""
import json
import sys

import tldextract
from tldextract.suffix_list import SuffixListNotFound

cache_dir = sys.argv[1]
urls = tuple(json.loads(sys.argv[2]))
hosts = tuple(sys.argv[3:])
try:
    extractor = tldextract.TLDExtract(
        cache_dir=cache_dir,
        suffix_list_urls=urls,
        fallback_to_snapshot=False,
    )
    extractor(hosts[0])
except SuffixListNotFound:
    print(json.dumps({"error": "SuffixListNotFound"}))
    sys.exit(23)
print(json.dumps({"error": "unexpected success"}))
sys.exit(0)
"""


def _python_extract_command(
    cache_dir: Path, urls: Sequence[str], hosts: Sequence[str]
) -> list[str]:
    return [
        sys.executable,
        "-c",
        PYTHON_EXTRACT_SCRIPT,
        str(cache_dir),
        json.dumps(list(urls)),
        *hosts,
    ]


def _python_no_fallback_command(
    cache_dir: Path, urls: Sequence[str], hosts: Sequence[str]
) -> list[str]:
    return [
        sys.executable,
        "-c",
        PYTHON_NO_FALLBACK_SCRIPT,
        str(cache_dir),
        json.dumps(list(urls)),
        *hosts,
    ]


def _assert_cli_output(
    stdout: str, source: SourceRules, include_psl_private_domains: bool
) -> None:
    lines = stdout.splitlines()
    assert len(lines) == len(source.hosts), stdout
    for host, line in zip(source.hosts, lines, strict=True):
        actual = json.loads(line)
        expected = expected_fields(host, source, include_psl_private_domains)
        assert actual == expected, f"host={host} source={source.name}"


def _assert_python_output(
    stdout: str, source: SourceRules, hosts: Sequence[str]
) -> None:
    records = json.loads(stdout)
    assert len(records) == len(hosts) * 2, stdout
    expected_pairs = [
        (host, include_private) for host in hosts for include_private in (False, True)
    ]
    actual_pairs = [
        (record.pop("host"), record.pop("include_psl_private_domains"))
        for record in records
    ]
    assert actual_pairs == expected_pairs
    for record, (host, include_private) in zip(records, expected_pairs, strict=True):
        expected = expected_fields(host, source, include_private)
        assert record == expected, f"host={host} source={source.name}"


def _cache_json_files(cache_dir: Path) -> list[Path]:
    """Return the cache's JSON entries, excluding lock files."""
    return sorted(cache_dir.rglob(f"*{CACHE_FILE_SUFFIX}"))


def _assert_cache_entries(
    cache_dir: Path, ordered_urls: tuple[str, ...], source: SourceRules
) -> None:
    """Assert the cache holds the expected list for this source and its first URL."""
    cache = DiskCache(str(cache_dir))
    key = {"urls": ordered_urls, "fallback_to_snapshot": True}
    assert cache.get("publicsuffix.org-tlds", key) == [
        list(source.public),
        list(source.private),
    ]
    assert cache.get("urls", {"url": ordered_urls[0]}) == source.content


def test_two_ordered_source_groups_share_only_legitimate_cache_entries(
    tmp_path: Path,
) -> None:
    """Two configs with reversed source order share a cold cache without mixing data."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    assert not list(cache_dir.iterdir())

    routes = {
        "/a.dat": (200, SOURCE_A.content),
        "/b.dat": (200, SOURCE_B.content),
    }
    gate = _OverlapGate(frozenset(routes), timeout=15)

    with _running_server(routes, gate) as server:
        url_a = f"{server.base_url}/a.dat"
        url_b = f"{server.base_url}/b.dat"
        env = _clean_environment()
        commands = [
            (
                "group1_cli_public",
                _cli_command(cache_dir, (url_a, url_b), SOURCE_A.hosts, False),
            ),
            (
                "group1_cli_private",
                _cli_command(cache_dir, (url_a, url_b), SOURCE_A.hosts, True),
            ),
            (
                "group1_python",
                _python_extract_command(cache_dir, (url_a, url_b), SOURCE_A.hosts),
            ),
            (
                "group2_cli_public",
                _cli_command(cache_dir, (url_b, url_a), SOURCE_B.hosts, False),
            ),
            (
                "group2_cli_private",
                _cli_command(cache_dir, (url_b, url_a), SOURCE_B.hosts, True),
            ),
            (
                "group2_python",
                _python_extract_command(cache_dir, (url_b, url_a), SOURCE_B.hosts),
            ),
        ]
        results = _run_processes(commands, env)
        counts = server.counts

    assert not gate.timed_out
    assert counts == {"/a.dat": 1, "/b.dat": 1}

    for label in ("group1_cli_public", "group1_cli_private", "group1_python"):
        assert results[label].returncode == 0, results[label].stderr
    for label in ("group2_cli_public", "group2_cli_private", "group2_python"):
        assert results[label].returncode == 0, results[label].stderr

    _assert_cli_output(results["group1_cli_public"].stdout, SOURCE_A, False)
    _assert_cli_output(results["group1_cli_private"].stdout, SOURCE_A, True)
    _assert_python_output(results["group1_python"].stdout, SOURCE_A, SOURCE_A.hosts)

    _assert_cli_output(results["group2_cli_public"].stdout, SOURCE_B, False)
    _assert_cli_output(results["group2_cli_private"].stdout, SOURCE_B, True)
    _assert_python_output(results["group2_python"].stdout, SOURCE_B, SOURCE_B.hosts)

    _assert_cache_entries(cache_dir, (url_a, url_b), SOURCE_A)
    _assert_cache_entries(cache_dir, (url_b, url_a), SOURCE_B)
    assert len(_cache_json_files(cache_dir)) == 4


def test_warm_snapshot_fallback_does_not_satisfy_no_fallback_processes(
    tmp_path: Path,
) -> None:
    """A warm fallback entry is reused legitimately but never as a no-fallback success."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    assert not list(cache_dir.iterdir())

    routes = {"/fail.dat": (503, "upstream unavailable")}
    host = SNAPSHOT_BLOGSPOT.hosts[0]

    with _running_server(routes) as server:
        fail_url = f"{server.base_url}/fail.dat"
        env = _clean_environment()

        warm = _run_processes(
            [
                (
                    "warm_fallback_cli",
                    _cli_command(
                        cache_dir,
                        (fail_url,),
                        (host,),
                        include_psl_private_domains=False,
                        fallback_to_snapshot=True,
                    ),
                )
            ],
            env,
        )
        assert warm["warm_fallback_cli"].returncode == 0
        _assert_cli_output(warm["warm_fallback_cli"].stdout, SNAPSHOT_BLOGSPOT, False)
        assert server.counts == {"/fail.dat": 1}

        warm_reuse = _run_processes(
            [
                (
                    "warm_fallback_python",
                    _python_extract_command(cache_dir, (fail_url,), (host,)),
                )
            ],
            env,
        )
        assert warm_reuse["warm_fallback_python"].returncode == 0
        _assert_python_output(
            warm_reuse["warm_fallback_python"].stdout,
            SNAPSHOT_BLOGSPOT,
            (host,),
        )
        assert server.counts == {"/fail.dat": 1}

        no_fallback_cli = _run_processes(
            [
                (
                    "no_fallback_cli",
                    _cli_command(
                        cache_dir,
                        (fail_url,),
                        (host,),
                        include_psl_private_domains=False,
                        fallback_to_snapshot=False,
                    ),
                )
            ],
            env,
        )
        assert no_fallback_cli["no_fallback_cli"].returncode != 0
        assert not no_fallback_cli["no_fallback_cli"].stdout.strip()
        assert "SuffixListNotFound" in no_fallback_cli["no_fallback_cli"].stderr
        assert server.counts == {"/fail.dat": 2}

        no_fallback_python = _run_processes(
            [
                (
                    "no_fallback_python",
                    _python_no_fallback_command(cache_dir, (fail_url,), (host,)),
                )
            ],
            env,
        )
        assert (
            no_fallback_python["no_fallback_python"].returncode
            == PYTHON_EXIT_SUFFIX_NOT_FOUND
        )
        assert json.loads(no_fallback_python["no_fallback_python"].stdout) == {
            "error": "SuffixListNotFound"
        }
        assert server.counts == {"/fail.dat": 3}

        cache = DiskCache(str(cache_dir))
        cached_public, cached_private = cast(
            tuple[list[str], list[str]],
            cache.get(
                "publicsuffix.org-tlds",
                {"urls": (fail_url,), "fallback_to_snapshot": True},
            ),
        )
        assert "com" in cached_public
        assert "blogspot.com" in cached_private
        assert "alpha" not in cached_public
        assert "tenant.alpha" not in cached_private
        with pytest.raises(KeyError):
            cache.get(
                "publicsuffix.org-tlds",
                {"urls": (fail_url,), "fallback_to_snapshot": False},
            )
