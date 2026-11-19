# Kiali at https://jithendar.com/kiali/ (token sign-in, view only). Values: deploy/minikube/istio/kiali.yaml.

# Signs Kiali's login cookies; Kiali accepts 16, 24 or 32 characters. Kept in the (encrypted) state so every apply
# reuses it.
resource "random_password" "kiali_signing_key" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "kiali_signing_key" {
  metadata {
    name      = "kiali-signing-key"
    namespace = "istio-system"
  }
  data = {
    key = random_password.kiali_signing_key.result
  }

  # istio-base creates the namespace.
  depends_on = [helm_release.istio_base]
}

resource "helm_release" "kiali" {
  name       = "kiali"
  namespace  = "istio-system"
  repository = "https://kiali.org/helm-charts"
  chart      = "kiali-server"
  version    = "2.34.0"
  values     = [file("${local.minikube}/istio/kiali.yaml")]

  wait    = true
  timeout = var.helm_timeout

  depends_on = [helm_release.istiod, helm_release.prometheus, kubernetes_secret_v1.kiali_signing_key]
}

# Kiali's sidecar metrics (envoy-stats job). The chart's own NetworkPolicy admits only Kiali's ports (20001, 9090);
# policies add up, so this one lets Prometheus reach the sidecar's 15020 and 15090 as well, like the istio-sidecars
# policies in the application namespaces.
resource "kubernetes_network_policy_v1" "kiali_sidecar_metrics" {
  metadata {
    name      = "kiali-sidecar-metrics"
    namespace = "istio-system"
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "kiali"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "monitoring"
          }
        }
      }
      ports {
        port     = "15020"
        protocol = "TCP"
      }
      ports {
        port     = "15090"
        protocol = "TCP"
      }
    }
  }

  depends_on = [helm_release.kiali]
}
