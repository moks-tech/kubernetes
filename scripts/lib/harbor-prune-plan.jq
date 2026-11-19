# Which artifacts of one Harbor repository disk-cleanup.sh keeps. Input: the repository's artifacts
# (GET .../artifacts?with_tag=true, every page). Arguments: --argjson keep N, --slurpfile inuse FILE ({digest: true} for
# every digest a pod runs or a workload pins). Output: one entry per top-level artifact, newest first, with keep and the
# reason it is kept (null when it is deleted).
#
# Kept: the newest $keep pushes (tagged or untagged), the artifact tagged "latest" (a rollback moves the tag to an older
# build), and anything in use. Children of an image index are left out: they live and go with their index.
($inuse[0] // {}) as $used
| ([.[] | (.references // [])[].child_digest] | map({(.): true}) | add // {}) as $children
| map(select($children[.digest] | not))
| sort_by(.push_time, .digest) | reverse
| to_entries
| map(
    .key as $rank
    | .value as $a
    | {
        digest: $a.digest,
        push_time: $a.push_time,
        tags: [($a.tags // [])[].name],
        size: ($a.size // 0),
        reason: (
          if $rank < $keep then "newest \($rank + 1)"
          elif any(($a.tags // [])[]; .name == "latest") then "tagged latest"
          elif any(([$a.digest] + [($a.references // [])[].child_digest])[]; $used[.] == true) then "in use"
          else null end)
      }
    | .keep = (.reason != null))
