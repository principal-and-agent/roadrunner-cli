"""Shipping regressions: real subprocesses, isolated project and runtime state."""
import json
import os
from pathlib import Path
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
