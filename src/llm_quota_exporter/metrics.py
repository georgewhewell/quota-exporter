"""Prometheus collector translating poller state into metric families."""

from __future__ import annotations

import time
from collections.abc import Iterable

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from .poller import Poller


class QuotaCollector(Collector):
    def __init__(self, poller: Poller) -> None:
        self._poller = poller

    def collect(self) -> Iterable[GaugeMetricFamily]:
        utilization = GaugeMetricFamily(
            "llm_quota_utilization_ratio",
            "Fraction of the quota window consumed (1.0 = limit reached)",
            labels=["provider", "window", "scope"],
        )
        resets_at = GaugeMetricFamily(
            "llm_quota_reset_timestamp_seconds",
            "Unix time at which the quota window resets",
            labels=["provider", "window", "scope"],
        )
        info = GaugeMetricFamily(
            "llm_provider_info",
            "Static subscription details per provider",
            labels=["provider", "plan", "tier"],
        )
        success = GaugeMetricFamily(
            "llm_quota_scrape_success",
            "Whether the most recent poll of this provider succeeded",
            labels=["provider"],
        )
        last_success = GaugeMetricFamily(
            "llm_quota_last_success_timestamp_seconds",
            "Unix time of the last successful poll",
            labels=["provider"],
        )
        duration = GaugeMetricFamily(
            "llm_quota_poll_duration_seconds",
            "Duration of the most recent poll attempt",
            labels=["provider"],
        )
        used = GaugeMetricFamily(
            "llm_quota_used",
            "Absolute quota consumption in provider-native units, where reported",
            labels=["provider", "window", "scope"],
        )
        limit = GaugeMetricFamily(
            "llm_quota_limit",
            "Absolute quota limit in provider-native units, where reported",
            labels=["provider", "window", "scope"],
        )
        spend = GaugeMetricFamily(
            "llm_spend_usd",
            "Extra-usage / overage spend in USD, where reported",
            labels=["provider"],
        )
        credits = GaugeMetricFamily(
            "llm_credits_balance",
            "Remaining prepaid credits in provider-native units, where reported",
            labels=["provider"],
        )
        model_available = GaugeMetricFamily(
            "llm_model_available",
            "Whether the provider currently reports access to a model (independent of quota utilization)",
            labels=["provider", "model"],
        )
        credits_next_expiry = GaugeMetricFamily(
            "llm_credits_next_expiry_timestamp_seconds",
            "Unix time at which the next part of the prepaid credit balance expires, where reported",
            labels=["provider"],
        )
        resets_available = GaugeMetricFamily(
            "llm_rate_limit_resets_available",
            "Banked rate-limit resets that can be redeemed on demand",
            labels=["provider"],
        )
        grant_expiry = GaugeMetricFamily(
            "llm_grant_expiry_timestamp_seconds",
            "Unix time at which an individual grant (banked reset, promo credit) expires",
            labels=["provider", "kind", "id"],
        )
        grant_granted = GaugeMetricFamily(
            "llm_grant_granted_timestamp_seconds",
            "Unix time at which an individual grant was issued",
            labels=["provider", "kind", "id"],
        )
        grant_remaining = GaugeMetricFamily(
            "llm_grant_remaining",
            "Unspent amount of a monetary grant in major currency units",
            labels=["provider", "kind", "id", "currency"],
        )

        with self._poller.lock:
            for state in self._poller.states:
                name = state.provider.name
                if state.last_attempt is not None:
                    success.add_metric([name], 0.0 if state.last_error else 1.0)
                if state.last_success is not None:
                    last_success.add_metric([name], state.last_success)
                if state.last_duration is not None:
                    duration.add_metric([name], state.last_duration)
                # Retain diagnostics on failure, but never present old usage
                # as a current measurement. Also expire a stalled poller's
                # snapshot after two polling intervals (minimum one minute).
                if (
                    state.snapshot is None
                    or state.last_error is not None
                    or state.last_success is None
                    or time.time() - state.last_success > max(60.0, 2 * self._poller.interval)
                ):
                    continue
                snapshot = state.snapshot
                info.add_metric(
                    [name, snapshot.info.get("plan", ""), snapshot.info.get("tier", "")], 1.0
                )
                if snapshot.spend_usd is not None:
                    spend.add_metric([name], snapshot.spend_usd)
                if snapshot.credits_balance is not None:
                    credits.add_metric([name], snapshot.credits_balance)
                for model, available in snapshot.model_availability.items():
                    model_available.add_metric([name, model], float(available))
                if snapshot.credits_next_expiry is not None:
                    credits_next_expiry.add_metric([name], snapshot.credits_next_expiry)
                if snapshot.resets_available is not None:
                    resets_available.add_metric([name], float(snapshot.resets_available))
                seen_grants: set[tuple[str, str]] = set()
                for grant in snapshot.grants:
                    if (grant.kind, grant.id) in seen_grants:
                        continue
                    seen_grants.add((grant.kind, grant.id))
                    grant_labels = [name, grant.kind, grant.id]
                    if grant.expires_at is not None:
                        grant_expiry.add_metric(grant_labels, grant.expires_at)
                    if grant.granted_at is not None:
                        grant_granted.add_metric(grant_labels, grant.granted_at)
                    if grant.remaining is not None:
                        grant_remaining.add_metric([*grant_labels, grant.currency], grant.remaining)
                # Guard against colliding (window, scope) tuples from a
                # provider: a duplicate series makes Prometheus reject the
                # whole scrape, which would take down every other metric too.
                seen: set[tuple[str, str]] = set()
                for sample in snapshot.samples:
                    key = (sample.window, sample.scope)
                    if key in seen:
                        continue
                    seen.add(key)
                    labels = [name, sample.window, sample.scope]
                    utilization.add_metric(labels, sample.utilization)
                    if sample.resets_at is not None:
                        resets_at.add_metric(labels, sample.resets_at)
                    if sample.used is not None:
                        used.add_metric(labels, sample.used)
                    if sample.limit is not None:
                        limit.add_metric(labels, sample.limit)

        return [
            utilization, resets_at, info, success, last_success, duration, used, limit, spend, credits, model_available,
            credits_next_expiry, resets_available, grant_expiry, grant_granted, grant_remaining,
        ]
