"""OpenAI (ChatGPT/Codex subscription) quota provider.

Reads the Codex CLI token from ~/.codex/auth.json and queries the endpoint the
CLI itself uses for its rate-limit display:

    GET https://chatgpt.com/backend-api/wham/usage

Windows report used_percent (0-100), limit_window_seconds and reset_at (epoch
s). Their duration, not their primary/secondary position, identifies them.
Chatpass windows and model availability are separate from the main quota;
an exhausted main quota does not imply that every model is unavailable.

No token refresh is attempted: OpenAI rotates refresh tokens on use, so an
out-of-band refresh that does not rewrite auth.json would invalidate the CLI's
own login. On 401 we fail the cycle and recover once the codex CLI refreshes
its credentials.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .base import (
    CredentialsUnavailable,
    Provider,
    ProviderError,
    ProviderSnapshot,
    QuotaSample,
    json_object,
)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT = "codex-cli"

_WINDOW_NAMES = {"primary_window": "primary", "secondary_window": "secondary"}


class OpenAICodexProvider(Provider):
    name = "openai"

    def __init__(
        self, home: Path, client: httpx.Client, *, account: str | None = None, codex_home: Path | None = None
    ) -> None:
        super().__init__(home, client)
        self._codex_home = codex_home
        if account is not None:
            self.name = f"openai-{account}"

    def credential_path(self) -> Path:
        if self._codex_home is not None:
            return self._codex_home / "auth.json"
        if codex_home := os.environ.get("CODEX_HOME"):
            return Path(codex_home) / "auth.json"
        return self._home / ".codex" / "auth.json"

    def fetch(self) -> ProviderSnapshot:
        tokens = self._read_tokens()
        access_token = tokens.get("access_token")
        if not access_token:
            raise CredentialsUnavailable("no tokens.access_token in auth.json")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if account_id := tokens.get("account_id"):
            headers["ChatGPT-Account-Id"] = account_id

        try:
            response = self._client.get(USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"usage request failed: {exc}") from exc
        if response.status_code in (401, 403):
            # Read-only provider: a stale token is an expected gap that the
            # codex CLI heals on its next run — not a backoff-worthy failure.
            raise CredentialsUnavailable(
                f"HTTP {response.status_code}: access token stale; will recover after the codex CLI refreshes it"
            )
        if response.status_code != 200:
            raise ProviderError(f"usage endpoint returned HTTP {response.status_code}")

        payload = json_object(response, "usage endpoint")
        samples = tuple(_parse_usage(payload))
        credits_balance = _parse_credits(payload)
        model_availability = _parse_model_availability(payload)
        if not samples and credits_balance is None and not model_availability:
            raise ProviderError("no quota, credits or model availability in usage response")

        info = {}
        if plan := payload.get("plan_type"):
            info["plan"] = str(plan)
        return ProviderSnapshot(
            samples=samples, info=info, credits_balance=credits_balance,
            model_availability=model_availability,
        )

    def _read_tokens(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.credential_path().read_text())
        except FileNotFoundError as exc:
            raise CredentialsUnavailable(str(exc)) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"unreadable auth.json: {exc}") from exc
        tokens = raw.get("tokens") if isinstance(raw, dict) else None
        if not isinstance(tokens, dict):
            raise CredentialsUnavailable("tokens section missing from auth.json (api-key login?)")
        return tokens


def _parse_rate_limit(rate_limit: Any, scope: str) -> list[QuotaSample]:
    samples: list[QuotaSample] = []
    if not isinstance(rate_limit, dict):
        return samples
    for key, window_name in _WINDOW_NAMES.items():
        if sample := _parse_window(rate_limit.get(key), window_name, scope):
            samples.append(sample)
    return samples


def _parse_window(window: Any, fallback: str, scope: str) -> QuotaSample | None:
    if not isinstance(window, dict):
        return None
    used_percent = window.get("used_percent")
    if not isinstance(used_percent, (int, float)) or isinstance(used_percent, bool):
        return None
    return QuotaSample(
        window=_describe_window(window, fallback),
        scope=scope,
        utilization=used_percent / 100.0,
        resets_at=float(reset_at) if isinstance(reset_at := window.get("reset_at"), (int, float)) else None,
    )


def _parse_usage(payload: dict[str, Any]) -> list[QuotaSample]:
    samples = _parse_rate_limit(payload.get("rate_limit"), "all")
    samples += _parse_rate_limit(payload.get("code_review_rate_limit"), "code_review")
    chatpass = payload.get("chatpass")
    if isinstance(chatpass, dict) and isinstance(windows := chatpass.get("windows"), list):
        for window in windows:
            if sample := _parse_window(window, "chatpass", "chatpass"):
                samples.append(sample)
    for entry in payload.get("additional_rate_limits") or []:
        if not isinstance(entry, dict):
            continue
        scope = _slugify(str(entry.get("limit_name") or entry.get("metered_feature") or "additional"))
        samples += _parse_rate_limit(entry.get("rate_limit"), scope)
    return samples


def _parse_credits(payload: dict[str, Any]) -> float | None:
    credits = payload.get("credits")
    balance = credits.get("balance") if isinstance(credits, dict) else None
    try:
        return float(balance)
    except (TypeError, ValueError):
        return None


def _parse_model_availability(payload: dict[str, Any]) -> dict[str, bool]:
    usage = payload.get("model_usage")
    if not isinstance(usage, dict):
        return {}
    return {
        model: status["available"]
        for model, status in usage.items()
        if isinstance(status, dict) and isinstance(status.get("available"), bool)
    }


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _describe_window(window: dict[str, Any], fallback: str) -> str:
    """Name windows by their reported length (five_hour/seven_day) when available."""
    seconds = window.get("limit_window_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        hours = seconds / 3600
        if hours <= 12:
            return "five_hour" if round(hours) == 5 else f"{round(hours)}h"
        days = round(hours / 24)
        return "seven_day" if days == 7 else f"{days}d"
    return fallback
