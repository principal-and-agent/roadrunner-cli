# Security policy

## Versions

Use the latest source from this repository for the current release candidate. There is no published package or stable release support commitment for this repository.

## Report a vulnerability

Contact the maintainer privately using the author email in [the package metadata](pyproject.toml). Include the affected revision, operating system, workflow, impact, and a minimal reproduction. Remove credentials, tokens, and project data from any logs. Use GitHub Issues for ordinary bugs.

## Trust boundaries

Roadrunner assumes the operator controls the Git project and reviews the commands it is allowed to run. The supervised workflow takes validation commands from a separately reviewed policy. Live Claude has limited file tools inside a macOS sandbox, and validation runs without network access. The operator should protect the gate scripts and any supporting fixtures. GitHub intake is read-only, and remote delivery has separate explicit capabilities.

The interactive hook workflow executes commands from the operator's roadmap under the local account. The project-level Claude guard blocks direct roadmap edits through Write and Edit. Review the roadmap before use and use the task CLI for status transitions. See [Architecture](docs/architecture.md) for details of both workflows.
