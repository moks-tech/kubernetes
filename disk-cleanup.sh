#!/usr/bin/env bash
# Free disk on the mk4 k3s cluster. A dry run by default: it lists what would go and changes nothing.
#
#   ./disk-cleanup.sh                          preview everything
#   ./disk-cleanup.sh --apply                  remove it (asks for the context name; add --yes without a terminal)
#   ./disk-cleanup.sh --apply --only harbor --keep 3
#
# pods    Finished pods (Succeeded, Failed, evicted) in every namespace. Their exited containers would keep crictl
#         from removing the images they used.
# nodes   On every node over SSH: k3s crictl rmi --prune (images no container uses), journald down to 200M,
#         apt-get clean; each node's disk, images, journald and apt cache before and after. local-path folders
#         without a PersistentVolume are listed, never deleted.
# harbor  Every untagged artifact is deleted; of the tagged ones each repository keeps the newest --keep (default 2)
#         and the one tagged latest. A digest a workload pins (image@sha256:...) is always kept. Garbage collection
#         then frees the volume (not 02:15-03:45 UTC, the backup window); it leaves blobs pushed in the last two hours.
#
# Ends with a table of each node's disk and the Harbor volume, before and after (in a dry run: now, and what --apply
# could free).
#
# Cluster: KUBE_CONTEXT, else the current context. SSH: SSH_USER (root), SSH_KEY (~/.ssh/hetzner_k3s), each node's
# ExternalIP. Harbor: HARBOR_URL (https://registry.jithendar.com), HARBOR_USER and HARBOR_PASSWORD (default: admin from
# Secret harbor/harbor-credentials; if Harbor refuses it, asked for on the terminal). The login reaches curl on stdin.
# Needs kubectl, jq, curl and ssh.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIB="$ROOT/scripts/lib"
APPLY=false YES=false KEEP=2 ONLY=pods,nodes,harbor FAILED=0

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
human() {   # human BYTES (may be negative)
  awk -v b="${1:-0}" 'BEGIN { s = ""; if (b < 0) { s = "-"; b = -b }; split("B KiB MiB GiB TiB", u, " "); i = 1
    while (b >= 1024 && i < 5) { b /= 1024; i++ }; printf "%s%.1f %s", s, b, u[i] }'
}
pct() { echo $((($1 * 100 + $2 / 2) / $2)); }   # pct PART TOTAL, rounded
used_pct() {   # used_pct USED [TOTAL]: "60.0 GiB (83%)"
  if [[ -z ${2:-} || ${2:-0} == 0 ]]; then human "$1"; return; fi
  printf '%s (%s%%)' "$(human "$1")" "$(pct "$1" "$2")"
}
REGISTRY_HOST=''

# Summary rows: name, used before, disk size (for the percentage; empty for a volume), used after (apply), and the
# estimate of what --apply frees (dry run). Sizes in bytes; empty when unknown.
ROW_NAME=() ROW_BEFORE=() ROW_SIZE=() ROW_AFTER=() ROW_ESTIMATE=()
add_row() { ROW_NAME+=("$1"); ROW_BEFORE+=("$2"); ROW_SIZE+=("$3"); ROW_AFTER+=("$4"); ROW_ESTIMATE+=("$5"); }
row() { printf '  %-34s  %-18s  %-18s  %s\n' "$@"; }

print_summary() {
  local i total=0 freed
  ((${#ROW_NAME[@]})) || return 0
  if $APPLY; then
    section 'Summary: used disk before and after'
    row '' before after freed
    for i in "${!ROW_NAME[@]}"; do
      if [[ -n ${ROW_BEFORE[i]} && -n ${ROW_AFTER[i]} ]]; then
        freed=$((ROW_BEFORE[i] - ROW_AFTER[i]))
        total=$((total + freed))
        row "${ROW_NAME[i]}" "$(used_pct "${ROW_BEFORE[i]}" "${ROW_SIZE[i]}")" \
          "$(used_pct "${ROW_AFTER[i]}" "${ROW_SIZE[i]}")" "$(human "$freed")"
      else
        row "${ROW_NAME[i]}" "${ROW_BEFORE[i]:+$(used_pct "${ROW_BEFORE[i]}" "${ROW_SIZE[i]}")}" '?' '?'
      fi
    done
    row 'total freed' '' '' "$(human "$total")"
    if step nodes && [[ -n $REGISTRY_HOST ]]; then
      echo "  Harbor's volume is on $REGISTRY_HOST, measured before Harbor's GC, so the rows add up."
    fi
  else
    section 'Summary: used disk now, and what --apply could free'
    row '' 'used now' 'could free'
    for i in "${!ROW_NAME[@]}"; do
      total=$((total + ${ROW_ESTIMATE[i]:-0}))
      row "${ROW_NAME[i]}" "${ROW_BEFORE[i]:+$(used_pct "${ROW_BEFORE[i]}" "${ROW_SIZE[i]}")}" \
        "${ROW_ESTIMATE[i]:+~$(human "${ROW_ESTIMATE[i]}")}"
    done
    row 'total' '' "~$(human "$total")"
    echo '  Estimates: images count compressed (they free more unpacked); Harbor counts every deleted build in full'
    echo '  (builds share layers, so GC frees less).'
  fi
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

# --- nodes ------------------------------------------------------------------------------------------------------------
SSH_USER="${SSH_USER:-root}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/hetzner_k3s}"
JOURNAL_LIMIT=$((200 * 1024 * 1024))   # journalctl --vacuum-size=200M in node-disk-cleanup.sh

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
  local node ip n images pv key a b estimate extra mode=report pvs=()
  local used avail journal apt removed journal_after apt_after used_after avail_after
  if $APPLY; then mode=apply; fi
  while IFS= read -r pv; do [[ -z $pv ]] || pvs+=("$pv"); done \
    < <(k get pv -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
  k get nodes -o json | jq -r '.items[] | .metadata.name + "\t" + ([.status.addresses[] | select(.type == "ExternalIP")][0].address // [.status.addresses[] | select(.type == "InternalIP")][0].address)' \
    >"$WORK/nodes"
  while IFS=$'\t' read -r -u 3 node ip; do
    echo
    echo "  -- $node ($ip)"
    if ! on_node "$ip" <<<'k3s crictl images -o json' >"$WORK/images.json" 2>"$WORK/ssh.err" ||
      ! on_node "$ip" <<<'k3s crictl ps -a -o json' >"$WORK/containers.json" 2>"$WORK/ssh.err"; then
      fail "    SSH failed: $(tail -n 1 "$WORK/ssh.err")"
      add_row "$node" '' '' '' ''
      continue
    fi
    jq -n --slurpfile i "$WORK/images.json" --slurpfile c "$WORK/containers.json" -f "$LIB/unused-images.jq" \
      >"$WORK/unused.json"
    n="$(jq length "$WORK/unused.json")"
    images="$(jq 'map(.size) | add // 0' "$WORK/unused.json")"
    if $APPLY; then echo "    removing $n unused images, journald above 200M and apt's cache ..."; fi
    if ! on_node "$ip" "$mode" ${pvs[@]+"${pvs[@]}"} <"$LIB/node-disk-cleanup.sh" >"$WORK/node.out" 2>"$WORK/ssh.err"; then
      fail "    cleanup on $node failed: $(tail -n 1 "$WORK/ssh.err")"
      add_row "$node" '' '' '' ''
      continue
    fi
    used='' avail='' journal=0 apt=0 removed='' journal_after='' apt_after='' used_after='' avail_after=''
    : >"$WORK/node.notes"
    while read -r key a b; do
      case $key in
        disk) used=$a avail=$b ;;
        journal) journal=$a ;;
        apt) apt=$a ;;
        orphan) echo "    no PersistentVolume  $(human "$a")  $b  (left in place)" >>"$WORK/node.notes" ;;
        orphans) echo '    local-path folders  not checked (no PersistentVolume list)' >>"$WORK/node.notes" ;;
        removed) removed=$a ;;
        journal_after) journal_after=$a ;;
        apt_after) apt_after=$a ;;
        disk_after) used_after=$a avail_after=$b ;;
        error) fail "    $node: $a $b" ;;
      esac
    done <"$WORK/node.out"
    if [[ -z $used ]]; then fail "    no disk figures from $node"; add_row "$node" '' '' '' ''; continue; fi

    echo "    disk          $(human "$used") used of $(human $((used + avail))), $(human "$avail") free ($(pct "$used" $((used + avail)))%)"
    echo "    images        $n unused, $(human "$images") compressed (more once unpacked)"
    jq -r '.[:25][] | "\(.size)\t\(.name)"' "$WORK/unused.json" |
      while IFS=$'\t' read -r a b; do printf '                  %10s  %s\n' "$(human "$a")" "$b"; done
    if ((n > 25)); then echo "                  ... and $((n - 25)) more"; fi
    echo "    journald      $(human "$journal")   apt cache  $(human "$apt")"
    cat "$WORK/node.notes"
    if $APPLY; then
      echo "    removed       ${removed:-?} images; journald $(human "$journal") -> $(human "${journal_after:-0}"); apt cache $(human "$apt") -> $(human "${apt_after:-0}")"
      if [[ -n $used_after ]]; then
        echo "    disk after    $(human "$used_after") used, $(human "$avail_after") free: freed $(human $((used - used_after)))"
      fi
      add_row "$node" "$used" $((used + avail)) "$used_after" ''
    else
      extra=$((journal > JOURNAL_LIMIT ? journal - JOURNAL_LIMIT : 0))
      estimate=$((images + extra + apt))
      echo "    could free    ~$(human "$estimate") (images $(human "$images"), journald $(human "$extra"), apt cache $(human "$apt"))"
      add_row "$node" "$used" $((used + avail)) '' "$estimate"
    fi
  done 3<"$WORK/nodes"
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

registry_bytes() {   # bytes in the registry volume (du in the registry container); nothing when unknown
  local kib
  kib="$(k -n harbor exec deploy/harbor-registry -c registry -- du -sk /storage 2>/dev/null || true)"
  kib="${kib%%[[:space:]]*}"
  if [[ $kib =~ ^[0-9]+$ ]]; then echo $((kib * 1024)); fi
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
  section "Harbor $HARBOR_URL: delete untagged artifacts, keep the newest $KEEP tagged of each repository"
  harbor_login
  local before after shown project repo count name path n size d deleted=0 bytes=0 verb='to delete'
  if $APPLY; then verb='deleted'; fi
  REGISTRY_HOST="$(k -n harbor get pods -l component=registry -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)"
  before="$(registry_bytes)"
  if [[ -n $before ]]; then shown="$(human "$before")"; else shown='unknown (du in the registry container failed)'; fi
  echo "  registry volume: $shown${REGISTRY_HOST:+ (on $REGISTRY_HOST)}"
  # Digests that a workload pins (image@sha256:...), old ReplicaSets included. A pod that runs a tag pulls the tag.
  k get pods,deployments,statefulsets,daemonsets,replicasets,jobs,cronjobs -A -o json |
    jq '[.. | objects | .image? | strings | scan("sha256:[0-9a-f]{64}")] | map({(.): true}) | add // {}' \
      >"$WORK/inuse.json"
  echo "  digests that workloads pin: $(jq length "$WORK/inuse.json")"

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
  if $APPLY; then
    after="$(registry_bytes)"
    if [[ -n $before && -n $after ]]; then
      echo "  registry volume: $(human "$before") -> $(human "$after"), freed $(human $((before - after)))"
    fi
    add_row "Harbor registry volume" "$before" '' "$after" ''
  else
    add_row "Harbor registry volume" "$before" '' '' "$bytes"
  fi
}

# --- run --------------------------------------------------------------------------------------------------------------
k get --raw /readyz >/dev/null || { echo "Cannot reach context '$CONTEXT'." >&2; exit 1; }
if $APPLY; then
  echo "APPLY on context '$CONTEXT': $ONLY (Harbor: untagged artifacts go, $KEEP tagged kept per repository)."
  if ! $YES; then
    [[ -t 0 ]] || { echo 'No terminal to confirm on: add --yes.' >&2; exit 1; }
    read -r -p 'Type the context name to continue: ' answer
    [[ $answer == "$CONTEXT" ]] || { echo 'Cancelled.'; exit 1; }
  fi
else
  echo "DRY RUN on context '$CONTEXT': nothing changes. Add --apply to remove what is listed."
fi

# Nodes before Harbor: each node's figures then leave out Harbor's GC, and the Harbor row adds it once.
if step pods; then clean_pods; fi
if step nodes; then clean_nodes; fi
if step harbor; then clean_harbor; fi

print_summary
section Done
if ((FAILED)); then echo "$FAILED problem(s); see the messages above." >&2; exit 1; fi
if ! $APPLY; then echo 'Dry run: nothing was changed. Run again with --apply.'; fi
