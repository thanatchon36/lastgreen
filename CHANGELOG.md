# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Optional ANSI terminal colours in text output: the version-change arrow and new version
  are coloured green; lines containing `Error:` are coloured red. Colours are enabled only
  when stdout is a TTY and the `NO_COLOR` environment variable is unset or empty, honouring
  the [NO_COLOR](https://no-color.org/) spec. Markdown and JSON output are never coloured.
- Detect Maven and Gradle dependency version changes in build logs.

## [0.1.0] - 2026-10-12

### Added

- `lastgreen [RUN]`: find the last green run of a failed GitHub Actions job and compare
  the two logs. Accepts a run URL, a job URL, a run id with `--repo`, or nothing (latest
  failed run on the current branch).
- Environment comparison: runner image, OS, runner version, action commits, package versions
  from pip, uv, tox, poetry, cargo, go, bundler, and pnpm output, and any other line that
  differs only in a version number.
- New lines in the failing step, compared against the whole passing log after removing
  timestamps, durations, hashes, and other volatile values.
- Commits between the two runs, with changed workflow files and lockfiles.
- `lastgreen diff GREEN.log RED.log` for saved logs from any CI.
- Text, Markdown, and JSON output.

[Unreleased]: https://github.com/thanatchon36/lastgreen/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/thanatchon36/lastgreen/releases/tag/v0.1.0
