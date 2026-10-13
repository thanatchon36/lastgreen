"""Command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Protocol

from lastgreen import __version__, github
from lastgreen.analyze import compare_logs
from lastgreen.github import GitHubError, Json
from lastgreen.report import Report, RunInfo, render_json, render_markdown, render_text

USAGE = """\
lastgreen [RUN] [options]          compare a failed GitHub Actions job with its last green run
lastgreen diff GREEN.log RED.log   compare two log files from any CI

RUN is a run or job URL, or a run id with --repo. Without RUN, lastgreen uses the latest
failed run on the current branch of the repository in the current directory."""

NO_TOKEN = (
    "lastgreen needs a GitHub token to read Actions logs; GitHub requires one even for public "
    "repositories. Run `gh auth login`, or set GH_TOKEN."
)


class _SupportsIsatty(Protocol):
    def isatty(self) -> bool: ...


def _should_use_color(stream: _SupportsIsatty | None = None) -> bool:
    """Return whether coloured terminal output is enabled.

    Pass *stream* in tests to avoid patching ``sys.stdout`` directly.
    When *stream* is ``None`` (the default), ``sys.stdout`` is used.
    """
    target: _SupportsIsatty = stream if stream is not None else sys.stdout
    return target.isatty() and not os.environ.get("NO_COLOR", "")


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format", choices=("text", "markdown", "json"), default="text", help="output format"
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=30,
        metavar="N",
        help="new log lines to show (default 30; json always includes all)",
    )


def _run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lastgreen", usage=USAGE)
    parser.add_argument("run", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--repo", help="OWNER/REPO, when RUN is a bare id or there is no RUN")
    parser.add_argument("--job", help="name of the job to compare (default: the first failed job)")
    _add_output_options(parser)
    parser.add_argument("--version", action="version", version=f"lastgreen {__version__}")
    return parser


def _diff_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lastgreen diff", usage="lastgreen diff GREEN.log RED.log"
    )
    parser.add_argument("green", type=Path, help="log of the passing run")
    parser.add_argument("red", type=Path, help="log of the failing run")
    _add_output_options(parser)
    return parser


def _render(report: Report, fmt: str, max_lines: int, color: bool = False) -> str:
    if fmt == "json":
        return render_json(report)
    if fmt == "markdown":
        return render_markdown(report, max_lines)
    return render_text(report, max_lines, color=color)


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError as error:
        raise GitHubError(f"cannot read {path}: {error.strerror}") from None


def _run_info(run: Json, job: Json) -> RunInfo:
    return RunInfo(
        run_id=int(run["id"]),
        attempt=int(job.get("run_attempt") or run.get("run_attempt") or 1),
        job=job["name"],
        sha=job.get("head_sha") or run["head_sha"],
        branch=run.get("head_branch") or "",
        created=job.get("started_at") or run["created_at"],
        url=job.get("html_url") or run["html_url"],
    )


def compare_run(args: argparse.Namespace, api: github.Api | None = None) -> Report:
    ref = github.parse_run_ref(args.run) if args.run else None
    repo = (ref.repo if ref else None) or (github.check_repo(args.repo) if args.repo else None)
    from_git = False
    if not repo:
        repo = github.repo_from_git()
        from_git = True
    if not repo:
        raise GitHubError("cannot tell which repository: pass a run URL or --repo OWNER/REPO.")

    if api is None:
        token = github.find_token()
        if not token:
            raise GitHubError(NO_TOKEN)
        api = github.Client(token)

    if ref is None:
        branch = github.current_branch() if from_git else None
        ref = github.RunRef(repo, github.latest_failed_run(api, repo, branch))
    red_run = api.get_json(f"/repos/{repo}/actions/runs/{ref.run_id}")
    attempt = ref.attempt or int(red_run.get("run_attempt") or 1)
    red_jobs = github.jobs(api, repo, ref.run_id, attempt)
    failed = github.failed_jobs(red_jobs)
    if not red_jobs:
        raise GitHubError(
            f"run {ref.run_id} attempt {attempt} has no jobs: it failed before any job started, "
            "usually because of an invalid workflow file. Open the run page for the error."
        )

    if ref.job_id:
        red_job = next((j for j in red_jobs if j["id"] == ref.job_id), None)
        if red_job is None:
            raise GitHubError(
                f"job {ref.job_id} is not part of run {ref.run_id} attempt {attempt}."
            )
    elif args.job:
        red_job = next((j for j in red_jobs if j["name"] == args.job), None)
        if red_job is None:
            names = ", ".join(sorted(j["name"] for j in red_jobs))
            raise GitHubError(f"no job named {args.job!r} in run {ref.run_id}. Jobs: {names}")
    elif failed:
        red_job = failed[0]
    else:
        raise GitHubError(
            f"run {ref.run_id} attempt {attempt} has no failed jobs "
            f"(conclusion: {red_run.get('conclusion') or red_run.get('status')}). "
            "Pass a failed run, or --job NAME to compare a specific job."
        )

    found = github.find_green(api, repo, red_run, red_job["name"])
    if found is None:
        raise GitHubError(
            f"no earlier successful run of job {red_job['name']!r} in the recent history of "
            f"this workflow on {red_run.get('head_branch') or 'its branch'} or the default branch."
        )
    green_run, green_job, note = found

    red_steps = [(s["name"], s.get("conclusion")) for s in red_job.get("steps", [])]
    failed_step = next(
        (s["name"] for s in red_job.get("steps", []) if s.get("conclusion") in github.FAILED), None
    )
    report = compare_logs(
        github.job_log(api, repo, green_job["id"]),
        github.job_log(api, repo, red_job["id"]),
        red_steps,
        failed_step,
    )
    # The repository's own code checked out as an action is a code change, not drift.
    report.changes = [
        c for c in report.changes if not (c.kind == "action" and c.name.lower() == repo.lower())
    ]
    report.red = _run_info(red_run, red_job)
    report.green = _run_info(green_run, green_job)
    if note:
        report.notes.append(note)
    others = [j["name"] for j in failed if j["id"] != red_job["id"]]
    if others:
        report.notes.append(f"also failed: {', '.join(others)} (compare one with --job NAME)")
    try:
        report.code = github.compare(
            api, repo, report.green.sha, report.red.sha, red_run.get("path")
        )
    except GitHubError as error:
        report.notes.append(f"could not compare commits: {error}")
    return report


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    diff_mode = bool(argv) and argv[0] == "diff"
    parser = _diff_parser() if diff_mode else _run_parser()
    args = parser.parse_args(argv[1:] if diff_mode else argv)
    if args.max_lines < 1:
        parser.error("--max-lines must be at least 1")
    try:
        if diff_mode:
            report = compare_logs(_read(args.green), _read(args.red))
        else:
            report = compare_run(args)
    except GitHubError as error:
        print(f"lastgreen: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    sys.stdout.write(_render(report, args.format, args.max_lines, _should_use_color()))
    return 0
