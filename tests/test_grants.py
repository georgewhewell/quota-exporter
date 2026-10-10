"""Banked rate-limit resets (OpenAI) and prepaid/promo credits (Anthropic)."""

import json
import time

import httpx
from prometheus_client import CollectorRegistry, generate_latest

from llm_quota_exporter._time import parse_iso8601
from llm_quota_exporter.metrics import QuotaCollector
from llm_quota_exporter.poller import Poller, ProviderState
from llm_quota_exporter.providers.anthropic import (
    PREPAID_CREDITS_URL,
    AnthropicProvider,
    _parse_prepaid,
)
from llm_quota_exporter.providers.anthropic import USAGE_URL as ANTHROPIC_USAGE_URL
from llm_quota_exporter.providers.openai_codex import (
    RESET_CREDITS_URL,
    USAGE_URL,
    OpenAICodexProvider,
    _parse_reset_credits,
)

# Shapes captured from the live endpoints (2026-10), identifiers anonymised.
CODEX_USAGE = {
    "plan_type": "pro",
    "rate_limit": {
        "allowed": False,
        "limit_reached": True,
        "primary_window": {
            "used_percent": 100, "limit_window_seconds": 604800,
            "reset_after_seconds": 308450, "reset_at": 1791948529,
        },
        "secondary_window": None,
    },
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 2},
}

RESET_CREDITS = {
    "credits": [
        {
            "id": "RateLimitResetCredit_aaa", "reset_type": "codex_rate_limits",
            "is_supported_by_plan": True, "status": "available",
            "granted_at": "2026-09-29T18:55:16.766474Z", "expires_at": "2026-10-29T18:55:16.766474Z",
            "redeem_started_at": None, "redeemed_at": None, "title": "Full reset",
        },
        {
            "id": "RateLimitResetCredit_bbb", "reset_type": "codex_rate_limits",
            "is_supported_by_plan": True, "status": "available",
            "granted_at": "2026-10-07T23:26:33.838468Z", "expires_at": "2026-11-06T23:26:33.838468Z",
            "redeem_started_at": None, "redeemed_at": None, "title": "Full reset",
        },
        {
            "id": "RateLimitResetCredit_ccc", "reset_type": "codex_rate_limits",
            "status": "redeemed", "granted_at": "2026-09-01T00:00:00Z",
            "expires_at": "2026-10-01T00:00:00Z", "redeemed_at": "2026-09-15T00:00:00Z",
        },
    ],
    "available_count": 2,
    "total_earned_count": 0,
}


def exposition(provider, snapshot):
    now = time.time()
    state = ProviderState(provider, snapshot=snapshot, last_attempt=now, last_success=now)
    poller = Poller([state], interval=60)
    registry = CollectorRegistry()
    registry.register(QuotaCollector(poller))
    return generate_latest(registry).decode()


def codex_provider(tmp_path, respond):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "tok", "account_id": "acct"}})
    )
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return OpenAICodexProvider(tmp_path, client)


class TestCodexResets:
    def test_parse_keeps_only_available_credits(self):
        count, grants = _parse_reset_credits(RESET_CREDITS)
        assert count == 2
        assert [g.id for g in grants] == ["aaa", "bbb"]
        assert grants[0].kind == "rate_limit_reset"
        assert grants[0].expires_at == parse_iso8601("2026-10-29T18:55:16.766474Z")
        assert grants[0].granted_at == parse_iso8601("2026-09-29T18:55:16.766474Z")
        assert grants[0].remaining is None

    def test_missing_count_falls_back_to_listed_credits(self):
        payload = {"credits": RESET_CREDITS["credits"]}
        count, grants = _parse_reset_credits(payload)
        assert count == len(grants) == 2

    def test_fetch_lists_resets_and_exports_them(self, tmp_path):
        urls = []

        def respond(request):
            urls.append(str(request.url))
            assert request.method == "GET"
            assert request.headers["ChatGPT-Account-Id"] == "acct"
            if str(request.url) == USAGE_URL:
                return httpx.Response(200, json=CODEX_USAGE)
            assert str(request.url) == RESET_CREDITS_URL
            return httpx.Response(200, json=RESET_CREDITS)

        provider = codex_provider(tmp_path, respond)
        snapshot = provider.fetch()
        assert urls == [USAGE_URL, RESET_CREDITS_URL]
        assert snapshot.resets_available == 2
        assert len(snapshot.grants) == 2

        text = exposition(provider, snapshot)
        assert 'llm_rate_limit_resets_available{provider="openai"} 2.0' in text
        assert 'llm_grant_expiry_timestamp_seconds{id="aaa",kind="rate_limit_reset",provider="openai"}' in text
        assert 'llm_grant_granted_timestamp_seconds{id="bbb",kind="rate_limit_reset",provider="openai"}' in text
        assert "llm_grant_remaining{" not in text

    def test_no_resets_means_no_second_request(self, tmp_path):
        urls = []
        usage = {**CODEX_USAGE, "rate_limit_reset_credits": {"available_count": 0}}

        def respond(request):
            urls.append(str(request.url))
            return httpx.Response(200, json=usage)

        snapshot = codex_provider(tmp_path, respond).fetch()
        assert urls == [USAGE_URL]
        assert snapshot.resets_available == 0
        assert snapshot.grants == ()

    def test_absent_summary_exports_nothing(self, tmp_path):
        usage = {k: v for k, v in CODEX_USAGE.items() if k != "rate_limit_reset_credits"}
        provider = codex_provider(tmp_path, lambda _: httpx.Response(200, json=usage))
        snapshot = provider.fetch()
        assert snapshot.resets_available is None
        assert "llm_rate_limit_resets_available{" not in exposition(provider, snapshot)

    def test_listing_failure_keeps_quota_and_count(self, tmp_path):
        def respond(request):
            if str(request.url) == USAGE_URL:
                return httpx.Response(200, json=CODEX_USAGE)
            return httpx.Response(503)

        snapshot = codex_provider(tmp_path, respond).fetch()
        assert snapshot.samples  # the main reading survives
        assert snapshot.resets_available == 2
        assert snapshot.grants == ()


CLAUDE_USAGE = {"five_hour": {"utilization": 3.0, "resets_at": "2026-10-10T17:00:00+00:00"}}

PREPAID_EMPTY = {
    "amount": 0, "currency": None, "balance": None, "balance_credits": None,
    "tranches": None, "promo_tranches": None, "next_expires_at": None,
}


def future(days):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))


class TestAnthropicPrepaid:
    def test_empty_balance(self):
        assert _parse_prepaid(PREPAID_EMPTY) == (0.0, None, ())

    def test_promo_tranches(self):
        prepaid = {
            "amount": 12345, "currency": "USD", "next_expires_at": future(10),
            "promo_tranches": [
                {"remaining_amount_minor_units": 10000, "currency": "USD",
                 "expires_at": future(10), "name": "Monthly credits"},
                {"remaining_amount_minor_units": 2345, "currency": "USD",
                 "expires_at": future(20), "name": "Monthly credits"},
                {"remaining_amount_minor_units": 0, "currency": "USD", "expires_at": future(5), "name": "spent"},
                {"remaining_amount_minor_units": 500, "currency": "USD", "expires_at": future(-1), "name": "lapsed"},
                {"remaining_amount_minor_units": 700, "currency": "usd", "expires_at": future(3)},
                "garbage",
            ],
        }
        balance, next_expiry, grants = _parse_prepaid(prepaid)
        assert balance == 123.45
        assert next_expiry == parse_iso8601(prepaid["next_expires_at"])
        assert [(g.id, g.remaining, g.currency) for g in grants] == [
            ("monthly_credits", 100.0, "USD"),
            ("monthly_credits_2", 23.45, "USD"),
            ("tranche_4", 7.0, "USD"),
        ]
        assert all(g.kind == "promo_credit" for g in grants)

    def test_zero_decimal_currency(self):
        _, _, (grant,) = _parse_prepaid({"promo_tranches": [
            {"remaining_amount_minor_units": 1500, "currency": "JPY", "expires_at": future(1), "name": "x"},
        ]})
        assert grant.remaining == 1500

    def _provider(self, tmp_path, respond):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "tok", "expiresAt": (time.time() + 3600) * 1000, "subscriptionType": "max",
        }}))
        return AnthropicProvider(tmp_path, httpx.Client(transport=httpx.MockTransport(respond)))

    def test_fetch_exports_prepaid(self, tmp_path):
        prepaid = {"amount": 10000, "currency": "USD", "next_expires_at": future(30), "promo_tranches": [
            {"remaining_amount_minor_units": 10000, "currency": "USD", "expires_at": future(30), "name": "Promo"},
        ]}

        def respond(request):
            assert request.headers["Authorization"] == "Bearer tok"
            if str(request.url) == ANTHROPIC_USAGE_URL:
                return httpx.Response(200, json=CLAUDE_USAGE)
            assert str(request.url) == PREPAID_CREDITS_URL
            return httpx.Response(200, json=prepaid)

        provider = self._provider(tmp_path, respond)
        snapshot = provider.fetch()
        assert snapshot.credits_balance == 100.0
        text = exposition(provider, snapshot)
        assert 'llm_credits_balance{provider="anthropic"} 100.0' in text
        assert 'llm_credits_next_expiry_timestamp_seconds{provider="anthropic"}' in text
        assert 'llm_grant_remaining{currency="USD",id="promo",kind="promo_credit",provider="anthropic"} 100.0' in text
        assert 'llm_grant_expiry_timestamp_seconds{id="promo",kind="promo_credit",provider="anthropic"}' in text

    def test_prepaid_failure_keeps_usage(self, tmp_path):
        def respond(request):
            if str(request.url) == ANTHROPIC_USAGE_URL:
                return httpx.Response(200, json=CLAUDE_USAGE)
            return httpx.Response(403)

        snapshot = self._provider(tmp_path, respond).fetch()
        assert snapshot.samples
        assert snapshot.credits_balance is None
        assert snapshot.grants == ()
