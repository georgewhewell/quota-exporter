# Grafana dashboard

`llm-quota.json` — a dashboard for the metrics this exporter produces.

## Import

1. Grafana → Dashboards → New → Import → upload `llm-quota.json`.
2. When prompted, pick your Prometheus / VictoriaMetrics data source (the
   dashboard exposes it as a `datasource` template variable, so nothing is
   hard-coded).

Or provision it declaratively by dropping the file into a
`grafana-dashboards` provider path.

## Panels

- **Provider tiles** — worst-window utilization per provider, from the latest successful poll; named OpenAI accounts are shown separately.
  Failed polls show no current value. The next-reset tile shows the soonest countdown.
- **Quota windows** — every window (`scope="all"`) as gradient gauge cells,
  sorted by pressure, with human-readable reset countdowns.
- **Model caps** — model-scoped weekly caps and per-feature quotas.
- **Quota history** — utilization over time; solid = weekly, dashed =
  monthly, dotted = 5-hour, one fixed colour per provider.
- **Tokens / sessions** — per-model token totals and cost, populated only if
  you also feed the CLIs' OpenTelemetry metrics into the same store (see the
  top-level README). Safe to delete if you don't.
- **Exporter / Plans** — poll health, data age, and subscription tier.
