"""bge-m3 monitoring: the Platform dashboard reads only bge-m3's series, counts a caller's first request, its alerts
have runbooks, and Prometheus can read the sidecars' own metrics (envoy-stats) instead of its own sidecar's."""
from pathlib import Path
import json
import re
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
BOARD = json.loads((ROOT / 'deploy/minikube/observability/grafana/dashboards/platform-embeddings.json').read_text())
ALERTS = yaml.safe_load((ROOT / 'deploy/minikube/observability/prometheus/files/alerts.yml').read_text())
README = (ROOT / 'deploy/minikube/embeddings/bge-m3/README.md').read_text()
EXPRS = [t['expr'] for p in BOARD['panels'] for t in p.get('targets', [])]
SELECTOR = re.compile(r'(istio_\w+)\{([^}]*)\}')
UNDELIVERED = 'response_flags=~".*(UH|UF|UO|NR).*"'


def flat(expr):
    return ' '.join(expr.split())


def counts_new_series(expr, window):
    """Every Istio counter in expr is counted with increase() plus the starting value of a series that appears inside
    the window, which increase() alone reads as 0."""
    expr = flat(expr)
    for name, labels in set(SELECTOR.findall(expr)):
        series = f'{name}{{{labels}}}'
        assert f'increase({series}[{window}])' in expr, series
        assert f'min_over_time({series}[{window}]) unless {series} offset {window}' in expr, series
    return True


def test_dashboard_is_a_platform_dashboard_on_the_provisioned_datasource():
    assert BOARD['uid'] == 'platform-embeddings' and 'platform' in BOARD['tags']
    targets = [t for p in BOARD['panels'] for t in p.get('targets', [])]
    assert targets and all(t['datasource'] == {'type': 'prometheus', 'uid': 'roboshop-prometheus'} for t in targets)
    ids = [p['id'] for p in BOARD['panels']]
    assert len(ids) == len(set(ids))
    # The platform ConfigMap globs platform-*.json, so the file name is what puts it in folder Platform.
    configmap = (ROOT / 'deploy/minikube/observability/grafana/dashboards/templates/'
                 'grafana-dashboards-platform-configmap.yaml').read_text()
    assert '"platform-*.json"' in configmap


def test_every_query_selects_bge_m3_only():
    for expr in EXPRS:
        if 'istio_' in expr:
            for _, labels in SELECTOR.findall(expr):
                assert 'destination_service_name="bge-m3"' in labels, labels
                assert 'destination_service_namespace="embeddings"' in labels and 'job="envoy-stats"' in labels, labels
                # bge-m3's own sidecar, whose counters outlive caller restarts; the callers' sidecars only for
                # requests that never reached the pod.
                assert 'reporter="destination"' in labels or ('reporter="source"' in labels and UNDELIVERED in labels), labels
        elif 'kubelet_volume_stats' in expr:
            assert 'persistentvolumeclaim="bge-m3-models"' in expr and 'namespace="embeddings"' in expr, expr
        else:
            assert 'container_' in expr and 'namespace="embeddings"' in expr and 'pod=~"bge-m3-.*"' in expr, expr


def test_stats_count_a_callers_first_request():
    stats = {p['title']: p['targets'][0]['expr'] for p in BOARD['panels']
             if p['type'] == 'stat' and 'istio_' in p['targets'][0]['expr']}
    assert set(stats) == {'Requests', 'Failed', 'p95 latency'}
    assert all(counts_new_series(expr, '$__range') for expr in stats.values())
    # Requests and Failed include the requests that never reached bge-m3.
    assert UNDELIVERED in stats['Requests'] and UNDELIVERED in stats['Failed']
    # No failures is 0 %, not "No requests".
    assert stats['Failed'].startswith('(sum(') and ') or vector(0)) / sum(' in stats['Failed']


def test_responses_show_requests_that_never_reached_the_pod():
    panel = next(p for p in BOARD['panels'] if p['title'] == 'Responses by code')
    undelivered = [t for t in panel['targets'] if UNDELIVERED in t['expr']]
    assert len(undelivered) == 1 and undelivered[0]['legendFormat'].startswith('not delivered')


def test_failing_alert_counts_new_codes_and_undelivered_requests():
    group = next(g for g in ALERTS['groups'] if g['name'] == 'embeddings')
    expr = next(r['expr'] for r in group['rules'] if r['alert'] == 'BgeM3RequestsFailing')
    assert counts_new_series(expr, '15m')
    assert 'response_code!~"2.."' in expr and UNDELIVERED in expr
    assert flat(expr).endswith(') >= 3')


def test_vector_estimate_matches_the_documented_size():
    estimate = next(e for e in EXPRS if 'istio_response_bytes_sum' in e)
    assert estimate.endswith('/ 12800') and '12.8 KB' in README


def test_alerts_have_runbooks_in_the_readme():
    group = next(g for g in ALERTS['groups'] if g['name'] == 'embeddings')
    names = [r['alert'] for r in group['rules']]
    assert names == ['BgeM3NotRunning', 'BgeM3ModelNotLoaded', 'BgeM3RequestsFailing', 'BgeM3MemoryNearLimit']
    for rule in group['rules']:
        assert rule['annotations']['runbook_url'].endswith('deploy/minikube/embeddings/bge-m3/README.md#monitoring')
        assert f'**{rule["alert"]}**' in README
    assert re.search(r'^## Monitoring$', README, re.M)


def test_prometheus_reaches_the_sidecar_ports_past_its_own_envoy():
    out = subprocess.check_output(['helm', 'template', 'prometheus', str(ROOT / 'deploy/minikube/observability/prometheus'),
                                   '-n', 'monitoring'], text=True)
    deploy = next(d for d in yaml.safe_load_all(out) if d and d['kind'] == 'Deployment')
    annotations = deploy['spec']['template']['metadata']['annotations']
    assert set(annotations['traffic.sidecar.istio.io/excludeOutboundPorts'].split(',')) == {'15020', '15090'}
    # Only those ports: everything else still goes out through the sidecar, as mTLS to STRICT targets.
    assert 'traffic.sidecar.istio.io/includeOutboundIPRanges' not in annotations


def test_kiali_sidecar_is_scrapable():
    tf = (ROOT / 'minikube-infrastructure/kiali.tf').read_text()
    policy = tf[tf.index('resource "kubernetes_network_policy_v1" "kiali_sidecar_metrics"'):]
    assert '"kubernetes.io/metadata.name" = "monitoring"' in policy
    assert 'port     = "15090"' in policy and 'port     = "15020"' in policy
