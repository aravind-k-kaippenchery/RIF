# Local Fallback Model

## Problem addressed

Every LLM-backed feature (SQL generation, record extraction, document RAG answers,
hybrid evidence, admin schema proposals) previously depended on exactly one Ollama model.
If that model was not installed, the Ollama service was under load, or generation timed
out, the entire agentic layer failed even though a smaller/faster local model might have
been able to answer.

## What was added

`LLMService.generate_json` now tries the configured primary model first
(`OLLAMA_MODEL`). If that attempt fails with an **availability** error --
unreachable, not installed, timed out, or an HTTP error from Ollama -- and a fallback
model is configured, the entire generation is retried once against the fallback model
(`OLLAMA_FALLBACK_MODEL`, default `llama3.2:3b`) on the same loopback Ollama server.

```text
OLLAMA_FALLBACK_MODEL=llama3.2:3b
OLLAMA_FALLBACK_ENABLED=true
```

Pull the fallback model like any other local model:

```powershell
ollama pull llama3.2:3b
```

Leave `OLLAMA_FALLBACK_MODEL` blank or set `OLLAMA_FALLBACK_ENABLED=false` to disable
fallback and restore the original single-model behavior.

## What did not change

- **Still fully local.** `_require_local_endpoint` is enforced for the fallback model
  exactly like the primary model -- both live on `settings.ollama_base_url`, which must
  resolve to `127.0.0.1` / `localhost`. There is no code path that can silently reach a
  cloud API. This preserves the existing local-first guarantee; it does not add a cloud
  dependency.
- **No fallback on output-validation failures.** If the primary model responds but
  produces JSON that fails schema or semantic validation, the existing one-shot
  correction retry against the *same* model still applies, exactly as before. Falling
  back to a different, likely smaller model would not fix a validation problem and would
  make behavior harder to reason about, so fallback is scoped strictly to availability
  failures.
- **SQL/CRUD safety boundaries are unaffected.** Whichever model answers, the response
  still goes through the same AST SQL validator, duplicate checks, confirmation-gated
  writes, and evidence-grounding rules. The fallback model is not a new trust boundary.

## What this does not fix

A local fallback model only helps when the *primary model* is the point of failure
(not installed, crashing, or slow). It does not add resilience if:

- Ollama itself is not running, or
- the machine running the backend is unreachable, or
- both the primary and fallback models are unavailable at the same time.

For those cases the service still returns a controlled `ollama_unavailable` /
`ollama_all_local_models_unavailable` error rather than a raw exception, but there is no
third option -- this remains a single-machine, local-only deployment. A true
multi-machine or cloud-backed high-availability setup is out of scope for this POC, by
design (see `is_local_ollama_endpoint` in `app/core/config.py`).

## Observability

`GET /api/demo/status` now reports an additional informational check, `Local fallback
model`, when a fallback model is configured. It does not block `full_demo_ready` --
the fallback is best-effort resilience, not a required capability.

Structured log events added: `ollama_primary_model_unavailable_trying_local_fallback`,
`ollama_local_fallback_model_used`, `ollama_local_fallback_also_unavailable`.

## Tests

See `app/tests/test_llm_local_fallback_model.py`:

- fallback is used when the primary model is not installed
- fallback is skipped entirely when disabled
- fallback is **not** attempted for output-validation failures
- a clear combined error is raised when both models are unavailable
- the fallback model health check is exposed separately
- the local-only endpoint guard applies to the fallback model too
