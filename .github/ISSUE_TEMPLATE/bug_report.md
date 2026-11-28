---
name: Bug report
about: Report a defect in roadrunner-cli
title: "[bug] "
labels: bug
---

**What happened**

A clear description of the bug.

**What you expected**

What should have happened instead.

**Reproduction**

List the command, workflow (supervised run or interactive hooks), and minimal steps to reproduce. Include the relevant plan, policy, or roadmap fields when scheduling or validation is involved.

**Environment**

- Python version (`python3 --version`):
- Roadrunner version (`pip show roadrunner-cli`, or `git rev-parse HEAD` if from source):
- OS and version:

**Logs**

For a supervised run, include the run ID, task phase, reason, and relevant attempt or validation output. For the hook loop, include the relevant `logs/trace.jsonl` lines. Redact sensitive data. Report vulnerabilities privately using [the security policy](https://github.com/principal-and-agent/roadrunner-cli/blob/main/SECURITY.md).
