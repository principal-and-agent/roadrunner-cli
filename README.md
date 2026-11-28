<p align="center">
  <img src="docs/assets/banner.png" alt="Roadrunner CLI illustrated banner" width="800" />
</p>

# Roadrunner CLI

**Run a reviewed backlog with Claude Code and get a local Git branch you can review.**

Roadrunner takes tasks from a YAML or Markdown roadmap, or from GitHub issues. It runs one task at a time in an isolated Git worktree, checks the change against operator-approved acceptance commands, and integrates passing work onto a dedicated local branch. The resulting report shows which tasks resolved and what needs operator input.

[![CI](https://github.com/principal-and-agent/roadrunner-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/principal-and-agent/roadrunner-cli/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)](pyproject.toml)
[![MIT license](https://img.shields.io/badge/License-MIT-green)](LICENSE)

## Start here

The source tree is a release candidate. Install it in a virtual environment from this repository. Live Claude runs require macOS with `sandbox-exec` and an authenticated Claude Code CLI. Python 3.10+ and Git are required.

```sh
git clone https://github.com/principal-and-agent/roadrunner-cli.git
cd roadrunner-cli
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
roadrunner --help
```

In a project with an initial Git commit, run the following after preparing your roadmap and copying and reviewing the [example run policy](docs/shipping-run-policy.json). Point the policy at an acceptance command that exists in your project.

```sh
roadrunner init .
roadrunner sync --roadmap tasks/tasks.yaml
roadrunner doctor --policy run-policy.json
roadrunner run --plan /absolute/path/from-sync.yaml --policy run-policy.json
```

Use the `plan` path returned by `sync`. The [operator guide](docs/WORKFLOW.md) takes you from an empty Git project to a reviewable run branch, and covers GitHub issue intake, recovery, and delivery.

## What Roadrunner gives you

| Stage | Result |
| --- | --- |
| Select | A frozen plan with stable task IDs, dependencies, acceptance criteria, and file scope. |
| Execute | A separate worktree per task with attempt, time, and budget limits. |
| Verify | Controller-run, network-disabled validation and evidence tied to committed changes. |
| Review | A local `roadrunner/run-<UUID>` branch and a JSON report. Your checkout and uncommitted edits stay in place. |

The installed release candidate has been exercised against failure-injection fixtures and a bounded native Claude canary. GitHub intake is read-only. Remote delivery is a separate command with explicit permissions, and live GitHub writes have yet to be qualified. See the [qualification evidence](docs/shipping-implementation-checklist.md).

## Documentation

| I want to | Start here |
| --- | --- |
| Run a backlog, inspect results, or recover a run | [Operator guide](docs/WORKFLOW.md) |
| Write a roadmap or review a run policy | [Configuration](docs/configuration.md) |
| Understand evidence, state, and isolation | [Architecture](docs/architecture.md) |
| Use the project-level Claude hook loop | [Hook workflow](docs/WORKFLOW.md#claude-hook-workflow) |
| Find troubleshooting recipes | [Operator notes](docs/operator-notes.md) |
| Contribute or report a vulnerability | [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) |

See [Changelog](CHANGELOG.md) for project changes and [Release process](docs/release.md) for maintainer instructions.
