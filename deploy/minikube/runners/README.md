# GitHub Actions runners on mk4

Self-hosted runners for every repository in the GitHub organization **mrjithendar**, run by GitHub's
[Actions Runner Controller](https://github.com/actions/actions-runner-controller) (ARC, runner scale sets).
Installed by Terraform: [`minikube-infrastructure/runners.tf`](../../../minikube-infrastructure/runners.tf).

| Namespace | Release | Chart (0.15.1) | Runs |
|---|---|---|---|
| `runners-system` | `arc` | `gha-runner-scale-set-controller` | The controller, and one listener pod per scale set that long-polls GitHub for queued jobs |
| `runners` | `arc-runners` | `gha-runner-scale-set` | Scale set **`mk4`**: one ephemeral runner pod per job, deleted when the job ends |

The controller has its own namespace, as GitHub recommends: its ServiceAccount can manage pods, Secrets and RBAC in
every namespace with a scale set, so it never shares one with the code a job runs.

## Using it

```yaml
jobs:
  test:
    runs-on: mk4        # the scale set's name; no "self-hosted" label
```

- **arm64.** Jobs run on mk4's arm64 workers (Hetzner CAX), like `ubuntu-24.04-arm`, not `ubuntu-latest` (x64).
- **The image** is `ghcr.io/mrjithendar/mk4-runner` (below): GitHub's runner image plus the tools the organization's
  workflows call without installing them. Languages come from the `setup-*` actions (`setup-node`, `setup-python`,
  `setup-go`, `setup-terraform`...), as on GitHub-hosted runners; anything else: `sudo apt-get install`.
- **Docker.** Every runner pod has its own Docker daemon (below), so `docker build`/`run`, `docker/*` actions,
  `helm/kind-action`, container actions (e.g. `bridgecrewio/checkov-action`), `container:` and `services:` work. Each
  job starts with an empty image cache.
- **Internet only.** NetworkPolicy `runners/runners` lets runner pods resolve names through CoreDNS and reach
  public addresses only, on any port. They cannot reach anything on mk4's private networks (pods, services, nodes)
  or the Hetzner metadata service, and no Service is exposed to them.
- **Public repositories.** The organization's `Default` runner group serves public repositories only while
  *Allow public repositories* is ticked (Settings → Actions → Runner groups). With it ticked, a pull request from a
  fork of a public repository runs its own workflow file on mk4, with a privileged Docker daemon (below).

## Docker

Each runner pod runs `docker:29.9.2-dind` as a native sidecar (`values.yaml`), with the runner's workspace, `/tmp`
and `/var/run` (the socket) shared, so `docker run -v <path>` sees the job's files. Its images and containers live in
an `emptyDir` on the node's disk and disappear with the pod.

The Docker daemon must be a **privileged** container. mk4 allows that in namespace `runners` only: Gatekeeper's
`privilegedContainers` constraint excludes it (`gatekeeper/constraints/values.yaml`) and its Pod Security level is
`privileged` (`runners.tf`). Host networking, host namespaces and `hostPath` volumes stay refused there.

**What that means:** a privileged container is root on its node. Any job (its workflow, or any action, package or
image it pulls) can read the node's disk, including other pods' volumes and Secrets, and the k3s credentials, and
from there take over the cluster. The NetworkPolicy does not stop that. It is only as safe as everything those jobs
run. Keep *Allow public repositories* off unless every public repository's fork pull requests need approval before
they run (Settings → Actions → General → *Fork pull request workflows*).

## The image

[`image/Dockerfile`](image/Dockerfile), built on a GitHub-hosted arm64 runner by
[`build-runner-image.yml`](../../../.github/workflows/build-runner-image.yml) (not on mk4, so a broken runner image can
still be rebuilt) and pushed to `ghcr.io/mrjithendar/mk4-runner` on `master`. Pull requests build and smoke-test it without pushing.

From `ghcr.io/actions/actions-runner` (Ubuntu 24.04: `git`, `curl`, `jq`, `unzip`, `sudo`, the `docker` CLI), plus:

| Added | Because (workflows that call it directly) |
|---|---|
| `git-lfs` | `actions/checkout` with `lfs: true` (jithendar.com `content.yml`) |
| `python3`, `python3-dev`, `python3-pip`, `python3-venv`; `/etc/pip.conf` `break-system-packages = true` | `python3`/`pip install` without `setup-python` (jithendar.com, moks-cms); the shared libraries `setup-python`'s builds load. pip set up as on GitHub's Ubuntu images |
| `build-essential` | native npm/pip modules; roboshop-ai-app `currency-service` |
| `libgomp1` | LightGBM (moks-sre-agent) |
| `shellcheck` | jithendar.com, k8s-deployments, moks-cms, moks-seo-agent, practice-programming |
| `openssh-client` | `disk-cleanup.yml` |
| `zstd` | `actions/cache` (moks-cms) compresses with it, as on GitHub's runners |
| `gh` 2.105.0 | `gh api`/`gh pr` (moks-cms and others) |
| `kubectl` v1.37.2 | k8s-deployments' deploy and disk-cleanup workflows (no `setup-kubectl`) |
| `helm` v4.3.2 | k8s-deployments' check and deploy workflows (no `setup-helm`) |

`gh`, `kubectl` and `helm` are release binaries checked against SHA-256 sums pinned in the Dockerfile.

`/home/runner` is mode 755 (writable by the runner user only), as on GitHub's Ubuntu runners. The base image makes it
777, and platformdirs refuses to create `~/.cache/<app>` under a directory other users can write: pip-audit (moks-seo-agent
`ci.yml`) failed with `PermissionError: /home/runner, writable by another user`. The smoke test checks both.

**Changing it:** edit the Dockerfile, then set `image:` in `values.yaml` to the tag the workflow names
(`<runner version>-<first 12 hex of the Dockerfile's SHA-256>`; it fails the change until they match). After the push
to `master` has built the image, apply Terraform to roll it out.

## Limits

`deploy/minikube/runners/values.yaml` sets no `minRunners` (no runner waits idle) and no `maxRunners` (one pod per
queued job, without bound). What bounds CI is each pod's CPU and memory requests:

| Container | Requests | Limits |
| --- | --- | --- |
| `runner` (the job's steps) | 500m CPU, 1Gi memory | 3Gi memory |
| `dind` (the job's Docker containers) | 100m CPU, 256Mi memory | none |

The scheduler places a runner pod only on a worker with that much unreserved, so pods spread over the workers, and a
job waits (pod Pending, job queued) while none has room. The workers also run every application on mk4. A step that
goes past 3Gi is OOMKilled in its pod (exit 137) instead of starving the node. Docker jobs have no memory limit, since
dind's containers share its cgroup.

## Credentials: a GitHub App (once)

ARC signs in to GitHub as a GitHub App owned by the organization:

1. <https://github.com/organizations/mrjithendar/settings/apps/new>: any name; Homepage URL
   `https://github.com/actions/actions-runner-controller`; untick **Webhook → Active**; permissions **Organization →
   Self-hosted runners: Read and write** (Repository → Metadata: Read-only is added by itself); **Only on this
   account**. Create it.
2. On the app's page, note the **App ID**, then **Generate a private key** (a `.pem` file is downloaded).
3. **Install App** → mrjithendar → *All repositories* (or the ones that will use `mk4`). The page you land on ends in
   `/installations/<installation ID>`.
4. Store all three as secrets of the environment `mk4` of this repository, where only `master` can read them:

   ```bash
   R=mrjithendar/k8s-deployments
   gh secret set RUNNERS_GITHUB_APP_ID              --repo $R --env mk4 --body '<App ID>'
   gh secret set RUNNERS_GITHUB_APP_INSTALLATION_ID --repo $R --env mk4 --body '<installation ID>'
   gh secret set RUNNERS_GITHUB_APP_PRIVATE_KEY     --repo $R --env mk4 < ~/Downloads/<app>.private-key.pem
   rm ~/Downloads/<app>.private-key.pem
   ```

`create-mk-infrastructure.yml` passes them to Terraform (`TF_VAR_github_runners_app_*`), which writes Secret
`runners/github-app`. The plan fails until all three are set. For a local apply, export
`TF_VAR_github_runners_app_id`, `TF_VAR_github_runners_app_installation_id` and
`TF_VAR_github_runners_app_private_key`.

**Rotating the key:** generate a new key on the app's page, replace `RUNNERS_GITHUB_APP_PRIVATE_KEY`, run
*Create minikube infrastructure* by hand, then delete the old key on the app's page.

## Checks

```bash
kubectl -n runners-system get pods                    # arc-gha-rs-controller-…, and mk4-…-listener Running
kubectl -n runners get autoscalingrunnersets,ephemeralrunners,pods
kubectl -n runners-system logs deploy/arc-gha-rs-controller | grep -i error
```

The scale set appears under the organization's Settings → Actions → Runners as `mk4`. No listener pod usually means
GitHub refused the app's credentials: the controller's log says why. *Create minikube infrastructure* waits for the
listener after each apply and fails if it is not running within 3 minutes.

## Metrics and dashboards

Grafana (<https://jithendar.com/grafana/>), folder **Platform**:

| Dashboard | Shows | From |
| --- | --- | --- |
| **Platform · GitHub runners** | Listener and controller up; jobs running and waiting for a runner; runners busy, idle and the maximum; jobs finished, failed and cancelled; how long jobs wait for a runner and how long they run (p50, p95), per repository and per job; runner time used; ARC's reconciles and errors | Prometheus job `arc` |
| **Platform · GitHub runner pods** | Each runner pod's CPU and memory (runner and dind) against the runner container's limit, and a per-pod table of peaks for sizing the requests; each worker's memory, CPU, memory pressure and OOM kills | cAdvisor (job `kubernetes-cadvisor`) |

- **Turning metrics on:** `metrics:` in [`controller.yaml`](controller.yaml) makes the controller and every listener it
  starts serve `:8080/metrics` (container port `metrics`). Without it the chart passes `--metrics-addr=0` and nothing
  is exported. Prometheus job `arc` (`observability/prometheus/values.yaml`) finds both pods in `runners-system`.
- **Labels:** `listenerMetrics` is unset in `values.yaml`, so the listener exports ARC's defaults: repository, job
  name, event and result, but not the workflow ref (that would add one series per branch and pull request). Each
  repository, job and event seen adds a counter and two histograms of about 50 buckets. To trim them, set
  `listenerMetrics` there. A set block replaces the defaults, so it must list every metric the dashboard reads
  (`tests/test_runners_monitoring.py`).
- **Counting:** a job name's first run after the listener starts is not counted, since Prometheus needs an earlier
  sample to see an increase. A listener restart (ARC upgrade, values change, node drain) starts the counters again.
  *Wait for a runner* runs from GitHub assigning the job to `mk4` to a runner picking it up, in whole seconds.
- **Pods and jobs:** runner pods are `mk4-runner-<random>`, and no metric links a pod to its job.
  `kubectl -n runners get ephemeralrunners` shows which job each live pod runs.

## Upgrading ARC

Helm installs the controller chart's CRDs only on first install and never upgrades them, so a new ARC version is not
a version bump in `runners.tf` alone. As GitHub's ARC documentation describes: remove `arc-runners`, then `arc`,
delete the `actions.github.com` CRDs, then apply both releases at the new version.
Both charts must stay on the same version (`local.arc_chart_version`). The runner image in `values.yaml` is separate.
Dependabot proposes new runner releases, and GitHub stops sending jobs to runner versions that are too old.
