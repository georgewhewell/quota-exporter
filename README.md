# llm-quota-exporter

Prometheus exporter for LLM **subscription** usage and rate-limit windows. It
reads the credential files the vendor CLIs already keep in your home directory
and polls each vendor's own usage endpoint, so you can see how close you are to
your Claude / Codex / Gemini / Grok / Kimi limits.

There's no official metrics endpoint for consumer LLM subscriptions; this uses
the same private, undocumented endpoints the CLIs use for their own usage
displays. They can change or break at any time. Not affiliated with any provider.

> This project, including this README, is 100% LLM-generated.

![dashboard](dashboards/screenshot.png)

## Providers

| Provider  | Credentials read              | Endpoint |
|-----------|-------------------------------|----------|
| anthropic | `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` |
| openai    | `~/.codex/auth.json`          | `chatgpt.com/backend-api/wham/usage` |
| gemini    | `~/.gemini/antigravity-cli/antigravity-oauth-token` (or legacy CLI credentials) | `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` |
| grok      | `~/.grok/auth.json`           | `cli-chat-proxy.grok.com/v1/billing` |
| kimi      | `~/.kimi-code/credentials/…`  | `api.kimi.com/coding/v1/usages` |
| deepseek  | `DEEPSEEK_API_KEY` or CLI credentials | `api.deepseek.com/user/balance` |
| openrouter | `OPENROUTER_API_KEY` or CLI credentials | `openrouter.ai/api/v1/credits` and `/key` |

Missing or expired credentials and failed polls report `llm_quota_scrape_success`
0 without emitting old quota values. The last successful timestamp remains
available for diagnostics. Snapshots also expire after two polling intervals
(minimum 60 seconds) if polling stalls. Transient failures back off; credential
gaps are retried at the normal interval.

## Run

```console
$ pip install llm-quota-exporter && llm-quota-exporter --port 9184
$ nix run github:georgewhewell/quota-exporter -- --once
```

Flake outputs: `packages.default`, `overlays.default`, `nixosModules.default`
(`services.llm-quota-exporter`). Poll interval is decoupled from scrapes
(default 300 s).

### Multiple OpenAI accounts

Configure any number of accounts, each with an arbitrary name and an absolute
directory containing its `auth.json`:

```console
$ llm-quota-exporter --providers openai \
    --openai-account account-a=/var/lib/codex/account-a \
    --openai-account account-b=/var/lib/codex/account-b
```

This exports `provider="openai-account-a"` and `provider="openai-account-b"`.
Explicit accounts replace the default `openai` provider and ignore `CODEX_HOME`
and the `~/.codex` symlink. Without them, the existing single-account behavior
is unchanged. Credentials are re-read on every poll; run the exporter beside
the active CLI credentials, since copied tokens can expire independently.

In NixOS, set `services.llm-quota-exporter.openaiAccounts` to an attribute set
mapping account names to these directories. OpenAI tokens remain read-only:
the exporter never consumes a refresh token or resets quota.

Dashboard current-value panels should use instant queries. A range query
reduced with `lastNotNull` can display a historical value after a failed poll.

## Metrics

`llm_quota_utilization_ratio{provider,window,scope}` (0–1),
`llm_quota_reset_timestamp_seconds`, `llm_quota_used` / `llm_quota_limit`,
`llm_spend_usd`, `llm_credits_balance`, `llm_model_available{provider,model}`,
`llm_provider_info{plan,tier}`, and
`llm_quota_scrape_success` / `_last_success_timestamp_seconds` / `_poll_duration_seconds`.

OpenAI's main quota (`scope="all"`), Chatpass windows (`scope="chatpass"`),
credit balance, and model availability are independent readings. A main quota
at 100% does not mean every model is unavailable. `llm_model_available` reports
the upstream `model_usage` availability flag as 1 or 0; an absent flag emits no
series. The dashboard shows model access, both quota scopes, and credits
separately. Availability is hidden on failed or stale polls, just like usage.

### Banked resets and promotional credits

| Metric | Labels | Meaning |
|---|---|---|
| `llm_rate_limit_resets_available` | `provider` | Banked rate-limit resets you can redeem now (OpenAI "Reset usage") |
| `llm_grant_expiry_timestamp_seconds` | `provider,kind,id` | When an individual grant expires |
| `llm_grant_granted_timestamp_seconds` | `provider,kind,id` | When it was issued (where reported) |
| `llm_grant_remaining` | `provider,kind,id,currency` | Unspent amount of a monetary grant, in major units |
| `llm_credits_next_expiry_timestamp_seconds` | `provider` | Next expiry within the prepaid balance |

`kind="rate_limit_reset"` comes from OpenAI: the usage payload carries the
count, and when it is non-zero a second GET to
`wham/rate-limit-reset-credits` lists each reset's grant and expiry dates.
Redeemed or expired resets are omitted. The exporter never redeems one.

`kind="promo_credit"` comes from Anthropic's
`api/oauth/organizations/:orgUUID/prepaid/credits`, which also feeds
`llm_credits_balance{provider="anthropic"}` in USD. That endpoint only covers
the claude.ai org that owns the Claude Code token. Monthly API credits granted
to a linked Claude Console org are not visible to this token and are not
exported yet.

If either detail request fails, the main quota reading is still exported.

Alert on resets about to lapse unused:

```promql
llm_grant_expiry_timestamp_seconds{kind="rate_limit_reset"} - time() < 3 * 86400
```

## Credentials

Tokens are only read, never logged or transmitted anywhere but the provider's
own endpoint. anthropic/openai are strictly read-only (they rotate refresh
tokens with reuse detection, so an outside refresh can revoke a live CLI
session); grok/kimi refresh only once the on-disk token has expired and persist
the rotated pair; gemini refreshes in memory.

A Grafana dashboard is in [`dashboards/`](dashboards/); the token/cost panels
need the CLIs' OpenTelemetry metrics in the same store (optional).

MIT.
