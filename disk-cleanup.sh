#!/usr/bin/env bash
# Free disk on the mk4 k3s cluster. A dry run by default: it lists what would go and changes nothing.
#
#   ./disk-cleanup.sh                          preview everything
#   ./disk-cleanup.sh --apply                  remove it (asks for the context name; add --yes without a terminal)
#   ./disk-cleanup.sh --apply --only harbor --keep 3
#
# pods    Finished pods (Succeeded, Failed, evicted) in every namespace. Their exited containers would keep crictl
#         from removing the images they used.
# harbor  Each repository of each project keeps its newest --keep pushes (default 2, tagged or not), the artifact
#         tagged latest and every digest a pod runs or a workload pins; the rest are deleted. Garbage collection then
#         frees the volume (not 02:15-03:45 UTC, the backup window). GC leaves blobs pushed in the last two hours.
# nodes   On every node over SSH: k3s crictl rmi --prune (images no container uses), journald down to 200M,
#         apt-get clean. local-path folders without a PersistentVolume are listed, never deleted.
#
# Cluster: KUBE_CONTEXT, else the current context. SSH: SSH_USER (root), SSH_KEY (~/.ssh/hetzner_k3s), each node's
# ExternalIP. Harbor: HARBOR_URL (https://registry.jithendar.com), HARBOR_USER and HARBOR_PASSWORD (default: admin from
# Secret harbor/harbor-credentials; if Harbor refuses it, asked for on the terminal). The login reaches curl on stdin.
# Needs kubectl, jq, curl and ssh.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIB="$ROOT/scripts/lib"
APPLY=false YES=false KEEP=2 ONLY=pods,harbor,nodes FAILED=0

usage() { awk 'NR == 1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"; }
while (($#)); do
  case "$1" in
    --apply) APPLY=true ;;
    --yes) YES=true ;;
    --keep)
      [[ ${2:-} =~ ^[1-9][0-9]*$ ]] || { echo '--keep needs a whole number of at least 1' >&2; exit 2; }
      KEEP=$2; shift ;;
    --only)
      [[ ${2:-} =~ ^(pods|harbor|nodes)(,(pods|harbor|nodes))*$ ]] ||
        { echo '--only takes pods, harbor and nodes, comma-separated' >&2; exit 2; }
      ONLY=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

for bin in kubectl jq curl ssh awk; do command -v "$bin" >/dev/null || { echo "Missing: $bin" >&2; exit 1; }; done
CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
trap 'echo "Stopped at line $LINENO; anything above marked deleted is gone, the rest is untouched." >&2' ERR

k() { kubectl --context "$CONTEXT" --request-timeout=60s "$@"; }
step() { [[ ",$ONLY," == *",$1,"* ]]; }
section() { printf '\n== %s\n' "$*"; }
fail() { echo "$*" >&2; FAILED=$((FAILED + 1)); }
human() {
  awk -v b="${1:-0}" 'BEGIN { split("B KiB MiB GiB TiB", u, " "); i = 1
    while (b >= 1024 && i < 5) { b /= 1024; i++ }; printf "%.1f %s", b, u[i] }'
}

# --- finished pods ----------------------------------------------------------------------------------------------------
clean_pods() {
  section 'Finished pods'
  local phase list pod
  for phase in Failed Succeeded; do
    list="$(k get pods -A --field-selector="status.phase==$phase" \
      -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}')"
    if [[ -z $list ]]; then echo "  $phase: none"; continue; fi
    echo "  $phase: $(grep -c . <<<"$list")"
    while IFS= read -r pod; do echo "    $pod"; done <<<"$list"
    if $APPLY; then k delete pods -A --field-selector="status.phase==$phase" --wait=false | sed 's/^/    /'; fi
  done
}

# --- Harbor -----------------------------------------------------------------------------------------------------------
HARBOR_URL="${HARBOR_URL:-https://registry.jithendar.com}"
HARBOR_URL="${HARBOR_URL%/}"
CURL_LOGIN=''

harbor() {   # harbor METHOD PATH [curl args...]: the login reaches curl on stdin, never the command line
  local method=$1 path=$2
  shift 2
  printf '%s\n' "$CURL_LOGIN" |
    curl -fsS --config - -X "$method" -H 'Content-Type: application/json' "$HARBOR_URL/api/v2.0$path" "$@"
}

harbor_all() {   # harbor_all PATH: every page of a list endpoint, as one JSON array
  local path=$1 sep='?' page=1 n
  if [[ $path == *\?* ]]; then sep='&'; fi
  while :; do
    harbor GET "$path${sep}page=$page&page_size=100" >"$WORK/page.json"
    jq -c '.[]?' "$WORK/page.json"
    n="$(jq 'length' "$WORK/page.json")"
    ((n == 100)) || break
    page=$((page + 1))
  done | jq -s .
}

set_login() {   # set_login USER PASSWORD: the curl config line, quoted for curl
  local login="$1:$2"
  login="${login//\\/\\\\}"
  login="${login//\"/\\\"}"
  CURL_LOGIN="user = \"$login\""
}

# HARBOR_PASSWORD, else the Secret. Harbor reads the Secret only on its first start, so after a password change in the
# portal it is refused: then the password is asked for on a terminal (not echoed, never in argv or the environment).
harbor_login() {
  local user="${HARBOR_USER:-admin}" password="${HARBOR_PASSWORD:-}" from=HARBOR_PASSWORD
  if [[ -z $password ]]; then
    from='Secret harbor/harbor-credentials'
    password="$(k -n harbor get secret harbor-credentials -o jsonpath='{.data.HARBOR_ADMIN_PASSWORD}' | base64 -d)"
  fi
  if [[ -n $password ]]; then
    set_login "$user" "$password"
    if harbor GET /users/current >/dev/null 2>&1; then return 0; fi
    echo "  Harbor refused $user's password from $from (changed in the portal since?)."
  fi
  if [[ ! -t 0 ]]; then
    echo "Harbor login as $user failed at $HARBOR_URL: run on a terminal to type the password, or set HARBOR_PASSWORD." >&2
    exit 1
  fi
  read -r -s -p "  Harbor password for $user (not shown): " password
  echo
  set_login "$user" "$password"
  harbor GET /users/current >/dev/null 2>&1 || { echo "Harbor login as $user failed at $HARBOR_URL." >&2; exit 1; }
}

harbor_gc() {
  local now last job id=0 status='' i
  now="$(date -u +%H%M)"
  if ((10#$now >= 215 && 10#$now <= 345)); then
    echo '  garbage collection: skipped, 02:15-03:45 UTC is the backup window; run --apply --only harbor later'
    return 0
  fi
  if ! $APPLY; then echo '  garbage collection: runs after the deletes (it is what frees the disk)'; return 0; fi
  last="$(harbor GET '/system/gc?page=1&page_size=1&sort=-creation_time' | jq '.[0].id // 0')"
  if ! harbor POST /system/gc/schedule >/dev/null \
      -d '{"schedule":{"type":"Manual"},"parameters":{"delete_untagged":false,"dry_run":false,"workers":1}}'; then
    fail '  garbage collection: could not start (is one running already?)'
    return 0
  fi
  echo '  garbage collection started; waiting up to 30 minutes'
  for ((i = 0; i < 180; i++)); do
    sleep 10
    job="$(harbor GET '/system/gc?page=1&page_size=1&sort=-creation_time')"
    id="$(jq '.[0].id // 0' <<<"$job")"
    status="$(jq -r '.[0].job_status // "" | ascii_downcase' <<<"$job")"
    if ((id > last)) && [[ $status =~ ^(success|error|stopped)$ ]]; then break; fi
  done
  echo "  garbage collection job $id: ${status:-still running}"
  { harbor GET "/system/gc/$id/log" | grep -iE 'eligible|frees|error' | tail -n 5 | sed 's/^/    /'; } || true
  if [[ $status != success ]]; then fail "  garbage collection did not succeed; see the job in the portal"; fi
}

clean_harbor() {
  section "Harbor $HARBOR_URL: keep the newest $KEEP pushes of each repository"
  harbor_login
  # Every digest a pod runs (imageID) or a workload pins (image@sha256:...), old ReplicaSets included.
  k get pods,deployments,statefulsets,daemonsets,replicasets,jobs,cronjobs -A -o json |
    jq '[.. | objects | (.image?, .imageID?) | strings | scan("sha256:[0-9a-f]{64}")] | map({(.): true}) | add // {}' \
      >"$WORK/inuse.json"
  echo "  digests that pods run or workloads pin: $(jq length "$WORK/inuse.json")"

  local project repo count name path n size d deleted=0 bytes=0 verb='to delete'
  if $APPLY; then verb='deleted'; fi
  harbor_all /projects | jq -r '.[].name' >"$WORK/projects"
  while IFS= read -r -u 3 project; do
    harbor_all "/projects/$project/repositories" | jq -r '.[] | [.name, .artifact_count] | @tsv' >"$WORK/repos"
    while IFS=$'\t' read -r -u 4 repo count; do
      name="${repo#"$project"/}"
      path="/projects/$project/repositories/${name//\//%252F}"
      if [[ $count == 0 ]]; then
        echo "  $repo: no artifacts, repository $verb"
        if $APPLY; then harbor DELETE "$path" >/dev/null || fail "    could not delete repository $repo"; fi
        continue
      fi
      harbor_all "$path/artifacts?with_tag=true" |
        jq --argjson keep "$KEEP" --slurpfile inuse "$WORK/inuse.json" -f "$LIB/harbor-prune-plan.jq" >"$WORK/plan.json"
      n="$(jq '[.[] | select(.keep | not)] | length' "$WORK/plan.json")"
      size="$(jq '[.[] | select(.keep | not) | .size] | add // 0' "$WORK/plan.json")"
      echo "  $repo: $(jq length "$WORK/plan.json") artifacts, $n $verb (up to $(human "$size"))"
      jq -r '.[] | "    \(if .keep then "keep  " else "delete" end)  \(.digest[7:19])  \(.push_time[0:16] | sub("T"; " "))  " +
        "\(if (.tags | length) > 0 then (.tags | join(",")) else "(untagged)" end)\(if .keep then "  [\(.reason)]" else "" end)"' \
        "$WORK/plan.json"
      deleted=$((deleted + n))
      bytes=$((bytes + size))
      if $APPLY; then
        for d in $(jq -r '.[] | select(.keep | not) | .digest' "$WORK/plan.json"); do
          harbor DELETE "$path/artifacts/$d" >/dev/null || fail "    could not delete $repo@$d"
        done
      fi
    done 4<"$WORK/repos"
  done 3<"$WORK/projects"
  echo "  total: $deleted artifacts $verb, up to $(human "$bytes") (builds share layers, so GC frees less)"
  harbor_gc
}

# --- nodes ------------------------------------------------------------------------------------------------------------
SSH_USER="${SSH_USER:-root}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/hetzner_k3s}"

on_node() {   # on_node IP [ARGS...] <script: runs the script on stdin with bash on the node, as root
  local ip=$1 sudo='' args=''
  shift
  if [[ $SSH_USER != root ]]; then sudo='sudo -n'; fi
  if (($#)); then args="$(printf '%q ' "$@")"; fi
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    "$SSH_USER@$ip" "$sudo bash -s -- $args"
}

clean_nodes() {
  section "Nodes (SSH as $SSH_USER, key $SSH_KEY)"
  local node ip n size pv mode=report pvs=()
  if $APPLY; then mode=apply; fi
  while IFS= read -r pv; do [[ -z $pv ]] || pvs+=("$pv"); done \
    < <(k get pv -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
  k get nodes -o json | jq -r '.items[] | .metadata.name + "\t" + ([.status.addresses[] | select(.type == "ExternalIP")][0].address // [.status.addresses[] | select(.type == "InternalIP")][0].address)' \
    >"$WORK/nodes"
  while IFS=$'\t' read -r -u 3 node ip; do
    echo "  -- $node ($ip)"
    if ! on_node "$ip" <<<'k3s crictl images -o json' >"$WORK/images.json" 2>"$WORK/ssh.err" ||
      ! on_node "$ip" <<<'k3s crictl ps -a -o json' >"$WORK/containers.json" 2>"$WORK/ssh.err"; then
      fail "    SSH failed: $(tail -n 1 "$WORK/ssh.err")"
      continue
    fi
    jq -n --slurpfile i "$WORK/images.json" --slurpfile c "$WORK/containers.json" -f "$LIB/unused-images.jq" \
      >"$WORK/unused.json"
    n="$(jq length "$WORK/unused.json")"
    size="$(jq 'map(.size) | add // 0' "$WORK/unused.json")"
    echo "    images no container uses: $n, $(human "$size") compressed (more on disk)$($APPLY && echo ', removing' || true)"
    jq -r '.[:25][] | "      \(.name)"' "$WORK/unused.json"
    if ((n > 25)); then echo "      ... and $((n - 25)) more"; fi
    on_node "$ip" "$mode" ${pvs[@]+"${pvs[@]}"} <"$LIB/node-disk-cleanup.sh" || fail "    cleanup on $node failed"
  done 3<"$WORK/nodes"
}

# --- run --------------------------------------------------------------------------------------------------------------
k get --raw /readyz >/dev/null || { echo "Cannot reach context '$CONTEXT'." >&2; exit 1; }
if $APPLY; then
  echo "APPLY on context '$CONTEXT': $ONLY (Harbor keeps $KEEP per repository)."
  if ! $YES; then
    [[ -t 0 ]] || { echo 'No terminal to confirm on: add --yes.' >&2; exit 1; }
    read -r -p 'Type the context name to continue: ' answer
    [[ $answer == "$CONTEXT" ]] || { echo 'Cancelled.'; exit 1; }
  fi
else
  echo "DRY RUN on context '$CONTEXT': nothing changes. Add --apply to remove what is listed."
fi

if step pods; then clean_pods; fi
if step harbor; then clean_harbor; fi
if step nodes; then clean_nodes; fi

section Done
if ((FAILED)); then echo "$FAILED problem(s); see the messages above." >&2; exit 1; fi
if ! $APPLY; then echo 'Dry run: nothing was changed. Run again with --apply.'; fi
