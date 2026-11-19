# bge-m3 embeddings

[BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) served by [Ollama](https://github.com/ollama/ollama) on CPU, for
seo-agent (Ask the Blog, crews, news desk) and moks (knowledge search). Installed by
[`minikube-infrastructure/embeddings.tf`](../../../../minikube-infrastructure/embeddings.tf) as release `bge-m3` in
namespace `embeddings`.

| | |
|---|---|
| Endpoint | `http://bge-m3.embeddings:11434`, cluster-internal only |
| Model | `bge-m3:567m` (F16, 1.16 GB), weights digest pinned in `values.yaml` |
| Output | dense vectors, 1024 dimensions, L2-normalised; no query or passage prefix |
| Input | up to 2048 tokens (`ollama.contextLength`); longer input is truncated |
| Callers | `jithendar/seo-agent` (service account `default`), `moks/*` (service account `moks`) |
| Footprint | 1.5 GiB requested, 2.5 GiB limit, no CPU limit; a 10 Gi Hetzner volume for the model |

Not TEI: text-embeddings-inference has no arm64 CPU image, and mk4's nodes are arm64. Ollama serves only the dense
vectors of bge-m3, not its sparse or multi-vector outputs; both callers do keyword search in PostgreSQL already.

## Calling it

```sh
# Ollama's API: the vectors in "embeddings"
curl -s http://bge-m3.embeddings:11434/api/embed -d '{"model": "bge-m3:567m", "input": ["first text", "second text"]}'

# OpenAI-compatible: the vectors in data[].embedding (moks's openai provider, endpoint http://bge-m3.embeddings:11434/v1)
curl -s http://bge-m3.embeddings:11434/v1/embeddings -d '{"model": "bge-m3:567m", "input": ["first text"]}'
```

A new caller needs three things: an entry in `callers` (`values.yaml`), an egress rule to namespace `embeddings`
port 11434 in its own NetworkPolicy, and a sidecar (the AuthorizationPolicy admits mTLS identities only).

## Checking it

```sh
kubectl -n embeddings logs deploy/bge-m3 -c fetch-model      # "downloaded" or "already on the volume"
kubectl -n embeddings port-forward svc/bge-m3 11434:11434 &   # port-forward reaches the pod past the mesh policy
curl -s localhost:11434/api/embed -d '{"model":"bge-m3:567m","input":["hello"]}' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["embeddings"][0]))'   # 1024
```

## Changing the model

Every caller's stored vectors belong to one model. A new model, a new tag or a re-pushed digest means a full reindex in
seo-agent (it builds `seo_article_chunks_next` and swaps it in) and `moks reindex` (a new index generation). Update
`model.name` and `model.digest` together: the digest is the `application/vnd.ollama.image.model` layer of
`https://registry.ollama.ai/v2/library/<name>/manifests/<tag>`.
