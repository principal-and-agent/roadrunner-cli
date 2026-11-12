"""External Git projects and fake-agent subprocess contracts."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
import yaml

from roadrunner import supervisor as runner


SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    runner.git(root, "init", "-b", "main")
    runner.git(root, "config", "user.name", "Fixture")
    runner.git(root, "config", "user.email", "fixture@example.invalid")
    (root / "base.txt").write_text("operator content")
    (root / "verify.py").write_text("from pathlib import Path\nassert Path('one').read_text() == 'fixed'\n")
    runner.git(root, "add", ".")
    runner.git(root, "commit", "-m", "initial")
    # Operator changes must survive every supervisor run.
    (root / "base.txt").write_text("operator dirty content")
    agent = tmp_path / "agent.py"
    agent.write_text("""import json, os
from pathlib import Path
task = json.loads(os.environ['ROADRUNNER_TASK'])
for name in task['files_expected']:
    Path(name).write_text('fixed')
""")
    policy = dict(
        adapter="fixture",
        sandbox="fixture",
        fixture_command=[sys.executable, str(agent)],
        validation_commands=[f"{sys.executable} verify.py"],
        protected_paths=["verify.py"],
        max_attempts=2,
        max_budget_usd=10,
        run_seconds=20,
        attempt_seconds=3,
    )
    (tmp_path / "policy.json").write_text(json.dumps(policy))
    task = dict(
        id="TASK-001",
        title="One",
        goal="Create one",
        status="todo",
        acceptance_criteria=["one is fixed"],
        files_expected=["one"],
        depends_on=[],
    )
    (tmp_path / "plan.yaml").write_text(yaml.safe_dump({"tasks": [task]}))
    return root


def command(project, *extra):
    return [
        sys.executable,
        "-m",
        "roadrunner",
        "run",
        "--project",
        str(project),
        "--plan",
        str(project.parent / "plan.yaml"),
        "--policy",
        str(project.parent / "policy.json"),
        *extra,
    ]


def invoke(project, *extra):
    return subprocess.run(
        command(project, *extra), text=True, capture_output=True, env=dict(os.environ, PYTHONPATH=str(SOURCE / "src")), timeout=30
    )


def state(project):
    paths = list(runner.location(project).glob("*/run.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text()), paths[0]


def test_serial_dependency_integration_preserves_operator(project):
    path = project.parent / "plan.yaml"
    data = yaml.safe_load(path.read_text())
    data["tasks"].append({**data["tasks"][0], "id": "TASK-002", "depends_on": ["TASK-001"], "files_expected": ["two"]})
    path.write_text(yaml.safe_dump(data))
    result = invoke(project)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == "all_resolved"
    assert [i["phase"] for i in report["items"]] == ["resolved", "resolved"]
    assert runner.git(project, "show", report["branch"] + ":two") == "fixed"
    assert runner.git(project, "branch", "--show-current") == "main"
    assert (project / "base.txt").read_text() == "operator dirty content"
    assert not (project / "one").exists()
    saved, _ = state(project)
    assert saved["items"][1]["attempts"][0]["base"] == saved["items"][0]["evidence"]["candidate"]
    resumed = invoke(project, "--resume", saved["id"])
    assert resumed.returncode == 0
    assert state(project)[0]["reserved_usd"] == 2


def test_validation_retry_repairs(project):
    agent = project.parent / "agent.py"
    agent.write_text(
        "import os\nfrom pathlib import Path\nPath('one').write_text('bad' if os.environ['ROADRUNNER_ATTEMPT'] == '1' else 'fixed')\n"
    )
    result = invoke(project)
    assert result.returncode == 0, result.stdout + result.stderr
    saved, _ = state(project)
    assert len(saved["items"][0]["attempts"]) == 2
    assert saved["items"][0]["attempts"][0]["outcome"] == "failed"
    assert saved["reserved_usd"] == 2


def test_no_progress_blocks_and_independent_continues(project):
    path = project.parent / "plan.yaml"
    data = yaml.safe_load(path.read_text())
    data["tasks"][0]["acceptance_criteria"] = []
    data["tasks"].append({**data["tasks"][0], "id": "TASK-002", "acceptance_criteria": ["one fixed"]})
    path.write_text(yaml.safe_dump(data))
    result = invoke(project)
    assert result.returncode == 2
    assert [i["phase"] for i in json.loads(result.stdout)["items"]] == ["needs_input", "resolved"]


def test_tampered_gate_blocks(project):
    agent = project.parent / "agent.py"
    agent.write_text("from pathlib import Path\nPath('one').write_text('bad')\nPath('verify.py').write_text('pass')\n")
    result = invoke(project)
    assert result.returncode == 2
    saved, _ = state(project)
    assert saved["items"][0]["phase"] == "blocked"
    assert "Frozen gate" in saved["items"][0]["attempts"][0]["reason"]
    assert runner.git(project, "rev-parse", saved["branch"]) == saved["base"]


def test_invalid_transition_rejected():
    for phase in ("ready", "running", "validating", "blocked", "resolved"):
        with pytest.raises(ValueError):
            runner.transition({"phase": phase}, "resolved")


@pytest.mark.parametrize("protected_paths", ["gate.py", ["gate.py", 1], [""]])
def test_load_policy_rejects_invalid_protected_paths(tmp_path, protected_paths):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "validation_commands": ["python verify.py"],
                "protected_paths": protected_paths,
            }
        )
    )
    with pytest.raises(ValueError, match="protected_paths must be a list of nonempty strings"):
        runner.load_policy(policy)


def test_reconcile_integrated_intent_without_duplicate(project):
    assert invoke(project).returncode == 0
    saved, path = state(project)
    candidate = saved["items"][0]["evidence"]["candidate"]
    saved["items"][0]["phase"] = "integrating"
    runner.atomic(path, saved)
    assert invoke(project, "--resume", saved["id"]).returncode == 0
    assert runner.git(project, "rev-parse", saved["branch"]) == candidate
    assert len(state(project)[0]["items"][0]["attempts"]) == 1


def test_deadline_and_cancellation_cleanup(project):
    (project.parent / "agent.py").write_text(
        "import os,sys,time,subprocess\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        "Path(os.environ['TMPDIR'],'child.pid').write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        command(project), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=str(SOURCE / "src"))
    )
    for _ in range(100):
        paths = list(runner.location(project).glob("*/scratch-*/child.pid"))
        if paths:
            break
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 2, stderr
    assert json.loads(stdout)["items"][0]["phase"] == "cancelled"
    assert state(project)[0]["reserved_usd"] == 1
    child_pid = int(paths[0].read_text())
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("Cancelled worker descendant survived process-group cleanup")


def test_crash_resume_retains_allowance(project):
    (project.parent / "agent.py").write_text(
        "import os,time\nfrom pathlib import Path\n"
        "if os.environ['ROADRUNNER_ATTEMPT'] == '1': time.sleep(30)\nPath('one').write_text('fixed')\n"
    )
    process = subprocess.Popen(
        command(project), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONPATH=str(SOURCE / "src"))
    )
    for _ in range(100):
        if list(runner.location(project).glob("*/*worker.log")):
            break
        time.sleep(0.05)
    process.kill()
    process.wait(timeout=5)
    time.sleep(0.2)  # Guardian observes controller pipe EOF and kills worker group.
    saved, _ = state(project)
    result = invoke(project, "--resume", saved["id"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert state(project)[0]["reserved_usd"] == 2


def test_attempt_timeout_blocks(project):
    (project.parent / "agent.py").write_text("import time\ntime.sleep(30)\n")
    path = project.parent / "policy.json"
    policy = json.loads(path.read_text())
    policy.update(max_attempts=1, attempt_seconds=0.2)
    path.write_text(json.dumps(policy))
    result = invoke(project)
    assert result.returncode == 2
    saved, _ = state(project)
    assert "Deadline exceeded" in saved["items"][0]["attempts"][0]["reason"]
    assert saved["items"][0]["phase"] == "blocked"


def test_changed_integration_ref_cannot_resolve(project):
    # Simulate an external writer, using the explicit trusted fixture adapter.
    (project.parent / "agent.py").write_text("""import json, os, subprocess
from pathlib import Path
Path('one').write_text('fixed')
state = json.loads((Path(os.environ['TMPDIR']).parent / 'run.json').read_text())
subprocess.run(['git', 'update-ref', '-d', state['branch']], check=True)
""")
    result = invoke(project)
    assert result.returncode == 2
    saved, _ = state(project)
    assert saved["items"][0]["phase"] == "blocked"
    assert "update-ref" in saved["items"][0]["reason"]


def test_frozen_policy_tampering_rejected(project):
    assert invoke(project).returncode == 0
    saved, path = state(project)
    saved["policy"]["validation_commands"] = ["true"]
    runner.atomic(path, saved)
    result = invoke(project, "--resume", saved["id"])
    assert result.returncode == 2
    assert "policy hash mismatch" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS worker sandbox")
def test_sandbox_denies_controller_and_gate_writes(tmp_path):
    work = tmp_path / "work"
    scratch = tmp_path / "scratch"
    work.mkdir()
    scratch.mkdir()
    outside = tmp_path / "controller.json"
    outside.write_text("trusted")
    (work / "gate.py").write_text("trusted gate")
    policy = dict(sandbox="macos", protected_paths=["gate.py"])
    script = """from pathlib import Path
import sys
for target in sys.argv[1:]:
    try: Path(target).write_text('tampered')
    except PermissionError: pass
    else: raise RuntimeError('sandbox allowed protected write')
Path('allowed').write_text('ok')
"""
    log = tmp_path / "sandbox.log"
    code = runner.execute(
        [sys.executable, "-c", script, str(outside), str(work / "gate.py")], work, scratch, policy, time.time() + 5, log, dict(os.environ)
    )
    assert code == 0, log.read_text()
    assert outside.read_text() == "trusted"
    assert (work / "gate.py").read_text() == "trusted gate"
    assert (work / "allowed").read_text() == "ok"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS worker sandbox")
def test_external_project_under_enforced_sandbox(project):
    path = project.parent / "policy.json"
    policy = json.loads(path.read_text())
    policy["sandbox"] = "macos"
    path.write_text(json.dumps(policy))
    result = invoke(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert state(project)[0]["items"][0]["phase"] == "resolved"


def test_worker_claim_and_yaml_edits_cannot_resolve(project):
    (project.parent / "agent.py").write_text("print('ROADMAP_COMPLETE')\n")
    result = invoke(project)
    assert result.returncode == 2
    saved, _ = state(project)
    assert saved["items"][0]["phase"] == "blocked"
    assert "No implementation progress" in saved["items"][0]["attempts"][0]["reason"]


def test_doctor_fixture_preflight(project):
    result = subprocess.run(
        [sys.executable, "-m", "roadrunner", "doctor", "--project", str(project), "--policy", str(project.parent / "policy.json")],
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(SOURCE / "src")),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["outcome"] == "ready_for_canary"


def test_resume_detects_reverted_integration(project):
    assert invoke(project).returncode == 0
    saved, _ = state(project)
    runner.git(project, "update-ref", saved["branch"], saved["base"])
    result = invoke(project, "--resume", saved["id"])
    assert result.returncode == 2
    assert json.loads(result.stdout)["items"][0]["phase"] == "blocked"
    assert "no longer valid" in json.loads(result.stdout)["items"][0]["reason"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Claude adapter boundary")
@pytest.mark.parametrize("denied", [False, True])
def test_claude_adapter_structured_output_ignores_stderr(project, monkeypatch, denied):
    binaries = project.parent / "fake-bin"
    binaries.mkdir()
    executable = binaries / "claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        "if sys.argv[1:]==['auth','status']: print(json.dumps({'loggedIn':True})); sys.exit(0)\n"
        "assert '--max-budget-usd' in sys.argv\n"
        "assert '.git' not in Path.cwd().resolve().parts, 'Claude protected-path write denial'\n"
        f"denied = {denied!r}\n"
        "if denied: print(json.dumps({'total_cost_usd':0.1, 'permission_denials':[{'tool_name':'Write'}]})); sys.exit(0)\n"
        "Path('one').write_text('fixed')\n"
        "print('diagnostic warning',file=sys.stderr)\n"
        "print(json.dumps({'session_id':'fixture-session','total_cost_usd':0.1,'is_error':False}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    path = project.parent / "policy.json"
    policy = json.loads(path.read_text())
    # The sandbox denial stream can take ~16s to start; keep it inside the budgets.
    policy.update(adapter="claude", sandbox="macos", run_seconds=120, attempt_seconds=60)
    path.write_text(json.dumps(policy))
    result = invoke(project)
    if denied:
        assert result.returncode == 2
        assert "Claude tool permission denied" in json.loads(result.stdout)["items"][0]["reason"]
        saved, _ = state(project)
        assert runner.git(project, "rev-parse", saved["branch"]) == saved["base"]
        return
    assert result.returncode == 0, result.stdout + result.stderr
    saved, _ = state(project)
    attempt = saved["items"][0]["attempts"][0]
    work_root = Path(saved["task_work_root"])
    assert not work_root.is_relative_to(project)
    assert work_root.stat().st_mode & 0o777 == 0o700
    assert (work_root / "work-TASK-001" / "one").read_text() == "fixed"
    assert invoke(project, "--resume", saved["id"]).returncode == 0
    resumed, _ = state(project)
    assert resumed["task_work_root"] == str(work_root)
    assert len(resumed["items"][0]["attempts"]) == 1
    assert attempt["session_id"] == "fixture-session"
    assert attempt["cost_usd"] == 0.1
    assert "diagnostic warning" in Path(attempt["worker_stderr"]).read_text()


@pytest.mark.parametrize("damage", ["gate_version", "missing_gates", "wrong_command", "failed_gate"])
def test_interrupted_integration_rechecks_entire_gate(project, damage):
    assert invoke(project).returncode == 0
    saved, path = state(project)
    item = saved["items"][0]
    item["phase"] = "integrating"
    evidence = item["evidence"]
    runner.git(project, "update-ref", saved["branch"], evidence["base"])
    if damage == "gate_version":
        evidence["gate_version"] = "stale-policy"
    elif damage == "missing_gates":
        evidence["gates"] = []
    elif damage == "wrong_command":
        evidence["gates"][0]["command"] = "true"
    else:
        evidence["gates"][0]["exit_code"] = 1
    runner.atomic(path, saved)
    result = invoke(project, "--resume", saved["id"])
    assert result.returncode == 2, result.stdout + result.stderr
    assert state(project)[0]["items"][0]["phase"] == "blocked"
    assert runner.git(project, "rev-parse", saved["branch"]) == evidence["base"]


@pytest.mark.parametrize("phase", ["resolved", "integrating"])
def test_stale_attempt_evidence_cannot_release_work(project, phase):
    assert invoke(project).returncode == 0
    saved, path = state(project)
    item = saved["items"][0]
    item["phase"] = phase
    item["evidence"]["attempt_id"] = "previous-attempt"
    runner.atomic(path, saved)
    result = invoke(project, "--resume", saved["id"])
    assert result.returncode == 2
    assert "current attempt" in json.loads(result.stdout)["items"][0]["reason"]


def test_reconcile_valid_intent_before_ref_update(project):
    assert invoke(project).returncode == 0
    saved, path = state(project)
    item = saved["items"][0]
    item["phase"] = "integrating"
    runner.git(project, "update-ref", saved["branch"], item["evidence"]["base"])
    runner.atomic(path, saved)
    result = invoke(project, "--resume", saved["id"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert runner.git(project, "rev-parse", saved["branch"]) == item["evidence"]["candidate"]
    assert state(project)[0]["reserved_usd"] == saved["reserved_usd"]
    assert len(state(project)[0]["items"][0]["attempts"]) == 1


def test_worker_preserves_auth_namespace_without_broadening_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/configured/auth/namespace")
    env = runner.worker_environment(tmp_path / "worker", tmp_path / "scratch")
    assert env["CLAUDE_CONFIG_DIR"] == "/configured/auth/namespace"
    assert env["TMPDIR"] == str(tmp_path / "scratch")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert "CLAUDE_CONFIG_DIR" not in runner.worker_environment(tmp_path, tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Claude adapter boundary")
def test_auth_preflight_stops_before_attempts_and_budget(project, monkeypatch):
    binaries = project.parent / "fake-bin"
    binaries.mkdir()
    executable = binaries / "claude"
    executable.write_text(
        f"#!{sys.executable}\nimport sys,json\n"
        "assert sys.argv[1:]==['auth','status']\nprint(json.dumps({'loggedIn':False}))\nsys.exit(1)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    path = project.parent / "policy.json"
    policy = json.loads(path.read_text())
    policy.update(adapter="claude", sandbox="macos")
    path.write_text(json.dumps(policy))
    result = invoke(project)
    assert result.returncode == 2
    saved, _ = state(project)
    assert saved["reserved_usd"] == 0
    assert saved["items"][0]["attempts"] == []
    assert "authentication unavailable inside" in saved["items"][0]["reason"]


def test_retry_context_empty_on_first_attempt(tmp_path):
    item = {"attempts": [{"number": 1}]}
    assert runner.retry_context(item, tmp_path) == ""


def test_retry_context_includes_reason_and_gate_log(tmp_path):
    log = tmp_path / "gate.log"
    log.write_text("AssertionError: expected fixed\n")
    item = {
        "attempts": [
            {
                "number": 1,
                "outcome": "failed",
                "failure_category": "validation",
                "reason": "Validation failed: pytest",
                "failure_log": str(log),
            },
            {"number": 2},
        ]
    }
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    text = runner.retry_context(item, scratch)
    assert "Previous attempt 1 failed (validation): Validation failed: pytest" in text
    assert str(scratch / "previous-gate.log") in text
    assert "AssertionError" in (scratch / "previous-gate.log").read_text()


def test_worker_command_appends_previous_to_prompt(tmp_path):
    policy = {"adapter": "claude"}
    cmd = runner.worker_command(policy, {"id": "T"}, {"budget_usd": 1}, tmp_path, "\n\nPrevious attempt 1 failed")
    assert cmd[2].endswith("Previous attempt 1 failed")


def test_retry_records_failure_log(project):
    agent = project.parent / "agent.py"
    agent.write_text(
        "import os\nfrom pathlib import Path\nPath('one').write_text('bad' if os.environ['ROADRUNNER_ATTEMPT'] == '1' else 'fixed')\n"
    )
    result = invoke(project)
    assert result.returncode == 0, result.stdout + result.stderr
    saved, _ = state(project)
    first = saved["items"][0]["attempts"][0]
    assert first["failure_log"].endswith("-gate-0.log")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("API Error: 529 overloaded_error", "transient"),
        ("rate limit exceeded, retry later", "transient"),
        ("connect ECONNRESET 1.2.3.4:443", "transient"),
        ("Not logged in. Run claude auth login", "auth"),
        ("HTTP 401 Unauthorized", "auth"),
        ("HTTP 403 rate limit", "auth"),
        ("tests failed: assertion error", "agent"),
        ("File \"app.py\", line 401, in handler", "agent"),
        ("expected 500 rows, got 3", "agent"),
        ("", "agent"),
    ],
)
def test_classify_worker_failure(text, expected):
    assert runner.classify_worker_failure(text) == expected


def _flaky_agent(project, stderr_text, writes_first=False):
    agent = project.parent / "agent.py"
    agent.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "if os.environ['ROADRUNNER_ATTEMPT'] == '1':\n"
        + ("    Path('one').write_text('partial')\n" if writes_first else "")
        + f"    sys.stderr.write({stderr_text!r})\n    sys.exit(1)\n"
        "Path('one').write_text('fixed')\n"
    )
    policy_path = project.parent / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["max_attempts"] = 1
    policy_path.write_text(json.dumps(policy))


def test_transient_failure_retries_free_when_no_files_changed(project):
    _flaky_agent(project, "API Error: 529 overloaded")
    result = invoke(project)
    assert result.returncode == 0, result.stdout + result.stderr
    saved, _ = state(project)
    attempts = saved["items"][0]["attempts"]
    assert [a["outcome"] for a in attempts] == ["failed", "resolved"]
    assert attempts[0]["uncharged"] is True
    assert attempts[0]["failure_category"] == "transient"
    assert saved["items"][0]["phase"] == "resolved"


def test_transient_failure_is_charged_when_files_changed(project):
    _flaky_agent(project, "API Error: 529 overloaded", writes_first=True)
    invoke(project)
    saved, _ = state(project)
    attempts = saved["items"][0]["attempts"]
    assert not attempts[0].get("uncharged")
    assert saved["items"][0]["phase"] == "blocked"
    assert len(attempts) == 1


def test_auth_failure_blocks_immediately_without_charging(project):
    _flaky_agent(project, "Not logged in. Run claude auth login")
    invoke(project)
    saved, _ = state(project)
    item = saved["items"][0]
    assert item["phase"] == "blocked"
    assert len(item["attempts"]) == 1
    assert item["attempts"][0]["failure_category"] == "auth"
    assert item["attempts"][0]["uncharged"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I am blocked: the API key name is not in the task.", True),
        ("Please clarify which endpoint to change.", True),
        ("The spec is ambiguous about the return type.", True),
        ("Done. Implemented the change and updated the tests.", False),
        ("", False),
        (None, False),
    ],
)
def test_states_blocker(text, expected):
    assert runner.states_blocker(text) is expected


def _claude_agent(project, monkeypatch, result, writes):
    binaries = project.parent / "fake-bin"
    binaries.mkdir()
    executable = binaries / "claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        "if sys.argv[1:]==['auth','status']: print(json.dumps({'loggedIn':True})); sys.exit(0)\n"
        f"if {writes!r}: Path('one').write_text('fixed')\n"
        f"print(json.dumps({{'session_id':'s','total_cost_usd':0.1,'is_error':False,'result':{result!r}}}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    path = project.parent / "policy.json"
    policy = json.loads(path.read_text())
    policy.update(adapter="claude", sandbox="macos")
    path.write_text(json.dumps(policy))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Claude adapter boundary")
def test_no_change_blocker_goes_to_needs_input_without_retry(project, monkeypatch):
    _claude_agent(project, monkeypatch, "I am blocked: which file should hold the config?", writes=False)
    invoke(project)
    saved, _ = state(project)
    item = saved["items"][0]
    assert item["phase"] == "needs_input"
    assert len(item["attempts"]) == 1
    attempt = item["attempts"][0]
    assert attempt["failure_category"] == "needs_input"
    assert attempt["uncharged"] is True
    assert "which file" in attempt["worker_summary"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Claude adapter boundary")
def test_worker_summary_recorded_and_progress_still_resolves(project, monkeypatch):
    _claude_agent(project, monkeypatch, "Done. This is ambiguous wording but files changed.", writes=True)
    result = invoke(project)
    assert result.returncode == 0, result.stdout + result.stderr
    saved, _ = state(project)
    assert saved["items"][0]["phase"] == "resolved"
    assert saved["items"][0]["attempts"][0]["worker_summary"].startswith("Done.")


SAMPLE_LOG = """Timestamp Ty Process[PID:TID]
2026-10-22 23:52:12.220 E  kernel[0:1] (Sandbox) Sandbox: python3.14(4242) deny(1) file-write-create /Users/x/.ssh/evil
2026-10-22 23:52:12.221 E  kernel[0:1] (Sandbox) Sandbox: python3.14(4242) deny(1) file-write-create /Users/x/.ssh/evil
2026-10-22 23:52:12.222 E  kernel[0:1] (Sandbox) Sandbox: Chrome(999) deny(1) file-read-data /unrelated
2026-10-22 23:52:12.223 E  kernel[0:1] (Sandbox) Sandbox: sh(4243) deny(1) network-outbound 1.2.3.4:443
"""


def test_parse_sandbox_denials_filters_pids_and_dedupes():
    found = runner.parse_sandbox_denials(SAMPLE_LOG, {4242, 4243})
    assert found == [
        {"process": "python3.14", "operation": "file-write-create", "target": "/Users/x/.ssh/evil"},
        {"process": "sh", "operation": "network-outbound", "target": "1.2.3.4:443"},
    ]
    assert runner.parse_sandbox_denials(SAMPLE_LOG, set()) == []
    assert len(runner.parse_sandbox_denials(SAMPLE_LOG, {4242, 4243}, limit=1)) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")
def test_sandbox_denial_is_recorded_and_categorised(project, monkeypatch, tmp_path):
    outside = tmp_path / "outside-sandbox.txt"
    binaries = project.parent / "fake-bin"
    binaries.mkdir()
    executable = binaries / "claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        "if sys.argv[1:]==['auth','status']: print(json.dumps({'loggedIn':True})); sys.exit(0)\n"
        f"try: Path({str(outside)!r}).write_text('x')\nexcept OSError: pass\nimport time; time.sleep(1)\n"
        "print(json.dumps({'session_id':'s','total_cost_usd':0.1,'is_error':False,'result':'done'}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    path = project.parent / "policy.json"
    policy = json.loads(path.read_text())
    policy.update(adapter="claude", sandbox="macos", max_attempts=1, attempt_seconds=30)
    path.write_text(json.dumps(policy))
    invoke(project)
    saved, _ = state(project)
    attempt = saved["items"][0]["attempts"][0]
    assert not outside.exists()
    assert attempt["failure_category"] == "sandbox"
    assert any(d["target"] == str(outside) for d in attempt["sandbox_denials"]), attempt["sandbox_denials"]
    assert "Sandbox denied" in attempt["reason"]
