"""Shipping regressions: real subprocesses, isolated project and runtime state."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'tasks').mkdir()
    (tmp_path / 'logs').mkdir()
    shutil.copytree(SOURCE / 'hooks', tmp_path / 'hooks')
    task = dict(id='TASK-001', title='Test', status='todo', depends_on=[],
                goal='Test', acceptance_criteria=['verified'], files_expected=['result'], validation_commands=['true'])
    (tmp_path / 'tasks/tasks.yaml').write_text(yaml.safe_dump({'coverage_floor': 0, 'tasks': [task]}))
    return tmp_path


def invoke(project, *args, payload=None):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), PYTHONPATH=str(SOURCE / 'src'))
    command = ['bash', str(project / 'hooks/stop_hook.sh')] if args == ('hook',) else [sys.executable, '-m', 'roadrunner', *args]
    return subprocess.run(command, cwd=project, env=env, input=json.dumps(payload or {}), text=True, capture_output=True)


def test_real_hook_continues_active_flag(project):
    result = invoke(project, 'hook', payload={'stop_hook_active': True})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['decision'] == 'block'


def test_sentinel_cannot_complete_pending(project):
    result = invoke(project, 'check-stop', payload={'last_assistant_message': 'ROADMAP_COMPLETE'})
    assert json.loads(result.stdout)['decision'] == 'block'
    log = project / 'logs/CHANGELOG.md'
    assert not log.exists() or 'ALL → complete' not in log.read_text()


def test_unstarted_cannot_complete(project):
    result = invoke(project, 'complete', 'TASK-001')
    assert result.returncode != 0
    assert yaml.safe_load((project / 'tasks/tasks.yaml').read_text())['tasks'][0]['status'] == 'todo'


def test_stop_preserves_base(project):
    (project / '.roadmap_state.json').write_text(json.dumps({'base_branch': 'release'}))
    invoke(project, 'check-stop')
    assert json.loads((project / '.roadmap_state.json').read_text())['base_branch'] == 'release'


def test_stop_hook_uses_default_iteration_cap_for_invalid_environment(project, monkeypatch):
    (project / '.roadmap_state.json').write_text(json.dumps({'session_iteration': 99}))
    monkeypatch.setenv('ROADMAP_MAX_ITERATIONS', 'abc')
    result = invoke(project, 'hook')
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision['continue'] is False
    assert 'Max iterations (100)' in decision['stopReason']
    assert result.stderr.count('ROADMAP_MAX_ITERATIONS') == 1


@pytest.mark.parametrize('budget', ['abc', 'inf', '-5'])
def test_stop_hook_warns_for_invalid_budget(project, monkeypatch, budget):
    monkeypatch.setenv('ROADMAP_MAX_BUDGET_USD', budget)
    result = invoke(project, 'hook', payload={'total_cost_usd': 100.0})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['decision'] == 'block'
    assert result.stderr.count('ROADMAP_MAX_BUDGET_USD') == 1


def test_block_preserves_completed_tasks_and_records_prior_status(project):
    result = invoke(project, 'block', 'TASK-001', '--notes', 'Waiting for review')
    assert result.returncode == 0, result.stderr
    path = project / 'tasks/tasks.yaml'
    data = yaml.safe_load(path.read_text())
    assert data['tasks'][0]['status'] == 'blocked'
    data['tasks'][0]['status'] = 'done'
    path.write_text(yaml.safe_dump(data))
    result = invoke(project, 'block', 'TASK-001')
    assert result.returncode != 0
    assert 'done' in result.stderr
    assert yaml.safe_load(path.read_text())['tasks'][0]['status'] == 'done'
    events = [json.loads(line) for line in (project / 'logs/trace.jsonl').read_text().splitlines()]
    assert [(event['event'], event['from_status']) for event in events if event['event'].startswith('task_block')] == [
        ('task_block', 'todo'), ('task_block_rejected', 'done'),
    ]


@pytest.mark.parametrize('defect', ['duplicate', 'unknown', 'cycle'])
def test_invalid_plan_rejected(project, defect):
    path = project / 'tasks/tasks.yaml'
    data = yaml.safe_load(path.read_text())
    if defect == 'duplicate':
        data['tasks'] *= 2
    else:
        data['tasks'][0]['depends_on'] = ['TASK-001' if defect == 'cycle' else 'TASK-999']
    path.write_text(yaml.safe_dump(data))
    assert invoke(project, 'health').returncode != 0


def git(project, *args):
    return subprocess.run(['git', *args], cwd=project, text=True, capture_output=True, check=True).stdout.strip()


def initialize_git(project):
    git(project, 'init', '-b', 'main')
    git(project, 'config', 'user.name', 'Fixture')
    git(project, 'config', 'user.email', 'fixture@example.invalid')
    (project / 'result').write_text('base')
    git(project, 'add', '.')
    git(project, 'commit', '-m', 'baseline')


def test_retry_allowance_resets_on_new_start(project):
    (project / '.roadmap_state.json').write_text(json.dumps({'attempts_per_task': {'TASK-001': 5}, 'iteration': 10}))
    assert invoke(project, 'start', 'TASK-001').returncode == 0
    result = invoke(project, 'check-stop')
    assert 'RESUME IN-PROGRESS' in json.loads(result.stdout)['reason']
    assert json.loads((project / '.roadmap_state.json').read_text())['iteration'] == 11


def test_merge_conflict_never_completes(project):
    initialize_git(project)
    assert invoke(project, 'start', 'TASK-001').returncode == 0
    (project / 'result').write_text('task')
    git(project, 'add', '.')
    git(project, 'commit', '-m', 'task change')
    git(project, 'checkout', 'main')
    (project / 'result').write_text('competing change')
    git(project, 'add', 'result')
    git(project, 'commit', '-m', 'conflict')
    git(project, 'checkout', 'roadrunner/TASK-001')
    result = invoke(project, 'complete', 'TASK-001')
    assert result.returncode != 0
    assert 'marked done' not in result.stdout
    assert yaml.safe_load((project / 'tasks/tasks.yaml').read_text())['tasks'][0]['status'] != 'done'
    assert git(project, 'branch', '--list', 'roadrunner/TASK-001')


def test_packaged_scaffold_assets():
    scaffold = SOURCE / 'src/roadrunner/scaffold'
    assert (scaffold / '.claude/settings.json').is_file()
    assert (scaffold / 'hooks/stop_hook.sh').read_bytes() == (SOURCE / 'hooks/stop_hook.sh').read_bytes()


def run_registered_roadmap_guard(project, tool, file_path, *, cwd=None):
    settings = json.loads((project / '.claude/settings.json').read_text())
    hooks = [hook for hook in settings['hooks']['PreToolUse'] if re.fullmatch(hook['matcher'], tool)]
    assert len(hooks) == 1
    command, = hooks[0]['hooks']
    assert command['type'] == 'command'
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': tool,
               'cwd': str(cwd or project), 'tool_input': {'file_path': str(file_path)}}
    return subprocess.run(
        ['bash', '-c', command['command']], input=json.dumps(payload), text=True, capture_output=True,
        cwd=cwd or project, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project)),
    )


@pytest.mark.parametrize('scaffolded', [False, True], ids=['repository', 'init-scaffold'])
def test_configured_pretool_hook_protects_only_the_roadmap(project, scaffolded):
    if scaffolded:
        target = project / 'generated project'
        result = invoke(project, 'init', str(target))
        assert result.returncode == 0, result.stderr
        project = target
    else:
        (project / '.claude').mkdir()
        shutil.copy2(SOURCE / '.claude/settings.json', project / '.claude/settings.json')

    roadmap = project / 'tasks/tasks.yaml'
    original = roadmap.read_bytes()
    alias = project / 'roadmap-link.yaml'
    alias.symlink_to(roadmap)
    source = project / 'src'
    source.mkdir()
    for relative_path in ('src/tasks.yaml', 'src/main.py'):
        (project / relative_path).touch()

    for tool in ('Write', 'Edit'):
        for path in (roadmap, 'tasks/tasks.yaml', alias):
            result = run_registered_roadmap_guard(project, tool, path)
            assert result.returncode == 2, (path, result.stderr)
            assert 'roadrunner start' in result.stderr
            assert 'roadrunner complete' in result.stderr
        result = run_registered_roadmap_guard(project, tool, '../tasks/tasks.yaml', cwd=source)
        assert result.returncode == 2, result.stderr
        for path in (source / 'tasks.yaml', 'src/tasks.yaml', 'src/main.py'):
            result = run_registered_roadmap_guard(project, tool, path)
            assert result.returncode == 0, (path, result.stderr)
    assert roadmap.read_bytes() == original


def test_cli_can_update_the_guarded_roadmap(project):
    (project / '.claude').mkdir()
    shutil.copy2(SOURCE / '.claude/settings.json', project / '.claude/settings.json')
    initialize_git(project)
    roadmap = project / 'tasks/tasks.yaml'
    assert run_registered_roadmap_guard(project, 'Edit', roadmap).returncode == 2

    result = invoke(project, 'start', 'TASK-001')
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(roadmap.read_text())['tasks'][0]['status'] == 'in_progress'

    (project / 'result').write_text('task change')
    git(project, 'add', 'result')
    git(project, 'commit', '-m', 'task change')
    result = invoke(project, 'complete', 'TASK-001')
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(roadmap.read_text())['tasks'][0]['status'] == 'done'


@pytest.mark.parametrize(('scope', 'field', 'changed'), [
    ('task', 'validation_commands', ['touch replacement-ran']),
    ('project', 'baseline_validation', ['touch replacement-ran']),
    ('project', 'coverage_floor', 1),
    ('task', 'validation_timeout', 5),
])
def test_completion_requires_start_time_validation(project, scope, field, changed):
    path = project / 'tasks/tasks.yaml'
    data = yaml.safe_load(path.read_text())
    data['tasks'][0]['validation_commands'] = ['test -f result && touch original-ran']
    path.write_text(yaml.safe_dump(data))
    initialize_git(project)
    assert invoke(project, 'start', 'TASK-001').returncode == 0
    original = path.read_text()
    data = yaml.safe_load(original)
    target = data if scope == 'project' else data['tasks'][0]
    target[field] = changed
    path.write_text(yaml.safe_dump(data))

    result = invoke(project, 'complete', 'TASK-001')
    assert result.returncode != 0
    assert 'validation definition changed' in (result.stdout + result.stderr).lower()
    assert not (project / 'replacement-ran').exists()
    assert not (project / 'original-ran').exists()
    assert yaml.safe_load(path.read_text())['tasks'][0]['status'] == 'in_progress'
    assert git(project, 'branch', '--show-current') == 'roadrunner/TASK-001'

    path.write_text(original)
    result = invoke(project, 'complete', 'TASK-001')
    assert result.returncode == 0, result.stderr
    assert (project / 'original-ran').exists()
    assert yaml.safe_load(path.read_text())['tasks'][0]['status'] == 'done'


def test_legacy_active_task_without_validation_snapshot_requires_restart(project):
    initialize_git(project)
    assert invoke(project, 'start', 'TASK-001').returncode == 0
    path = project / '.roadmap_state.json'
    state = json.loads(path.read_text())
    del state['active_validation']
    path.write_text(json.dumps(state))

    result = invoke(project, 'complete', 'TASK-001')
    assert result.returncode != 0
    assert 'restart this task from todo' in result.stderr
    assert git(project, 'branch', '--show-current') == 'roadrunner/TASK-001'
