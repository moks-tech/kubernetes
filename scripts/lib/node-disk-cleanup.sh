#!/usr/bin/env bash
# Runs on one k3s node as root; disk-cleanup.sh sends it over SSH:  bash -s -- report|apply [PersistentVolume name...]
# Prints "key values" lines, sizes in bytes, for disk-cleanup.sh to format:
#   disk USED AVAIL   journal BYTES   apt BYTES   orphan BYTES PATH (a local-path folder no PersistentVolume owns; never
#   deleted) or orphans unchecked (no PersistentVolume list).
# apply then removes images no container uses (k3s crictl rmi --prune), trims journald to 200M, cleans apt's cache and
# prints: removed COUNT   journal_after BYTES   apt_after BYTES   disk_after USED AVAIL   error TEXT (if anything failed).
set -uo pipefail
mode=${1:?report or apply}
shift
known=" $* "

disk() { df -B1 --output=used,avail / | tail -n 1 | awk '{print $1, $2}'; }
bytes() { du -scb "$@" 2>/dev/null | awk 'END {print $1 + 0}'; }
journal() { bytes /var/log/journal /run/log/journal; }
apt_cache() { bytes /var/cache/apt/archives; }

echo "disk $(disk)"
echo "journal $(journal)"
echo "apt $(apt_cache)"
if [ $# -eq 0 ]; then
  echo 'orphans unchecked'
else
  for dir in /var/local-path-provisioner/*/; do
    [ -d "$dir" ] || continue
    name=$(basename "$dir")
    case $known in
      *" ${name%%_*} "*) ;;
      *) echo "orphan $(bytes "$dir") ${dir%/}" ;;
    esac
  done
fi

[ "$mode" = apply ] || exit 0

# crictl's default 2s deadline is too short for many removals at once (DeadlineExceeded while containerd carries on), so
# allow 10 minutes, and go round once more for anything that still failed.
prune() { k3s crictl --timeout 10m rmi --prune 2>&1; }
out=$(prune)
removed=$(grep -c '^Deleted' <<<"$out")
if grep -v '^Deleted' <<<"$out" | grep -q .; then
  out=$(prune)
  removed=$((removed + $(grep -c '^Deleted' <<<"$out")))
fi
echo "removed $removed"
grep -v '^Deleted' <<<"$out" | sed '/^$/d; s/^/error crictl: /'
journalctl --vacuum-size=200M >/dev/null 2>&1 || echo 'error journalctl --vacuum-size=200M failed'
apt-get clean || echo 'error apt-get clean failed'
echo "journal_after $(journal)"
echo "apt_after $(apt_cache)"
echo "disk_after $(disk)"
