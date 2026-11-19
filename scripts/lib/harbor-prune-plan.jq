# Which artifacts of one Harbor repository disk-cleanup.sh keeps. Input: the repository's artifacts
# (GET .../artifacts?with_tag=true, every page). Arguments: --argjson keep N, --slurpfile inuse FILE ({digest: true} for
# every digest a workload pins as image@sha256:...). Output: one entry per top-level artifact, newest first, with keep
# and the reason it is kept (null when it is deleted).
#
# Kept: the newest $keep tagged artifacts, the one tagged "latest" (a rollback moves the tag to an older build) and any
# digest a workload pins. Every untagged artifact goes unless it is pinned: pods that run a tag pull the tag again, not
# the digest they started with. Children of an image index are left out: they live and go with their index.
($inuse[0] // {}) as $pinned
| ([.[] | (.references // [])[].child_digest] | map({(.): true}) | add // {}) as $children
| map(select($children[.digest] | not))
| sort_by(.push_time, .digest) | reverse
| ([.[] | select((.tags // []) | length > 0) | .digest]) as $tagged
| map(
    . as $a
    | [($a.tags // [])[].name] as $tags
    | ($tagged | index($a.digest)) as $rank
    | {
        digest: $a.digest,
        push_time: $a.push_time,
        tags: $tags,
        size: ($a.size // 0),
        reason: (
          if $rank != null and $rank < $keep then "newest tagged \($rank + 1)"
          elif ($tags | index("latest")) != null then "tagged latest"
          elif any(([$a.digest] + [($a.references // [])[].child_digest])[]; $pinned[.] == true) then "pinned"
          else null end)
      }
    | .keep = (.reason != null))
