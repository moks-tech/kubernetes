"""bge-m3 monitoring: the Platform dashboard reads only bge-m3's series, its alerts have runbooks, and Prometheus can
read the sidecars' own metrics (envoy-stats) instead of its own sidecar's."""
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
            # Caller-side: counts requests that never reached the pod too.
            assert 'reporter="source"' in expr and 'destination_service_name="bge-m3"' in expr, expr
            assert 'destination_service_namespace="embeddings"' in expr and 'job="envoy-stats"' in expr, expr
        elif 'kubelet_volume_stats' in expr:
            assert 'persistentvolumeclaim="bge-m3-models"' in expr and 'namespace="embeddings"' in expr, expr
        else:
            assert 'container_' in expr and 'namespace="embeddings"' in expr and 'pod=~"bge-m3-.*"' in expr, expr


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
