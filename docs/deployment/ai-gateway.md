# AI Gateway (2.0.0 shell)

DataOpsStudio 2.0.0 ships the **AI Gateway shell** only: it can reach one
provider and it has the egress-check / redaction / audit hook points wired in,
but the user-facing Assist/Copilot features are later versions (design §2.7).
This page documents how to turn the gateway on and point it at a provider.

## Off by default

AI is **disabled by default** in portable and on-prem forms (design §2.7.6).
With `DATAOPS_AI_ENABLED` unset/false, the gateway still constructs, but any
`complete()` call raises `AiDisabledError`. Nothing leaves the host until you
explicitly enable it.

## Provider

2.0.0 implements one real provider: **OpenAI-compatible** `/chat/completions`.
That single protocol covers OpenAI itself and most self-hosted / private LLM
gateways (vLLM, Ollama, LM Studio, and OpenAI-compatible proxies), which fits the
"on-prem can configure a private LLM" requirement. A built-in `MockProvider` is
used for tests and as the fallback when no key is configured.

## Configuration (environment only)

★ 2.0.0 has **no `ai_configs` table** and no AI config file. The API key is read
**only** from an environment variable, and is never written to disk or git (R8).

| Variable | Meaning |
|---|---|
| `DATAOPS_AI_ENABLED` | `true` to enable; default `false`. |
| `DATAOPS_AI_PROVIDER` | `openai_compatible` to use the real provider. Anything else / unset falls back to the mock. |
| `DATAOPS_AI_ENDPOINT` | Base URL, e.g. `https://api.openai.com/v1` or your private gateway base. |
| `DATAOPS_AI_API_KEY` | Provider API key. ★ Env only — never commit, never put in a config file. |
| `DATAOPS_AI_MODEL` | Optional model name (default `gpt-4o-mini`). |
| `DATAOPS_AI_MAX_AUTO_EGRESS_LEVEL` | Highest egress level allowed to leave automatically (default `0` = L0). See below. |
| `DATAOPS_AI_L4_REQUIRES_OPTIN` | `true` (default): L4 sample data needs explicit opt-in. |

A real provider is wired only when **all** of `DATAOPS_AI_PROVIDER=openai_compatible`,
`DATAOPS_AI_ENDPOINT`, and `DATAOPS_AI_API_KEY` are present; otherwise the gateway
uses the mock so it still constructs cleanly.

### Under systemd

Do not inline the key in the unit file. Use a systemd credential or an
environment file with mode `0600` owned by the deploy user. See the commented
`LoadCredential` example in [`systemd/dataops.service`](./systemd/dataops.service).

## Egress levels (enforced by the gateway, not the caller)

Content is tagged L0–L5 (design §2.7.5). The gateway enforces:

- **L5** (passwords / tokens / PII): **permanently blocked**, every form, every
  config. Blocked calls are audited and never reach the provider.
- **L4** (sample data values): blocked unless opt-in is enabled.
- **L0–L3**: allowed up to `DATAOPS_AI_MAX_AUTO_EGRESS_LEVEL`.

The redaction hook runs on allowed content (a no-op in 2.0.0; the real
RedactionPolicy is a later version) and every call is audited. Per R5, the audit
records only a prompt **hash + length + token counts + egress level** — never the
prompt or response text.

## Verifying a real provider (manual)

The unit/contract tests run fully on a mock transport (no network). To verify a
real provider end to end, set the env vars and run the integration test, which
**skips automatically** when the key is absent:

```bash
DATAOPS_AI_API_KEY=...        # your key (do not echo it into shared logs)
DATAOPS_AI_ENDPOINT=https://api.openai.com/v1
uv run pytest -m integration tests/integration/test_ai_gateway_real_provider.py
```
