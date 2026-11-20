"""MOKS in Prometheus and Grafana: the pod-discovery scrape job (which process each series came from), alerts that fire once
rather than once per pod, and the MOKS dashboards (copies of moks-sre's deploy/grafana/dashboards, built there)."""
from pathlib import Path
import json
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / 'deploy/minikube/observability/grafana/dashboards'
ALERTS = yaml.safe_load((ROOT / 'deploy/minikube/observability/prometheus/files/alerts.yml').read_text())
BOARDS = sorted(p.name for p in DASHBOARDS.glob('moks-*.json'))


def moks_job():
    out = subprocess.check_output(['helm', 'template', 'prometheus', str(ROOT / 'deploy/minikube/observability/prometheus'),
                                   '-n', 'monitoring', '-s', 'templates/config.yaml'], text=True)
    config = yaml.safe_load(yaml.safe_load(out)['data']['prometheus.yml'])
    return next(job for job in config['scrape_configs'] if job['job_name'] == 'moks')


def relabel(rules, meta):
    """Prometheus's relabelling for the actions this job uses (keep, drop, replace), on one discovered target."""
    labels = dict(meta)
    for r in rules:
        value = ';'.join(labels.get(s, '') for s in r.get('source_labels', []))
        m = re.fullmatch(r.get('regex', '(.*)'), value)
        action = r.get('action', 'replace')
        if action == 'keep' and not m or action == 'drop' and m:
            return None
        if action == 'replace' and m:
            labels[r['target_label']] = m.expand(r.get('replacement', '$1').replace('$', '\\'))
    return {k: v for k, v in labels.items() if not k.startswith('__')}


def pod(app, port, component='', name='moks-x-1', phase='Running'):
    return {'__meta_kubernetes_pod_label_app': app, '__meta_kubernetes_pod_label_component': component, '__meta_kubernetes_pod_name': name,
            '__meta_kubernetes_namespace': 'moks-sre', '__meta_kubernetes_pod_container_port_number': str(port), '__meta_kubernetes_pod_phase': phase}


def test_every_moks_process_is_found_by_pod_and_named():
    job = moks_job()
    assert job['kubernetes_sd_configs'] == [{'role': 'pod', 'namespaces': {'names': ['moks-sre']}}]
    rules = job['relabel_configs']
    assert relabel(rules, pod('moks', 9464, 'crewai', 'moks-crewai-5c8-x')) == {'instance': 'moks-crewai-5c8-x', 'pod': 'moks-crewai-5c8-x', 'namespace': 'moks-sre',
                                                                               'component': 'crewai', 'service': 'moks'}
    assert relabel(rules, pod('moks-reranker', 9464))['component'] == 'reranker'
    assert relabel(rules, pod('moks-embedder', 9464))['component'] == 'embedder'
    # Only the metrics port of MOKS's own pods: not the API port, not session pods, not a pod that has not started.
    assert relabel(rules, pod('moks', 8080, 'api')) is None
    assert relabel(rules, pod('moks-terminal', 9464)) is None and relabel(rules, pod('moks-diagnostic', 9464)) is None
    assert relabel(rules, pod('moks', 9464, 'worker', phase='Pending')) is None


def test_worker_only_gauges_alert_once_not_once_per_pod():
    rules = {r['alert']: r['expr'] for g in ALERTS['groups'] if g['name'].startswith('moks') for r in g['rules']}
    for name in ('MoksBackupOverdue', 'MoksBackupFailed', 'MoksDatabaseVolumeFilling'):
        assert re.search(r'\bmax\(moks_(backup|database)_', rules[name]), name
    assert {'MoksMLModelInvalid', 'MoksMLChampionNeedsReview', 'MoksMLContextUnreadable'} <= set(rules)


@pytest.mark.parametrize('name', BOARDS)
def test_moks_dashboards_read_the_moks_job(name):
    board = json.loads((DASHBOARDS / name).read_text())
    assert board['uid'] == name[:-5] and 'moks' in board['tags'] and board['title'].startswith('MOKS · ')
    exprs = [t['expr'] for p in board['panels'] for t in p.get('targets', []) if t['datasource']['type'] == 'prometheus']
    # Every query names its job: never another cluster's remote-written series.
    assert exprs and all('job="moks"' in e or 'job="otel-collector"' in e or e.startswith('count(ALERTS{') for e in exprs), name
    ids = [p['id'] for p in board['panels']]
    assert len(ids) == len(set(ids))


def test_the_moks_folder_has_the_six_dashboards():
    assert BOARDS == ['moks-crewai.json', 'moks-knowledge.json', 'moks-llm.json', 'moks-ml.json', 'moks-overview.json', 'moks-platform.json']
    assert '"moks-*.json"' in (DASHBOARDS / 'templates/grafana-dashboards-moks-configmap.yaml').read_text()
