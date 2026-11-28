# Contributing to Roadrunner

Use the [operator guide](docs/WORKFLOW.md) to understand the supervised and interactive workflows. For contributor changes, work on a branch from current `main` and open a focused pull request. Report bugs and propose features in [GitHub Issues](https://github.com/principal-and-agent/roadrunner-cli/issues). Report vulnerabilities through the [security policy](SECURITY.md).

## Develop locally

Roadrunner requires Python 3.10+ and Git. macOS and Linux can run the development suite. Live Claude execution requires the macOS sandbox. Install [Just](https://github.com/casey/just) and [ShellCheck](https://www.shellcheck.net/) to run the checks below.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
just ci
shellcheck hooks/*.sh
```

`just ci` runs pytest, Ruff, and mypy. GitHub Actions runs the test matrix on Python 3.10, 3.11, and 3.12, plus separate Ruff, mypy, and ShellCheck jobs. macOS sandbox tests need an environment that can start `sandbox-exec`.

## Installed package check

Use a separate environment to check the wheel from outside the source tree. The fixture qualification runs against a disposable project using a test worker, so it needs no Claude credentials.

```sh
python -m build --wheel
CHECK_DIR=$(mktemp -d)
python -m venv "$CHECK_DIR/venv"
"$CHECK_DIR/venv/bin/python" -m pip install dist/roadrunner_cli-*.whl
python tests/qualify_installed.py "$CHECK_DIR/venv/bin/roadrunner" "$CHECK_DIR/qualification"
```

If `dist/` contains multiple wheels, pass the exact wheel you just built. The qualification verifies dependency order, retry, blocked work, independent progress, resume, and preservation of the operator checkout.

## Where changes belong

| Area | Files and tests |
| --- | --- |
| Backlog and supervisor | `src/roadrunner/intake.py`, `supervisor.py`, `delivery.py`; `tests/test_intake_delivery.py`, `tests/test_supervisor.py`, and installed qualification |
| Interactive loop | `src/roadrunner/cli.py`, `state.py`, `session.py`; controller, session, shipping guard, and smoke tests |
| Claude hooks and scaffolding | `hooks/`, `.claude/settings.json`, and their copies under `src/roadrunner/scaffold/`; run `tests/test_hooks.py` and `tests/test_shipping_guards.py` |
| Operator workflow and policy | [Workflow](docs/WORKFLOW.md), [Configuration](docs/configuration.md), and [Architecture](docs/architecture.md) |
| Publishing | [Release process](docs/release.md) and [release workflow](.github/workflows/publish.yml) |

Keep project hook scripts and their scaffold copies in sync. Test the actual hook entry point when changing a hook payload or decision. Preserve the distinction between Stop decisions, the roadmap file guard, and informational hooks. See the [hook contracts](docs/architecture.md#2-hook-contracts).

## Submit a pull request

Use conventional commit subjects such as `fix: preserve run evidence` or `docs: explain the launch policy`. Include the behavior change, commands run, and any host or fixture limitations. For a bug fix, keep a regression test that fails before the change and passes afterward. Leave run output and state files out of the diff. See [REVIEW.md](REVIEW.md) for review criteria.
