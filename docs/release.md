# Release process

The repository currently ships a release candidate from source. The [publish workflow](../.github/workflows/publish.yml) can publish an sdist and wheel to PyPI from a version tag once Trusted Publishing is configured. A GitHub Release is a separate step.

## Configure Trusted Publishing

On PyPI, create a pending Trusted Publisher for the first upload, or add a publisher to the existing project. Use these values:

| Field | Value |
| --- | --- |
| Project | `roadrunner-cli` |
| GitHub owner | `principal-and-agent` |
| Repository | `roadrunner-cli` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

Create the matching `pypi` environment in GitHub and apply the repository's release approval settings. The workflow requests a short-lived OIDC token for upload. See the [PyPI Trusted Publishing guide](https://docs.pypi.org/trusted-publishers/).

## Rehearse

Run `just ci` and the [installed package check](../CONTRIBUTING.md#installed-package-check). Check the [qualification evidence](shipping-implementation-checklist.md) for feature limits before making any release claims.

The workflow called `publish` also supports manual dispatch. A manual dispatch runs the test matrix and uploads build artifacts without publishing. Download the artifact, install the wheel into a fresh virtual environment, and run `roadrunner --help`, `roadrunner init`, and the qualification against a disposable Git project.

## Publish

1. Review the release candidate on `main` and confirm its [CI](https://github.com/principal-and-agent/roadrunner-cli/actions/workflows/ci.yml) is green.
2. Update the package version in [pyproject.toml](../pyproject.toml) and record the changes in [CHANGELOG.md](../CHANGELOG.md). Merge the release preparation PR.
3. Create an annotated `vX.Y.Z` tag on the reviewed release commit and push just that tag. Use a matching version suffix for a prerelease. The tag push triggers publishing after tests and build.
4. Verify that the expected version and files appear on PyPI. Install that exact version in a fresh environment and repeat the installed check.
5. If publishing a GitHub Release, use the same tag and release notes. Mark release candidates as prereleases.

## Recover from a bad release

Yank the affected version on PyPI, document the impact in the changelog, and publish a corrected version with a new version number. Keep release notes and installation guidance aligned with the available artifacts. See the [PyPI yanking guide](https://docs.pypi.org/project-management/yanking/).
