# Image of the mk4 runner scale set (deploy/minikube/runners/README.md, "The image"): GitHub's runner image plus the
# tools the mrjithendar organization's workflows call without installing them first, as ubuntu-latest provided them.
# Built on a GitHub-hosted arm64 runner by .github/workflows/build-runner-image.yml and pushed as
# ghcr.io/mrjithendar/mk4-runner:<runner version>-<first 12 hex of this file's SHA-256>. values.yaml must name exactly
# that tag (the workflow fails the change otherwise), so every edit here gets a new tag.
FROM ghcr.io/actions/actions-runner:2.338.2

LABEL org.opencontainers.image.source="https://github.com/mrjithendar/k8s-deployments" \
      org.opencontainers.image.description="GitHub Actions runner for mk4 (ARC scale set mk4, arm64)"

# mk4's nodes are arm64 only; the checksums below are the linux/arm64 builds.
ARG GH_VERSION=2.105.0
ARG GH_SHA256=b1278c75107423d818f1937fd92cafc9e5e9b606631568f68d7fb816f5885a02
ARG KUBECTL_VERSION=v1.37.2
ARG KUBECTL_SHA256=fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509
ARG HELM_VERSION=v4.3.2
ARG HELM_SHA256=df31cb61680ef728f82c770e5280dd565617bee6b039d37e85c677288be4bbb0

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Ubuntu 24.04 packages, each for a workflow that calls it (README.md has who uses what):
#   git-lfs                     actions/checkout with lfs: true (jithendar.com content.yml)
#   python3 -dev -pip -venv     python3 / pip without actions/setup-python (jithendar.com, moks-cms); headers and the
#                               shared libraries setup-python's own builds load
#   build-essential             native npm and pip modules; roboshop-ai-app currency-service
#   libgomp1                    LightGBM's OpenMP runtime (moks-sre-agent)
#   shellcheck                  jithendar.com, k8s-deployments, moks-cms, moks-seo-agent, practice-programming
#   openssh-client              disk-cleanup.yml (ssh to the nodes)
#   zstd                        actions/cache compresses with zstd when present, as on GitHub's runners
# pip.conf as on GitHub's Ubuntu images: plain `pip install` works on the system Python (PEP 668).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git-lfs \
      python3 python3-dev python3-pip python3-venv \
      build-essential \
      libgomp1 \
      shellcheck \
      openssh-client \
      zstd \
 && rm -rf /var/lib/apt/lists/* \
 && git lfs install --system \
 && printf '[global]\nbreak-system-packages = true\n' > /etc/pip.conf

# Released binaries, checked against the SHA-256 pinned above:
#   gh       gh api / gh pr in moks-cms and others
#   kubectl  k8s-deployments' deploy and disk-cleanup workflows (no setup-kubectl there); same minor as mk4
#   helm     k8s-deployments' check and deploy workflows (no setup-helm there)
RUN cd /tmp \
 && curl -fsSLo gh.tar.gz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz" \
 && echo "${GH_SHA256}  gh.tar.gz" | sha256sum -c - \
 && tar -xzf gh.tar.gz \
 && install -m 0755 "gh_${GH_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh \
 && curl -fsSLo kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/arm64/kubectl" \
 && echo "${KUBECTL_SHA256}  kubectl" | sha256sum -c - \
 && install -m 0755 kubectl /usr/local/bin/kubectl \
 && curl -fsSLo helm.tar.gz "https://get.helm.sh/helm-${HELM_VERSION}-linux-arm64.tar.gz" \
 && echo "${HELM_SHA256}  helm.tar.gz" | sha256sum -c - \
 && tar -xzf helm.tar.gz \
 && install -m 0755 linux-arm64/helm /usr/local/bin/helm \
 && rm -rf /tmp/*

# pip installs for the runner user land in ~/.local (scripts in ~/.local/bin).
ENV LANG=C.UTF-8 \
    PATH=/home/runner/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

USER runner
