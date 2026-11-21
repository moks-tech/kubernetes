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
- **A bare image.** `ghcr.io/actions/actions-runner` is a minimal Ubuntu with `git`, `curl` and passwordless `sudo`.
  It ships without the toolchains GitHub's own images have, so use the `setup-*` actions (`setup-node`, `setup-python`,
  `setup-terraform`...) or `sudo apt-get install`.
- **No Docker.** Jobs run inside the runner container. There is no Docker daemon, because Docker-in-Docker needs a
  privileged container, which Gatekeeper refuses on mk4. Jobs that build images, or use `container:` or `services:`,
  stay on GitHub-hosted runners.
- **Internet only.** NetworkPolicy `runners/runners` lets runner pods resolve names through CoreDNS and reach
  public addresses only, on any port. They cannot reach anything on mk4's private networks (pods, services, nodes)
  or the Hetzner metadata service, and no Service is exposed to them.
- **Private repositories only.** The organization's `Default` runner group does not serve public repositories
  unless *Allow public repositories* is ticked (Settings → Actions → Runner groups). Leave it off: a pull request from
  a fork of a public repository would otherwise run its code on mk4.

## Limits

`deploy/minikube/runners/values.yaml` sets none yet: no `minRunners` (no runner waits idle), no `maxRunners` (one pod
per queued job, without bound), and no CPU or memory requests or limits on the runner pod. Set them there before
pointing busy workflows here; the workers also run every application on mk4.

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

## Upgrading ARC

Helm installs the controller chart's CRDs only on first install and never upgrades them, so a new ARC version is not
a version bump in `runners.tf` alone. As GitHub's ARC documentation describes: remove `arc-runners`, then `arc`,
delete the `actions.github.com` CRDs, then apply both releases at the new version.
Both charts must stay on the same version (`local.arc_chart_version`). The runner image in `values.yaml` is separate.
Dependabot proposes new runner releases, and GitHub stops sending jobs to runner versions that are too old.
