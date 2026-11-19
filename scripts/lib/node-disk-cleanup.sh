#!/usr/bin/env bash
# Runs on one k3s node as root; disk-cleanup.sh sends it over SSH:  bash -s -- report|apply [PersistentVolume name...]
# report: disk, journald and apt cache use, and local-path folders that no PersistentVolume owns (never deleted).
# apply:  also removes images no container uses (k3s crictl rmi --prune), trims journald to 200M and cleans apt's cache.
set -uo pipefail
mode=${1:?report or apply}
shift
known=" $* "

disk() { df -h / | awk 'NR == 2 {print $3 " used of " $2 ", " $4 " free (" $5 ")"}'; }
journal() { journalctl --disk-usage 2>/dev/null | sed -nE 's/.* take up ([^ ]+) .*/\1/p'; }

echo "    disk: $(disk)"
echo "    journald: $(journal)   apt cache: $(du -sh /var/cache/apt/archives 2>/dev/null | cut -f1)"

if [ $# -eq 0 ]; then
  echo "    local-path folders: not checked (no PersistentVolume list)"
else
  for dir in /var/local-path-provisioner/*/; do
    [ -d "$dir" ] || continue
    name=$(basename "$dir")
    case $known in
      *" ${name%%_*} "*) ;;
      *) echo "    no PersistentVolume, left in place: $(du -sh "$dir" 2>/dev/null | cut -f1)  $dir" ;;
    esac
  done
fi

[ "$mode" = apply ] || exit 0

out=$(k3s crictl rmi --prune 2>&1)
echo "    removed $(grep -c '^Deleted' <<<"$out") images"
grep -v '^Deleted' <<<"$out" | sed '/^$/d; s/^/    crictl: /'
journalctl --vacuum-size=200M >/dev/null 2>&1 && echo "    journald trimmed to $(journal)"
apt-get clean && echo "    apt cache cleaned"
echo "    disk now: $(disk)"
