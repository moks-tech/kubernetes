"""GitHub Actions runners in Grafana: the Platform · GitHub runners and · GitHub runner pods dashboards, the metrics
Actions Runner Controller exports for them (controller.yaml), and Prometheus job arc that scrapes the controller and
the listener."""
from pathlib import Path
import json
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / 'deploy/minikube/observability/grafana/dashboards'
BOARDS = ['platform-github-runners.json', 'platform-github-runner-pods.json']
CONTROLLER = yaml.safe_load((ROOT / 'deploy/minikube/runners/controller.yaml').read_text())
SCALE_SET = yaml.safe_load((ROOT / 'deploy/minikube/runners/values.yaml').read_text())
README = (ROOT / 'deploy/minikube/runners/README.md').read_text()

# What ARC 0.15.1 exports: the listener (cmd/ghalistener/metrics, every one of them with listenerMetrics unset) and the
# controller (controllers/actions.github.com/metrics).
LISTENER_METRICS = {
    'gha_assigned_jobs', 'gha_running_jobs', 'gha_registered_runners', 'gha_busy_runners', 'gha_min_runners',
    'gha_max_runners', 'gha_desired_runners', 'gha_idle_runners', 'gha_started_jobs_total',
    'gha_completed_jobs_total', 'gha_job_startup_duration_seconds', 'gha_job_execution_duration_seconds'}
CONTROLLER_METRICS = {
    f'gha_controller_{m}' for m in ('pending_ephemeral_runners', 'running_ephemeral_runners',
                                    'succeeded_ephemeral_runners', 'failed_ephemeral_runners',
                                    'outdated_ephemeral_runners', 'running_listeners')}
JOB_LABELS = {'repository', 'organization', 'enterprise', 'job_name', 'event_name', 'job_result', 'le'}


def scrape_jobs():
    out = subprocess.check_output(['helm', 'template', 'prometheus', str(ROOT / 'deploy/minikube/observability/prometheus'),
                                   '-n', 'monitoring', '-s', 'templates/config.yaml'], text=True)
    config = yaml.safe_load(yaml.safe_load(out)['data']['prometheus.yml'])
    return {job['job_name']: job for job in config['scrape_configs']}


def board(name):
    return json.loads((DASHBOARDS / name).read_text())


def exprs(b):
    return [t['expr'] for p in b['panels'] for t in p.get('targets', [])]


def arc_metrics_read():
    """Metric names (histograms without _bucket/_sum/_count) read from job arc by either dashboard."""
    read = set()
    for name in BOARDS:
        for expr in exprs(board(name)) + [v['query']['query'] for v in board(name)['templating']['list']]:
            if 'job="arc"' in expr:
                read |= {re.sub(r'_(bucket|sum|count)$', '', m) for m in re.findall(r'\b([a-z_]+)\{job="arc"', expr)}
    return read


@pytest.mark.parametrize('name', BOARDS)
def test_dashboards_are_platform_dashboards_on_the_provisioned_datasource(name):
    b = board(name)
    assert b['uid'] == name[:-5] and {'platform', 'github-runners'} <= set(b['tags'])
    targets = [t for p in b['panels'] for t in p.get('targets', [])]
    assert targets and all(t['datasource'] == {'type': 'prometheus', 'uid': 'roboshop-prometheus'} for t in targets)
    assert all(v['datasource']['uid'] == 'roboshop-prometheus' for v in b['templating']['list'])
    # Every query names its job: never another cluster's remote-written series.
    assert all(re.search(r'job="(arc|kubernetes-cadvisor)"', e) for e in exprs(b)), name
    ids = [p['id'] for p in b['panels']]
    assert len(ids) == len(set(ids))
    configmap = (DASHBOARDS / 'templates/grafana-dashboards-platform-configmap.yaml').read_text()
    assert '"platform-*.json"' in configmap


@pytest.mark.parametrize('name', BOARDS)
def test_panels_do_not_overlap(name):
    taken = set()
    for p in board(name)['panels']:
        g = p['gridPos']
        assert g['x'] + g['w'] <= 24, p['title']
        cells = {(x, y) for x in range(g['x'], g['x'] + g['w']) for y in range(g['y'], g['y'] + g['h'])}
        assert not cells & taken, p['title']
        taken |= cells


@pytest.mark.parametrize('name', BOARDS)
def test_variables_are_used_and_quantiles_keep_le(name):
    b = board(name)
    text = '\n'.join(exprs(b))
    for v in b['templating']['list']:
        assert f'${v["name"]}' in text, v['name']
    for expr in exprs(b):
        for by in re.findall(r'histogram_quantile\([0-9.]+, sum by \(([^)]*)\) \(increase\([a-z_]+_bucket\{', expr):
            assert 'le' in [label.strip() for label in by.split(',')], expr
        assert expr.count('histogram_quantile(') == expr.count('_bucket{'), expr


def test_the_dashboards_read_only_what_arc_exports():
    read = arc_metrics_read()
    assert {'gha_running_jobs', 'gha_completed_jobs_total', 'gha_job_startup_duration_seconds',
            'gha_controller_pending_ephemeral_runners'} <= read
    assert read - {'up'} <= LISTENER_METRICS | CONTROLLER_METRICS | {
        'controller_runtime_reconcile_total', 'controller_runtime_reconcile_errors_total', 'workqueue_depth'}
    # The job metrics' labels used in by () clauses and selectors exist with listenerMetrics unset (ARC's defaults).
    assert 'listenerMetrics' not in SCALE_SET
    jobs = board('platform-github-runners.json')
    for expr in exprs(jobs):
        for by in re.findall(r'by \(([^)]*)\) \((?:increase|rate)\(gha_(?:completed|started|job)_', expr):
            assert {label.strip() for label in by.split(',')} <= JOB_LABELS, expr


def test_every_arc_metric_read_survives_the_scrape_filter():
    job = scrape_jobs()['arc']
    (keep,) = job['metric_relabel_configs']
    assert keep['source_labels'] == ['__name__'] and keep['action'] == 'keep'
    names = set()
    for m in arc_metrics_read() - {'up'}:
        names |= {f'{m}_{s}' for s in ('bucket', 'sum', 'count')} if m.endswith('_seconds') else {m}
    assert {n for n in names if not re.fullmatch(keep['regex'], n)} == set()


def test_arc_exports_metrics_where_job_arc_looks():
    metrics = CONTROLLER['metrics']
    assert metrics['controllerManagerAddr'] != '0' and metrics['listenerAddr']
    job = scrape_jobs()['arc']
    assert job['metrics_path'] == metrics['listenerEndpoint'] == '/metrics'   # the controller's is always /metrics
    assert job['kubernetes_sd_configs'] == [{'role': 'pod', 'namespaces': {'names': ['runners-system']}}]
    keep = job['relabel_configs'][0]
    assert keep == {'source_labels': ['__meta_kubernetes_pod_container_port_name'], 'regex': 'metrics',
                    'action': 'keep'}
    components = {r['replacement'] for r in job['relabel_configs'] if r.get('target_label') == 'component'}
    assert components == {'controller', 'listener'}
    # The listener's series carry the scale set's namespace (runners); a target label would rename it.
    assert 'namespace' not in {r.get('target_label') for r in job['relabel_configs']}


def test_the_readme_names_both_dashboards():
    assert '## Metrics and dashboards' in README
    for name in BOARDS:
        assert board(name)['title'] in README
