"""disk-cleanup.sh: what it keeps in Harbor and what it removes from nodes; no cluster, Harbor or SSH."""
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'scripts/lib'
pytestmark = pytest.mark.skipif(shutil.which('jq') is None, reason='jq not installed')


def digest(n):
    return f'sha256:{n:064x}'


def artifact(n, pushed, tags=(), children=()):
    return {'digest': digest(n), 'push_time': f'2026-11-{pushed:02d}T10:00:00.000Z', 'size': 100 * n,
            'tags': [{'name': t} for t in tags] or None,
            'references': [{'child_digest': digest(c)} for c in children] or None}


def plan(artifacts, keep=2, in_use=(), tmp_path=None):
    used = tmp_path / 'inuse.json'
    used.write_text(json.dumps({digest(n): True for n in in_use}))
    out = subprocess.run(['jq', '--argjson', 'keep', str(keep), '--slurpfile', 'inuse', str(used),
                          '-f', str(LIB / 'harbor-prune-plan.jq')],
                         input=json.dumps(artifacts), capture_output=True, text=True, check=True)
    return {int(a['digest'][7:], 16): a['reason'] for a in json.loads(out.stdout)}


def test_keeps_the_newest_pushes_tagged_or_not(tmp_path):
    result = plan([artifact(1, 1), artifact(2, 2), artifact(3, 3, ['latest']), artifact(4, 4)], tmp_path=tmp_path)
    assert result == {4: 'newest 1', 3: 'newest 2', 2: None, 1: None}


def test_keeps_latest_after_a_rollback_and_whatever_runs(tmp_path):
    # latest was pointed back at build 1; build 2 is still running somewhere.
    result = plan([artifact(1, 1, ['latest']), artifact(2, 2), artifact(3, 3), artifact(4, 4), artifact(5, 5)],
                  in_use=[2], tmp_path=tmp_path)
    assert result == {5: 'newest 1', 4: 'newest 2', 3: None, 2: 'in use', 1: 'tagged latest'}


def test_an_index_counts_once_and_a_running_child_keeps_it(tmp_path):
    artifacts = [artifact(10, 1, children=[11, 12]), artifact(11, 1), artifact(12, 1), artifact(20, 2), artifact(30, 3)]
    assert plan(artifacts, keep=1, tmp_path=tmp_path) == {30: 'newest 1', 20: None, 10: None}
    assert plan(artifacts, keep=1, in_use=[12], tmp_path=tmp_path)[10] == 'in use'


def test_keep_larger_than_the_repository_deletes_nothing(tmp_path):
    assert set(plan([artifact(1, 1), artifact(2, 2)], keep=5, tmp_path=tmp_path).values()) == {'newest 1', 'newest 2'}


def unused(tmp_path, images, containers):
    (tmp_path / 'i.json').write_text(json.dumps({'images': images}))
    (tmp_path / 'c.json').write_text(json.dumps({'containers': containers}))
    out = subprocess.run(['jq', '-n', '--slurpfile', 'i', str(tmp_path / 'i.json'), '--slurpfile', 'c',
                          str(tmp_path / 'c.json'), '-f', str(LIB / 'unused-images.jq')],
                         capture_output=True, text=True, check=True)
    return [i['name'] for i in json.loads(out.stdout)]


def test_unused_images_skip_running_exited_and_pinned(tmp_path):
    images = [
        {'id': 'sha256:a', 'repoTags': ['app:latest'], 'repoDigests': [], 'size': '10'},          # running
        {'id': 'sha256:b', 'repoTags': [], 'repoDigests': ['old@sha256:b1'], 'size': '30'},       # unused, untagged
        {'id': 'sha256:c', 'repoTags': ['job:1'], 'repoDigests': [], 'size': '20'},               # exited container
        {'id': 'sha256:d', 'repoTags': ['pause:3.10'], 'repoDigests': [], 'size': '1', 'pinned': True},
        {'id': 'sha256:e', 'repoTags': ['gone:2'], 'repoDigests': [], 'size': '50'},              # unused
    ]
    containers = [{'imageRef': 'sha256:a', 'image': {'image': 'sha256:a'}, 'state': 'CONTAINER_RUNNING'},
                  {'imageRef': 'job:1', 'image': {'image': 'job:1'}, 'state': 'CONTAINER_EXITED'}]
    assert unused(tmp_path, images, containers) == ['gone:2', 'old@sha256:b1']


def test_node_without_containers_lists_every_unpinned_image(tmp_path):
    assert unused(tmp_path, [{'id': 'sha256:x', 'size': '5'}], []) == ['sha256:x']


@pytest.mark.parametrize('args', [['--keep', '0'], ['--keep'], ['--only', 'disks'], ['--bogus']])
def test_bad_arguments_stop_before_touching_anything(args):
    result = subprocess.run(['bash', str(ROOT / 'disk-cleanup.sh'), *args], cwd='/tmp', capture_output=True, text=True)
    assert result.returncode == 2


# --- the Harbor step end to end, against fake kubectl, curl, date and sleep -------------------------------------------
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys, urllib.parse
args, login, state = sys.argv[1:], sys.stdin.read(), os.environ['FAKE_STATE']
url = urllib.parse.urlsplit(next(a for a in args if a.startswith('http')))
method, path = args[args.index('-X') + 1], url.path.removeprefix('/api/v2.0')
page = int(urllib.parse.parse_qs(url.query).get('page', ['1'])[0])
with open(os.path.join(state, 'calls'), 'a') as f:
    f.write(json.dumps({'method': method, 'path': path, 'argv': args, 'login': login}) + '\n')
if login != os.environ['FAKE_LOGIN']:
    print('curl: (56) The requested URL returned error: 401', file=sys.stderr); sys.exit(56)
started = os.path.exists(os.path.join(state, 'gc-started'))
if (method, path) == ('POST', '/system/gc/schedule'):
    open(os.path.join(state, 'gc-started'), 'w').close(); sys.exit(0)
if (method, path) == ('GET', '/system/gc'):
    print(json.dumps([{'id': 2 if started else 1, 'job_status': 'Success'}])); sys.exit(0)
if method == 'GET' and path.endswith('/log'):
    print('The GC job actual frees up 42 MB space.'); sys.exit(0)
if method == 'DELETE':
    sys.exit(0)
body = json.load(open(os.path.join(state, 'harbor.json'))).get(path)
if body is None:
    print(f'unexpected {method} {path}', file=sys.stderr); sys.exit(22)
print(json.dumps(body if page == 1 or not isinstance(body, list) else []))
'''

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import base64, json, os, sys
args = sys.argv[1:]
if args[:2] == ['config', 'current-context']:
    print('test'); sys.exit(0)
assert args[:2] == ['--context', 'test'], args
args = [a for a in args[2:] if not a.startswith('--request-timeout')]
if args == ['get', '--raw', '/readyz']:
    print('ok')
elif args[:5] == ['-n', 'harbor', 'get', 'secret', 'harbor-credentials']:
    print(base64.b64encode(b'pw').decode())
elif args[0] == 'get' and args[1].startswith('pods,'):
    print(open(os.path.join(os.environ['FAKE_STATE'], 'workloads.json')).read())
else:
    print(f'unexpected kubectl {args}', file=sys.stderr); sys.exit(1)
'''


@pytest.fixture
def fake_harbor(tmp_path):
    bin_dir, state = tmp_path / 'bin', tmp_path / 'state'
    bin_dir.mkdir(), state.mkdir()
    for name, body in {'curl': FAKE_CURL, 'kubectl': FAKE_KUBECTL,
                       'date': '#!/bin/sh\necho 1200\n', 'sleep': '#!/bin/sh\nexit 0\n'}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    agent = [artifact(1, 1), artifact(2, 2), artifact(3, 3), artifact(4, 4), artifact(5, 5, ['latest'])]
    nested = [artifact(7, 1), artifact(8, 2), artifact(9, 3, ['latest'])]
    (state / 'harbor.json').write_text(json.dumps({
        '/users/current': {'username': 'admin'},
        '/projects': [{'name': 'jithendar'}],
        '/projects/jithendar/repositories': [{'name': 'jithendar/seo-agent', 'artifact_count': 5},
                                             {'name': 'jithendar/team/app', 'artifact_count': 3},
                                             {'name': 'jithendar/empty', 'artifact_count': 0}],
        '/projects/jithendar/repositories/seo-agent/artifacts': agent,
        '/projects/jithendar/repositories/team%252Fapp/artifacts': nested,
    }))
    # A pod still runs build 2 of seo-agent.
    (state / 'workloads.json').write_text(json.dumps({'items': [{'status': {'containerStatuses': [
        {'image': 'registry.jithendar.com/jithendar/seo-agent:latest',
         'imageID': f'registry.jithendar.com/jithendar/seo-agent@{digest(2)}'}]}}]}))

    def run(*args, harbor_login='user = "admin:pw"\n', typed=None, **extra_env):
        """Harbor accepts only `harbor_login` (the Secret holds pw); `typed` is typed into a terminal on stdin."""
        env = {'PATH': f'{bin_dir}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin', 'FAKE_STATE': str(state),
               'HOME': str(tmp_path), 'FAKE_LOGIN': harbor_login, **extra_env}
        cmd = ['bash', str(ROOT / 'disk-cleanup.sh'), '--only', 'harbor', *args]
        if typed is None:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        else:   # type only once the prompt is up: read -s drops what was typed ahead (macOS)
            terminal, stdin = pty.openpty()
            try:
                proc = subprocess.Popen(cmd, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                err = b''
                while b'password for' not in err and proc.poll() is None:
                    err += os.read(proc.stderr.fileno(), 1024)
                os.write(terminal, typed.encode())
                out, rest = proc.communicate(timeout=60)
                result = subprocess.CompletedProcess(cmd, proc.returncode, out.decode(), (err + rest).decode())
            finally:
                os.close(stdin), os.close(terminal)
        log = state / 'calls'
        return result, [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return run


def test_harbor_dry_run_only_reads(fake_harbor):
    result, calls = fake_harbor()
    assert result.returncode == 0, result.stderr
    assert {c['method'] for c in calls} == {'GET'}
    assert 'jithendar/seo-agent: 5 artifacts, 2 to delete' in result.stdout
    assert 'jithendar/team/app: 3 artifacts, 1 to delete' in result.stdout


def test_harbor_apply_deletes_old_builds_then_collects_garbage(fake_harbor):
    result, calls = fake_harbor('--apply', '--yes')
    assert result.returncode == 0, result.stderr
    changes = [(c['method'], c['path']) for c in calls if c['method'] != 'GET']
    agent, nested = '/projects/jithendar/repositories/seo-agent', '/projects/jithendar/repositories/team%252Fapp'
    assert changes == [
        ('DELETE', f'{agent}/artifacts/{digest(3)}'),        # 5 and 4 are newest, 2 runs, 3 and 1 go
        ('DELETE', f'{agent}/artifacts/{digest(1)}'),
        ('DELETE', f'{nested}/artifacts/{digest(7)}'),
        ('DELETE', '/projects/jithendar/repositories/empty'),
        ('POST', '/system/gc/schedule'),
    ]
    assert 'frees up 42 MB' in result.stdout
    # The login travels on stdin only.
    assert all(c['login'] == 'user = "admin:pw"\n' and not any('pw' in a for a in c['argv']) for c in calls)


def test_apply_without_a_terminal_needs_yes(fake_harbor):
    result, calls = fake_harbor('--apply')
    assert result.returncode == 1 and 'add --yes' in result.stderr
    assert calls == []


def test_a_refused_secret_without_a_terminal_stops_before_any_change(fake_harbor):
    result, calls = fake_harbor('--apply', '--yes', harbor_login='user = "admin:changed"\n')
    assert result.returncode == 1 and 'set HARBOR_PASSWORD' in result.stderr
    assert {c['method'] for c in calls} == {'GET'} and {c['path'] for c in calls} == {'/users/current'}


@pytest.mark.parametrize('password, login', [('changed', 'user = "admin:changed"\n'),
                                             ('a"b\\c', 'user = "admin:a\\"b\\\\c"\n')])
def test_harbor_password_overrides_the_secret_and_is_quoted_for_curl(fake_harbor, password, login):
    result, _ = fake_harbor(harbor_login=login, HARBOR_PASSWORD=password)
    assert result.returncode == 0, result.stderr


def test_a_refused_secret_asks_for_the_password_on_the_terminal(fake_harbor):
    result, calls = fake_harbor('--apply', '--yes', harbor_login='user = "admin:changed"\n', typed='changed\n')
    assert result.returncode == 0, result.stderr
    assert "refused admin's password from Secret harbor/harbor-credentials" in result.stdout
    assert ('POST', '/system/gc/schedule') in [(c['method'], c['path']) for c in calls]
    assert not any('changed' in a for c in calls for a in c['argv'])
