"""Find what changed in the build environment between two job logs.

Three sources, in order of confidence:

1. GitHub's job preamble: runner image, operating system, runner version, and the exact
   commit each action resolved to.
2. Package lists printed by installers (pip, uv, tox, poetry, cargo, go, bundler, pnpm,
   Maven, and Gradle).
3. Any other line that appears exactly once in both logs and differs only in version
   numbers, such as `git version 2.55.0` or `Successfully set up CPython (3.14.7)`.
   This catches tools nobody wrote a rule for.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from lastgreen.joblog import JobLog
from lastgreen.normalize import normalize

_GROUP = re.compile(r"^##\[group\](.*)$")
_RUNNER_VERSION = re.compile(r"^Current runner version: '(.+)'$")
_RUNNER_NAME = re.compile(r"^Runner name: '(.+)'$")
_ACTION = re.compile(r"^Download action repository '([^@']+)@([^']+)' \(SHA:([0-9a-f]+)\)")

_PIP_INSTALLED = re.compile(r"^Successfully installed (.+)$")
_PINNED = re.compile(r"(?<![\w.-])([A-Za-z0-9][\w.-]*)==([0-9][^\s,;'\"\])]*)")
_PACKAGE_LINES = [
    re.compile(r"^\s*(?:Compiling|Downloaded|Checking) ([\w-]+) v(\d\S*)"),  # cargo
    re.compile(r"^go: downloading (\S+) (v\d\S*)$"),  # go
    re.compile(  # bundler
        r"^(?:Installing|Using|Fetching) ([\w.-]+) (\d[\w.]*)"
        r"(?: \(was [^)]*\))?(?: with native extensions)?$"
    ),
    re.compile(  # poetry
        r"^\s*- (?:Installing|Updating|Downgrading) ([\w.-]+) \((?:\S+ -> )?(\d[^)\s]*)\)"
    ),
    re.compile(r"^\+ (@?[\w./-]+) (\d\S*)$"),  # pnpm
    re.compile(  # pip downloading a wheel: name-version(-build)-python-abi-platform.whl
        r"^\s*(?:Downloading|Using cached) (?:\S*/)?([A-Za-z0-9_.]+)-(\d[^-\s]*)"
        r"(?:-\d[^-\s]*)?-[^-\s]+-[^-\s]+-[^-\s]+\.whl\b"
    ),
    re.compile(  # pip downloading an sdist
        r"^\s*(?:Downloading|Using cached) (?:\S*/)?([A-Za-z0-9_.-]+?)-(\d[\w.]*)"
        r"\.(?:tar\.gz|zip)\b"
    ),
]

# Maven artifact downloads.
# Supports standard Maven Central URLs and optional Maven [INFO] prefixes.
_MAVEN_DOWNLOAD = re.compile(
    r"^(?:\[INFO\]\s+)?"
    r"(?P<status>Downloading|Downloaded) from \S+: "
    r"https?://[^/\s]+/"
    r"(?:[^/\s]+/)*?maven2/"
    r"(?P<group_path>(?:[^/\s]+/)+?)"
    r"(?P<artifact>[^/\s]+)/"
    r"(?P<version>[^/\s]+)/"
    r"(?P<filename>[^/\s]+\.(?:jar|pom))(?:\s|$)"
)

# Match Maven download lines even when their URL cannot be parsed as an artifact.
# This prevents incomplete downloads from being treated as generic tool changes.
_MAVEN_DOWNLOAD_LINE = re.compile(
    r"^(?:\[INFO\]\s+)?(?:Downloading|Downloaded) from \S+: https?://"
)

# Gradle dependency reports commonly contain lines such as:
# |    +--- org.slf4j:slf4j-api:2.0.12 -> 2.0.13
# \--- com.google.guava:guava:33.0.0-jre
_GRADLE_DEPENDENCY = re.compile(
    r"^\s*(?:[+\\| ]*---)\s+"
    r"(?P<group>[A-Za-z0-9_.-]+):"
    r"(?P<artifact>[A-Za-z0-9_.-]+):"
    r"(?P<requested>[^\s()]+)"
    r"(?:\s+->\s+(?P<resolved>[^\s()]+))?"
    r"(?:\s|$)"
)

_VERSION = re.compile(
    r"(?<![\w.])(v?\d+(?:\.\d+)+(?:(?:a|b|rc|\.dev|\.post)\d+)*"
    r"(?:-(?:alpha|beta|rc|pre|dev|preview|canary|nightly)[0-9A-Za-z.]*)?"
    r"(?:\+[0-9A-Za-z.]+)?)(?![\w.])"
)
_UNIT = re.compile(r"\s*(?:%|[KMGT]i?B\b|B/s|MBs|ms\b|s\b|secs?\b|seconds\b|minutes\b|x\b)")
_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# Versions computed from the checked-out commit (setuptools-scm, git describe) change with
# every commit: they describe the code under test, not its environment.
_FROM_COMMIT = re.compile(r"[+.]g[0-9a-f]{7,40}\b")


@dataclass(frozen=True)
class Change:
    kind: str  # "runner", "action", "tool", "package"
    name: str
    old: str | None  # None: only in the failing run
    new: str | None  # None: only in the passing run
    also: int = 0  # other log lines that show the same version change


@dataclass
class Facts:
    runner: dict[str, str] = field(default_factory=dict)
    actions: dict[str, set[str]] = field(default_factory=dict)
    packages: dict[str, set[str]] = field(default_factory=dict)
    package_names: dict[str, str] = field(default_factory=dict)
    # template -> (display label, versions) for lines that occur once
    lines: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)


def _package_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _add_package(facts: Facts, name: str, version: str) -> None:
    key = _package_key(name)
    facts.package_names.setdefault(key, name)
    facts.packages.setdefault(key, set()).add(version)


def _packages_in(line: str) -> list[tuple[str, str]]:
    stripped = line.strip()
    match = _PIP_INSTALLED.match(stripped)
    if match:
        found = []
        for token in match.group(1).split():
            name, sep, version = token.rpartition("-")
            if sep and name and version[:1].isdigit():
                found.append((name, version))
        return found

    # uv prints "- pkg==old" before "+ pkg==new"; the removed version is not installed.
    if not stripped.startswith("-"):
        pinned = _PINNED.findall(line)
        if pinned:
            return pinned

    # Ignore incomplete or unrecognized Maven downloads as package changes.
    match = _MAVEN_DOWNLOAD.match(stripped)
    if match:
        if match.group("status") != "Downloaded":
            return []

        group_id = match.group("group_path").rstrip("/").replace("/", ".")
        artifact = match.group("artifact")
        version = match.group("version")
        filename = match.group("filename")

        # Verify that the filename matches the artifact coordinates.
        if filename not in (f"{artifact}-{version}.jar", f"{artifact}-{version}.pom"):
            return []

        return [(f"{group_id}:{artifact}", version)]

    # Gradle dependency reports may show a requested version and a resolved version.
    match = _GRADLE_DEPENDENCY.match(stripped)
    if match:
        group = match.group("group")
        artifact = match.group("artifact")
        version = match.group("resolved") or match.group("requested")

        # Ignore dynamic selectors and unresolved constraints.
        if version and re.match(r"^\d[\w.+-]*$", version):
            return [(f"{group}:{artifact}", version)]

    for pattern in _PACKAGE_LINES:
        match = pattern.match(line)
        if match:
            return [(match.group(1), match.group(2))]
    return []


def _versions_in(line: str) -> list[tuple[int, int]]:
    """Spans of tokens that look like software versions rather than sizes, ratios, or IPs."""
    mentions_version = "version" in line.lower()
    spans = []
    for match in _VERSION.finditer(line):
        token = match.group(1)
        if _IP.match(token) or _UNIT.match(line, match.end()):
            continue
        if token.count(".") >= 2 or token.startswith("v") or mentions_version:
            spans.append(match.span(1))
    return spans


def extract(log: JobLog) -> Facts:
    facts = Facts()
    group = ""
    group_lines: dict[str, list[str]] = {}
    candidates: list[tuple[str, str, tuple[str, ...]]] = []
    for _, text in log.lines:
        group_match = _GROUP.match(text)
        if group_match:
            group = group_match.group(1).strip()
            continue
        if text == "##[endgroup]":
            group = ""
            continue
        if group in ("Runner Image", "Operating System", "Runner Image Provisioner"):
            group_lines.setdefault(group, []).append(text.strip())
            continue
        if match := _RUNNER_VERSION.match(text):
            facts.runner["runner version"] = match.group(1)
            continue
        if match := _RUNNER_NAME.match(text):
            facts.runner["runner name"] = match.group(1)
            continue
        if match := _ACTION.match(text):
            action, ref, sha = match.groups()
            pinned = ref == sha or ref.startswith(sha) or sha.startswith(ref)
            facts.actions.setdefault(action, set()).add(sha[:7] if pinned else f"{ref} {sha[:7]}")
            continue
        packages = _packages_in(text)
        if packages:
            for name, version in packages:
                _add_package(facts, name, version)
            continue
        # Maven download-start lines must not fall through to generic version detection.
        if _MAVEN_DOWNLOAD_LINE.match(text.strip()):
            continue
        spans = _versions_in(text)
        if spans:
            versions = tuple(text[a:b] for a, b in spans)
            template = text
            label = text
            for a, b in reversed(spans):
                template = template[:a] + "<v>" + template[b:]
                label = label[:a] + "…" + label[b:]
            candidates.append((normalize(template), label.strip(), versions))

    image = group_lines.get("Runner Image", [])
    image_name = next((x.split(":", 1)[1].strip() for x in image if x.startswith("Image:")), "")
    image_version = next(
        (x.split(":", 1)[1].strip() for x in image if x.startswith("Version:")), ""
    )
    if image_name or image_version:
        facts.runner["runner image"] = f"{image_name} {image_version}".strip()
    if group_lines.get("Operating System"):
        facts.runner["os"] = " ".join(group_lines["Operating System"])

    counts = Counter(template for template, _, _ in candidates)
    for template, label, versions in candidates:
        if counts[template] == 1:
            facts.lines[template] = (label, versions)
    return facts


def _join(values: set[str]) -> str:
    return ", ".join(sorted(values))


def _shorten(label: str, width: int = 60) -> str:
    return label if len(label) <= width else label[: width - 1] + "…"


def _distinct(versions: tuple[str, ...]) -> str:
    return ", ".join(dict.fromkeys(versions))


def compare(green: Facts, red: Facts) -> list[Change]:
    """Changes from the passing run (`green`) to the failing run (`red`)."""
    changes: list[Change] = []
    for key in ("runner image", "os", "runner version", "runner name"):
        old, new = green.runner.get(key), red.runner.get(key)
        if old != new and (old or new):
            changes.append(Change("runner", key, old, new))

    for action in sorted(green.actions.keys() | red.actions.keys()):
        old_set, new_set = green.actions.get(action), red.actions.get(action)
        if old_set != new_set:
            changes.append(
                Change(
                    "action",
                    action,
                    _join(old_set) if old_set else None,
                    _join(new_set) if new_set else None,
                )
            )

    grouped: dict[tuple[str, str], list[str]] = {}
    for template, (_, old_versions) in green.lines.items():
        if template not in red.lines:
            continue
        label, new_versions = red.lines[template]
        if old_versions != new_versions:
            if any(_FROM_COMMIT.search(v) for v in old_versions + new_versions):
                continue
            pair = (_distinct(old_versions), _distinct(new_versions))
            grouped.setdefault(pair, []).append(label)
    for (old, new), labels in grouped.items():
        label = min(labels, key=len)
        changes.append(Change("tool", _shorten(label), old, new, also=len(labels) - 1))

    # Upgrades and downgrades first, then packages that appeared or disappeared.
    in_both = green.packages.keys() & red.packages.keys()
    for key in sorted(
        green.packages.keys() | red.packages.keys(), key=lambda k: (k not in in_both, k)
    ):
        old_set, new_set = green.packages.get(key), red.packages.get(key)
        if old_set != new_set:
            name = red.package_names.get(key) or green.package_names[key]
            changes.append(
                Change(
                    "package",
                    name,
                    _join(old_set) if old_set else None,
                    _join(new_set) if new_set else None,
                )
            )
    return changes
