# GitHub Actions self-hosted runners for the GitHub organization in var.github_runners_config_url (mrjithendar):
# Actions Runner Controller with one runner scale set (deploy/minikube/runners/README.md). Workflows opt in with
# `runs-on: mk4`.
#   runners-system  release arc: the controller, and the scale set's listener (long-polls GitHub for queued jobs)
#   runners         release arc-runners: scale set mk4, one ephemeral pod per job, deleted when the job ends
# Two namespaces, as GitHub recommends: the controller's ServiceAccount can manage pods, Secrets and RBAC wherever a
# scale set lives, so it never shares a namespace with the code a job runs.
#
# The GitHub App's credentials: var.github_runners_app_* (CI: secrets RUNNERS_GITHUB_APP_ID,
# RUNNERS_GITHUB_APP_INSTALLATION_ID and RUNNERS_GITHUB_APP_PRIVATE_KEY of environment mk4; locally: export
# TF_VAR_github_runners_app_id, TF_VAR_github_runners_app_installation_id, TF_VAR_github_runners_app_private_key).

locals {
  # Controller and scale set charts must be on the same version. Helm never upgrades the controller's CRDs: a new
  # version is an uninstall and reinstall (deploy/minikube/runners/README.md, "Upgrading ARC").
  arc_chart_version     = "0.15.1"
  arc_chart_repository  = "oci://ghcr.io/actions/actions-runner-controller-charts"
  runners_scale_set     = "mk4"                   # the runs-on label
  runners_controller_sa = "arc-gha-rs-controller" # chart gha-runner-scale-set-controller: <release>-gha-rs-controller
}

# Not in the mesh: nothing here serves or calls anything on mk4. Pod Security baseline on top of Gatekeeper.
resource "kubernetes_namespace_v1" "runners_system" {
  metadata {
    name = "runners-system"
    labels = {
      "app.kubernetes.io/part-of"          = "github-runners"
      "pod-security.kubernetes.io/enforce" = "baseline"
    }
  }
}

resource "kubernetes_namespace_v1" "runners" {
  metadata {
    name = "runners"
    labels = {
      "app.kubernetes.io/part-of"          = "github-runners"
      "pod-security.kubernetes.io/enforce" = "baseline"
    }
  }
}

# The scale set's githubConfigSecret. Runner pods run as ServiceAccount mk4-gha-rs-no-permission, which
# cannot read it.
resource "kubernetes_secret_v1" "runners_github_app" {
  metadata {
    name      = "github-app"
    namespace = kubernetes_namespace_v1.runners.metadata[0].name
    labels = {
      "app.kubernetes.io/part-of"    = "github-runners"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
  data = {
    github_app_id              = var.github_runners_app_id
    github_app_installation_id = var.github_runners_app_installation_id
    github_app_private_key     = var.github_runners_app_private_key
  }

  lifecycle {
    precondition {
      condition     = var.github_runners_app_id != "" && var.github_runners_app_installation_id != "" && nonsensitive(var.github_runners_app_private_key != "")
      error_message = "The runners' GitHub App is not configured: set secrets RUNNERS_GITHUB_APP_ID, RUNNERS_GITHUB_APP_INSTALLATION_ID and RUNNERS_GITHUB_APP_PRIVATE_KEY of environment mk4 (locally: export TF_VAR_github_runners_app_*). deploy/minikube/runners/README.md, \"Credentials\"."
    }
  }
}

# Jobs run whatever their workflow (and every action and package it pulls) says, so runner pods get the internet and
# nothing else: DNS through CoreDNS, then public addresses only, any port (git, apt over http, registries...). No pod,
# Service or node on mk4, and not the Hetzner metadata service (169.254.169.254). Nothing may connect to them.
# The same address ranges as the bge-m3 chart's download rule.
resource "kubernetes_network_policy_v1" "runners" {
  metadata {
    name      = "runners"
    namespace = kubernetes_namespace_v1.runners.metadata[0].name
    labels = {
      "app.kubernetes.io/part-of" = "github-runners"
    }
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_labels = { "k8s-app" = "kube-dns" }
        }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    egress {
      to {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]
        }
      }
    }
  }
}

resource "helm_release" "arc_controller" {
  name       = "arc"
  namespace  = kubernetes_namespace_v1.runners_system.metadata[0].name
  repository = local.arc_chart_repository
  chart      = "gha-runner-scale-set-controller"
  version    = local.arc_chart_version
  values     = [file("${local.minikube}/runners/controller.yaml")]

  wait    = true
  timeout = var.helm_timeout
}

# Helm waits for nothing here (the chart renders an AutoscalingRunnerSet, not pods): the deploy workflow checks the
# listener pod afterwards, which only starts once GitHub has accepted the app's credentials.
resource "helm_release" "arc_runners" {
  name       = "arc-runners"
  namespace  = kubernetes_namespace_v1.runners.metadata[0].name
  repository = local.arc_chart_repository
  chart      = "gha-runner-scale-set"
  version    = local.arc_chart_version
  values = [
    file("${local.minikube}/runners/values.yaml"),
    yamlencode({
      githubConfigUrl    = var.github_runners_config_url
      githubConfigSecret = kubernetes_secret_v1.runners_github_app.metadata[0].name
      runnerScaleSetName = local.runners_scale_set
      # Named, so the chart does not search the cluster for the controller while rendering.
      controllerServiceAccount = {
        namespace = helm_release.arc_controller.namespace
        name      = local.runners_controller_sa
      }
    }),
  ]

  wait    = true
  timeout = var.helm_timeout

  # The AutoscalingRunnerSet CRD comes with the controller; no runner pod starts before its NetworkPolicy.
  depends_on = [helm_release.arc_controller, kubernetes_network_policy_v1.runners]
}
