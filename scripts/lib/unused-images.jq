# The images `crictl rmi --prune` removes from a node, for disk-cleanup.sh's preview: no container (running or exited)
# uses them and they are not pinned (containerd pins the pod sandbox image). Run with
#   jq -n --slurpfile i IMAGES.json --slurpfile c CONTAINERS.json -f unused-images.jq
# where IMAGES.json is `crictl images -o json` and CONTAINERS.json is `crictl ps -a -o json`.
([$c[0].containers[]? | .imageRef, .image.image | strings] | map({(.): true}) | add // {}) as $used
| [$i[0].images[]?
   | select((.pinned // false) | not)
   | select(any(([.id] + (.repoTags // []) + (.repoDigests // []))[]; $used[.] == true) | not)
   | {name: ((.repoTags // [])[0] // (.repoDigests // [])[0] // .id), size: ((.size // "0") | tonumber)}]
| sort_by(-.size)
