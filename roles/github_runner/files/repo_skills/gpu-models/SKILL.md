---
name: gpu-models
description: Use the shared GPU model server over HTTP from a job on the home server, for chat, JSON schema, tools, embeddings, reranking and token counts with llama.cpp models. Use when code or a test needs a local model, when an AI feature of the repository needs trying against a real model, or when asked which models exist, what context they have or why a model call fails.
---

# GPU models

A llama.cpp router on another machine on the host's private network serves
an OpenAI-compatible API. You get HTTP and nothing else: no shell, no way to
add a model, and no other port on that machine. The firewall lets a job reach
the API and the manifest there, and nothing else off this box, as the rules
say.

## Rules

- **Test data only.** The public corpus, seed data and made-up examples. Never
  send personal data, customer documents, secrets or anything from a real
  customer or household. If you are unsure, do not send it.
- **The key is a secret.** Do not print it, log it, put it in a commit, a PR, a
  comment or a URL, or pass it on a command line, where every account on the
  box can read it. Read it from the environment in code, never with `env`,
  `printenv` or `cat /proc/self/environ`.
- **The access is for development.** Do not build anything that needs it to
  last. Production code takes the address from its own settings.
- **The GPU is shared and has one slot.** One request runs at a time and others
  wait, and an evaluation run may be measuring speed. Keep batches small, run one
  client at a time, and stop if asked. Do not call `/models/load` or
  `/models/unload`, since a request loads what it names.

## Setup

A job of a repository that has this skill finds `MODEL_URL` (ends in `/v1`),
`MODEL_API_KEY` and `MODEL_INFO_URL` in its environment already; there is
nothing to source. If `MODEL_URL` is unset, this repository has no access: say
so in the PR and stop. See what is served, without printing the key:

```sh
curl -s "$MODEL_INFO_URL"
python3 - <<'PY'
import json, os, urllib.request
info = os.environ["MODEL_INFO_URL"].removesuffix("/manifest")
print(json.load(urllib.request.urlopen(info + "/gpu", timeout=30)))
request = urllib.request.Request(
    os.environ["MODEL_URL"] + "/models",
    headers={"Authorization": "Bearer " + os.environ["MODEL_API_KEY"]},
)
for row in json.load(urllib.request.urlopen(request, timeout=30))["data"]:
    args = row["status"]["args"]
    print(row["id"], row["status"]["value"], args[args.index("--ctx-size") + 1])
PY
```

`MODEL_INFO_URL` (`/manifest`) and `/gpu` beside it need no key. They give the
model list with its checksums and the GPU's state. Record both with any result
you keep.

## Models

The list above is the truth. As of 2026-11-05:

| Name                   | Use                                                     | Context     |
| ---------------------- | ------------------------------------------------------- | ----------- |
| `gemma-4-12b-q4`       | The default. Best reader of Finnish legal text measured | 16,384      |
| `gemma-4-12b-q4-32k`   | The same, for long prompts                              | 32,768      |
| `gemma-4-12b-q4-think` | The same with reasoning, at most 2,048 tokens of it     | 32,768      |
| `gemma-4-12b-q4-par2`  | The same with two slots, for two reads at once          | 16,384 each |
| `gemma-4-12b-q6`       | Less quantised, untested for gains, fills the GPU       | 16,384      |
| `gemma-4-e4b-q4`       | Smaller and about twice as fast, fewer right answers    | 16,384      |
| `gemma-4-e4b-q4-32k`   | The same, for long prompts                              | 32,768      |
| `qwen3-8b-q4`          | A second opinion of another family                      | 16,384      |
| `poro-2-8b-q4`         | Finnish pretraining, weak on refusals, no tools         | 16,384      |
| `eurollm-22b-q4`       | Larger than the GPU, slow, no tools or JSON schema      | 16,384      |
| `bge-m3`               | Embeddings, 1,024 dimensions                            | 8,192       |
| `bge-reranker-v2-m3`   | Reranking                                               | 8,192       |

A setting is chosen by the model's name. Each name loads on its own, so changing
name reloads the model, which takes from seconds to about a minute, and only one
model is loaded at a time. Group your calls by model. A setting no name offers
needs the owner, who runs the server: ask for it in the PR. `max_tokens` works
per request.

## Calls

Use `$MODEL_URL` as the base URL of any OpenAI client, or plain HTTP. Allow a
timeout of ten minutes, for a load and a queue.

```python
import json, os, urllib.request

def post(path, body, root=False):
    url = os.environ["MODEL_URL"]
    if root:
        url = url.removesuffix("/v1")
    request = urllib.request.Request(
        url + path,
        json.dumps(body).encode(),
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["MODEL_API_KEY"],
        },
    )
    return json.load(urllib.request.urlopen(request, timeout=600))

reply = post("/chat/completions", {
    "model": "gemma-4-12b-q4",
    "messages": [{"role": "user", "content": "Kuka kutsuu yhtiökokouksen koolle?"}],
    "temperature": 0,
    "seed": 0,
    "max_tokens": 800,
    # Without these Gemma thinks first, and is about ten times slower.
    "reasoning_effort": "none",
    "chat_template_kwargs": {"enable_thinking": False},
})
print(reply["choices"][0]["message"]["content"])
```

- **JSON schema:** add `"response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {...}}}`.
- **Tools:** the OpenAI `tools` field, on Gemma and Qwen.
- **Embeddings:** `post("/embeddings", {"model": "bge-m3", "input": ["..."]})`.
  Send batches of at most 32.
- **Reranking:** `post("/rerank", {"model": "bge-reranker-v2-m3", "query": "...", "documents": ["..."]})` gives `results` with `index` and `relevance_score`.
  Sort them yourself, and cut a long text to about 4,000 characters.
- **Token count:** `post("/tokenize", {"model": name, "content": text}, root=True)`, on the root and not under `/v1`. Prompt and answer together must
  fit the context, or the request fails.
- **Context of a loaded model:** `GET /props?model=<name>` on the root. Its
  `n_ctx` is per slot.

## What the measurements taught

From the owner's measurements, November 2026:

- Ask for the reasoning first and the conclusion last: quoted rule, situation,
  conditions, conclusion. It was the largest single gain.
- Temperature 0 and a fixed seed, so that two runs compare.
- A model's verdict on another model's answer is not a measure. Model judges
  scored right answers about 10 points low.
- A gain measured on the questions you tuned on often does not hold. Test on
  questions no one has tuned on, and use them once.
- A number computed by a model is not to be trusted. Compute in code.

## When a call fails

| Sign                          | Meaning                                                                                                                                                               |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 401                           | The key is missing or was taken away. Say so in the PR; the owner rotates it                                                                                          |
| Connection refused or timeout | The server is outside its window, or down. Check `/health` on the root, which needs no key. A refused connection to any other address is the firewall, not the server |
| Long wait, then an answer     | A model load, or another client in the queue                                                                                                                          |
| An error about the context    | The prompt is too long. Count tokens, or take a `-32k` name                                                                                                           |
| 404 on `/tokenize`            | It is on the root, not under `/v1`                                                                                                                                    |
| Empty content                 | With thinking on, the output went to the reasoning. Raise `max_tokens`, or turn it off                                                                                |
| 500 on a tool call            | Poro writes a form the server refuses                                                                                                                                 |

Do not retry in a tight loop, and report what the server said. Fixes to the
server belong to the owner: say in the PR what it answered.
