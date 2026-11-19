"""disk-cleanup.sh: what it keeps in Harbor, what it removes from nodes and how it reports sizes; no cluster, Harbor or
SSH (fake kubectl, curl, ssh, date and sleep on PATH)."""
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'scripts/lib'
GiB, MiB = 1 << 30, 1 << 20
pytestmark = pytest.mark.skipif(shutil.which('jq') is None, reason='jq not installed')


def digest(n):
    return f'sha256:{n:064x}'


def artifact(n, pushed, tags=(), children=()):
    return {'digest': digest(n), 'push_time': f'2026-11-{pushed:02d}T10:00:00.000Z', 'size': 100 * n,
            'tags': [{'name': t} for t in tags] or None,
            'references': [{'child_digest': digest(c)} for c in children] or None}


def plan(artifacts, keep=2, pinned=(), tmp_path=None):
    used = tmp_path / 'inuse.json'
    used.write_text(json.dumps({digest(n): True for n in pinned}))
    out = subprocess.run(['jq', '--argjson', 'keep', str(keep), '--slurpfile', 'inuse', str(used),
                          '-f', str(LIB / 'harbor-prune-plan.jq')],
                         input=json.dumps(artifacts), capture_output=True, text=True, check=True)
    return {int(a['digest'][7:], 16): a['reason'] for a in json.loads(out.stdout)}


def test_every_untagged_artifact_goes_and_the_newest_tagged_stay(tmp_path):
    result = plan([artifact(1, 1, ['v1']), artifact(2, 2), artifact(3, 3, ['v2']), artifact(4, 4, ['v3']),
                   artifact(5, 5), artifact(6, 6, ['latest'])], tmp_path=tmp_path)
    assert result == {6: 'newest tagged 1', 5: None, 4: 'newest tagged 2', 3: None, 2: None, 1: None}


def test_latest_after_a_rollback_and_pinned_digests_stay(tmp_path):
    # latest was pointed back at build 1; a workload pins build 2 by digest.
    result = plan([artifact(1, 1, ['latest']), artifact(2, 2), artifact(3, 3, ['v3']), artifact(4, 4, ['v4']),
                   artifact(5, 5, ['v5'])], pinned=[2], tmp_path=tmp_path)
    assert result == {5: 'newest tagged 1', 4: 'newest tagged 2', 3: None, 2: 'pinned', 1: 'tagged latest'}


def test_latest_only_pushes_keep_just_latest(tmp_path):
    result = plan([artifact(1, 1), artifact(2, 2), artifact(3, 3, ['latest'])], tmp_path=tmp_path)
    assert result == {3: 'newest tagged 1', 2: None, 1: None}


def test_an_index_counts_once_and_a_pinned_child_keeps_it(tmp_path):
    artifacts = [artifact(10, 1, children=[11, 12]), artifact(11, 1), artifact(12, 1), artifact(30, 3, ['latest'])]
    assert plan(artifacts, keep=1, tmp_path=tmp_path) == {30: 'newest tagged 1', 10: None}
    assert plan(artifacts, keep=1, pinned=[12], tmp_path=tmp_path)[10] == 'pinned'


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


# --- node-disk-cleanup.sh on its own, with fake k3s, df and du ---------------------------------------------------------
@pytest.mark.parametrize('second_pass, removed, errors', [('Deleted: b\n', 2, 0), ('E1119 still DeadlineExceeded\n', 1, 1)])
def test_node_prune_goes_round_again_after_a_timeout(tmp_path, second_pass, removed, errors):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fakes = {
        'k3s': f'''#!/bin/sh
case "$*" in *"--timeout 10m rmi --prune"*) ;; *) echo "unexpected k3s $*" >&2; exit 1 ;; esac
if [ -e "{tmp_path}/pruned" ]; then printf '{second_pass}'; exit 0; fi
touch "{tmp_path}/pruned"; printf 'Deleted: a\\nE1119 RemoveImage failed: DeadlineExceeded\\n'
''',
        'df': '#!/bin/sh\nprintf "Used Avail\\n100 900\\n"\n',
        'du': '#!/bin/sh\nprintf "7\\ttotal\\n"\n',
        'journalctl': '#!/bin/sh\nexit 0\n',
        'apt-get': '#!/bin/sh\nexit 0\n',
    }
    for name, body in fakes.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    result = subprocess.run(['bash', str(LIB / 'node-disk-cleanup.sh'), 'apply', 'pvc-1'], capture_output=True,
                            text=True, env={'PATH': f'{bin_dir}:/usr/bin:/bin'})
    lines = result.stdout.splitlines()
    assert lines[:3] == ['disk 100 900', 'journal 7', 'apt 7']
    assert f'removed {removed}' in lines and 'disk_after 100 900' in lines
    assert len([line for line in lines if line.startswith('error crictl:')]) == errors


# --- the script end to end, against fakes --------------------------------------------------------------------------
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
state = os.environ['FAKE_STATE']
args = sys.argv[1:]
if args[:2] == ['config', 'current-context']:
    print('test'); sys.exit(0)
assert args[:2] == ['--context', 'test'], args
args = [a for a in args[2:] if not a.startswith('--request-timeout')]
if args == ['get', '--raw', '/readyz']:
    print('ok')
elif args[:5] == ['-n', 'harbor', 'get', 'secret', 'harbor-credentials']:
    print(base64.b64encode(b'pw').decode())
elif args[:4] == ['-n', 'harbor', 'get', 'pods']:
    print('node-b', end='')
elif args[:3] == ['-n', 'harbor', 'exec'] and args[-3:] == ['du', '-sk', '/storage']:
    gc_ran = os.path.exists(os.path.join(state, 'gc-started'))
    print(f"{(2 if gc_ran else 10) * 1024 * 1024}\t/storage")
elif args[0] == 'get' and args[1].startswith('pods,'):
    print(open(os.path.join(state, 'workloads.json')).read())
elif args[:2] == ['get', 'pv']:
    print('pvc-1')
elif args[:2] == ['get', 'nodes']:
    print(json.dumps({'items': [
        {'metadata': {'name': 'node-a'}, 'status': {'addresses': [{'type': 'ExternalIP', 'address': '10.0.0.1'}]}},
        {'metadata': {'name': 'node-b'}, 'status': {'addresses': [{'type': 'InternalIP', 'address': '10.0.0.2'}]}}]}))
else:
    print(f'unexpected kubectl {args}', file=sys.stderr); sys.exit(1)
'''

# Answers node-disk-cleanup.sh and the crictl calls the way a node would; NODES is keyed by IP.
FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys
args, script = sys.argv[1:], sys.stdin.read()
node = json.load(open(os.path.join(os.environ['FAKE_STATE'], 'nodes.json')))[args[-2].split('@')[1]]
if 'crictl images' in script:
    print(json.dumps({'images': node['images']}))
elif 'crictl ps' in script:
    print(json.dumps({'containers': node['containers']}))
else:
    mode = args[-1].split()[3]
    print(node['report'] + ('\n' + node['apply'] if mode == 'apply' else ''))
'''

NODES = {
    '10.0.0.1': {   # node-a: one unused 3 GiB image, journald 100 MiB over the limit, a folder without a PV
        'images': [{'id': 'sha256:a', 'repoTags': ['app:1'], 'size': str(3 * GiB)},
                   {'id': 'sha256:b', 'repoTags': ['app:2'], 'size': str(GiB)}],
        'containers': [{'imageRef': 'sha256:b', 'image': {'image': 'sha256:b'}}],
        'report': f'disk {50 * GiB} {25 * GiB}\njournal {300 * MiB}\napt {MiB}\n'
                  f'orphan {2 * GiB} /var/local-path-provisioner/pvc-old_ns_data',
        'apply': f'removed 1\njournal_after {200 * MiB}\napt_after 0\ndisk_after {30 * GiB} {45 * GiB}',
    },
    '10.0.0.2': {   # node-b: nothing unused
        'images': [], 'containers': [],
        'report': f'disk {10 * GiB} {60 * GiB}\njournal {100 * MiB}\napt 0',
        'apply': f'removed 0\njournal_after {100 * MiB}\napt_after 0\ndisk_after {9 * GiB} {61 * GiB}',
    },
}


@pytest.fixture
def cleanup(tmp_path):
    bin_dir, state = tmp_path / 'bin', tmp_path / 'state'
    bin_dir.mkdir(), state.mkdir()
    for name, body in {'curl': FAKE_CURL, 'kubectl': FAKE_KUBECTL, 'ssh': FAKE_SSH,
                       'date': '#!/bin/sh\necho 1200\n', 'sleep': '#!/bin/sh\nexit 0\n'}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (state / 'nodes.json').write_text(json.dumps(NODES))
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
    # A CronJob pins build 2 of seo-agent by digest; a pod that runs :latest started on build 3 (not protected).
    (state / 'workloads.json').write_text(json.dumps({'items': [
        {'kind': 'CronJob', 'spec': {'jobTemplate': {'spec': {'template': {'spec': {'containers': [
            {'image': f'registry.jithendar.com/jithendar/seo-agent@{digest(2)}'}]}}}}}},
        {'kind': 'Pod', 'status': {'containerStatuses': [
            {'image': 'registry.jithendar.com/jithendar/seo-agent:latest',
             'imageID': f'registry.jithendar.com/jithendar/seo-agent@{digest(3)}'}]}}]}))

    def run(*args, only='harbor', harbor_login='user = "admin:pw"\n', typed=None, **extra_env):
        """Harbor accepts only `harbor_login` (the Secret holds pw); `typed` is typed into a terminal on stdin."""
        env = {'PATH': f'{bin_dir}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin', 'FAKE_STATE': str(state),
               'HOME': str(tmp_path), 'FAKE_LOGIN': harbor_login, **extra_env}
        cmd = ['bash', str(ROOT / 'disk-cleanup.sh'), '--only', only, *args]
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


def summary(stdout):
    return stdout.split('== Summary', 1)[1].split('== Done', 1)[0]


def test_harbor_dry_run_only_reads(cleanup):
    result, calls = cleanup()
    assert result.returncode == 0, result.stderr
    assert {c['method'] for c in calls} == {'GET'}
    assert 'registry volume: 10.0 GiB (on node-b)' in result.stdout
    assert 'jithendar/seo-agent: 5 artifacts, 3 to delete' in result.stdout
    assert 'jithendar/team/app: 3 artifacts, 2 to delete' in result.stdout


def test_harbor_apply_deletes_untagged_builds_then_collects_garbage(cleanup):
    result, calls = cleanup('--apply', '--yes')
    assert result.returncode == 0, result.stderr
    changes = [(c['method'], c['path']) for c in calls if c['method'] != 'GET']
    agent, nested = '/projects/jithendar/repositories/seo-agent', '/projects/jithendar/repositories/team%252Fapp'
    assert changes == [
        ('DELETE', f'{agent}/artifacts/{digest(4)}'),        # 5 is latest, 2 is pinned; 4, 3 (running :latest), 1 go
        ('DELETE', f'{agent}/artifacts/{digest(3)}'),
        ('DELETE', f'{agent}/artifacts/{digest(1)}'),
        ('DELETE', f'{nested}/artifacts/{digest(8)}'),
        ('DELETE', f'{nested}/artifacts/{digest(7)}'),
        ('DELETE', '/projects/jithendar/repositories/empty'),
        ('POST', '/system/gc/schedule'),
    ]
    assert 'frees up 42 MB' in result.stdout
    assert 'registry volume: 10.0 GiB -> 2.0 GiB, freed 8.0 GiB' in result.stdout
    assert 'Harbor registry volume              10.0 GiB            2.0 GiB             8.0 GiB' in summary(result.stdout)
    # The login travels on stdin only.
    assert all(c['login'] == 'user = "admin:pw"\n' and not any('pw' in a for a in c['argv']) for c in calls)


def test_nodes_dry_run_shows_each_node_and_what_could_go(cleanup):
    result, _ = cleanup(only='nodes')
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert '  -- node-a (10.0.0.1)' in out and '  -- node-b (10.0.0.2)' in out
    assert 'disk          50.0 GiB used of 75.0 GiB, 25.0 GiB free (67%)' in out
    assert 'images        1 unused, 3.0 GiB compressed' in out and '3.0 GiB  app:1' in out
    assert 'no PersistentVolume  2.0 GiB  /var/local-path-provisioner/pvc-old_ns_data  (left in place)' in out
    assert 'could free    ~3.1 GiB (images 3.0 GiB, journald 100.0 MiB, apt cache 1.0 MiB)' in out
    table = summary(out)
    assert 'node-a                              50.0 GiB (67%)      ~3.1 GiB' in table
    assert 'node-b                              10.0 GiB (14%)      ~0.0 B' in table
    assert 'total                                                   ~3.1 GiB' in table


def test_apply_reports_each_node_and_harbor_before_and_after(cleanup):
    result, _ = cleanup('--apply', '--yes', only='nodes,harbor')
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.index('-- node-a') < out.index('== Harbor')          # nodes first: their figures leave out Harbor's GC
    assert 'removed       1 images; journald 300.0 MiB -> 200.0 MiB; apt cache 1.0 MiB -> 0.0 B' in out
    assert 'disk after    30.0 GiB used, 45.0 GiB free: freed 20.0 GiB' in out
    table = summary(out)
    assert 'node-a                              50.0 GiB (67%)      30.0 GiB (40%)      20.0 GiB' in table
    assert 'node-b                              10.0 GiB (14%)      9.0 GiB (13%)       1.0 GiB' in table
    assert 'Harbor registry volume              10.0 GiB            2.0 GiB             8.0 GiB' in table
    assert 'total freed                                                                 29.0 GiB' in table
    assert "Harbor's volume is on node-b" in table


def test_apply_without_a_terminal_needs_yes(cleanup):
    result, calls = cleanup('--apply')
    assert result.returncode == 1 and 'add --yes' in result.stderr
    assert calls == []


def test_a_refused_secret_without_a_terminal_stops_before_any_change(cleanup):
    result, calls = cleanup('--apply', '--yes', harbor_login='user = "admin:changed"\n')
    assert result.returncode == 1 and 'set HARBOR_PASSWORD' in result.stderr
    assert {c['method'] for c in calls} == {'GET'} and {c['path'] for c in calls} == {'/users/current'}


@pytest.mark.parametrize('password, login', [('changed', 'user = "admin:changed"\n'),
                                             ('a"b\\c', 'user = "admin:a\\"b\\\\c"\n')])
def test_harbor_password_overrides_the_secret_and_is_quoted_for_curl(cleanup, password, login):
    result, _ = cleanup(harbor_login=login, HARBOR_PASSWORD=password)
    assert result.returncode == 0, result.stderr


def test_a_refused_secret_asks_for_the_password_on_the_terminal(cleanup):
    result, calls = cleanup('--apply', '--yes', harbor_login='user = "admin:changed"\n', typed='changed\n')
    assert result.returncode == 0, result.stderr
    assert "refused admin's password from Secret harbor/harbor-credentials" in result.stdout
    assert ('POST', '/system/gc/schedule') in [(c['method'], c['path']) for c in calls]
    assert not any('changed' in a for c in calls for a in c['argv'])
