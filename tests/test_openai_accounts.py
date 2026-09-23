"""Exercise account routing and stale-data removal across polling and exposition."""

import json
import time

import httpx
import pytest
from prometheus_client import CollectorRegistry, generate_latest

from llm_quota_exporter.cli import main
from llm_quota_exporter.metrics import QuotaCollector
from llm_quota_exporter.poller import Poller, ProviderState
from llm_quota_exporter.providers.openai_codex import OpenAICodexProvider


def write_auth(directory, account, token):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth.json"
    path.write_text(json.dumps({"tokens": {"account_id": account, "access_token": token}}))
    return path


def usage(percent):
    return {"rate_limit": {"primary_window": {
        "used_percent": percent, "limit_window_seconds": 604800, "reset_at": 2000000000,
    }}}


def exposition(poller):
    registry = CollectorRegistry()
    registry.register(QuotaCollector(poller))
    return generate_latest(registry).decode()


@pytest.mark.parametrize("count", [1, 3, 8])
def test_named_accounts_override_environment_and_symlink(tmp_path, monkeypatch, capsys, count):
    accounts = {f"account-{i}": tmp_path / f"config-{i}" for i in range(count)}
    percentages = {name: 10 * i for i, name in enumerate(accounts)}
    for name, directory in accounts.items():
        write_auth(directory, f"{name}-id", f"{name}-token")
    default = tmp_path / "unselected"
    write_auth(default, "unselected-id", "unselected-token")
    (tmp_path / ".codex").symlink_to(default)
    monkeypatch.setenv("CODEX_HOME", str(default))
    requests = []

    def respond(request):
        requests.append(request)
        account = request.headers["ChatGPT-Account-Id"].removesuffix("-id")
        assert request.method == "GET"
        assert str(request.url) == "https://chatgpt.com/backend-api/wham/usage"
        assert request.headers["Authorization"] == f"Bearer {account}-token"
        return httpx.Response(200, json=usage(percentages[account]))

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("llm_quota_exporter.cli.httpx.Client", lambda **kwargs: client)
    args = ["--once", "--providers", "openai", "--home", str(tmp_path)]
    for name, directory in accounts.items():
        args.extend(["--openai-account", f"{name}={directory}"])
    assert main(args) == 0
    metrics = capsys.readouterr().out
    for name, percent in percentages.items():
        assert f'provider="openai-{name}",scope="all",window="seven_day"}} {percent / 100}' in metrics
    assert 'provider="openai"' not in metrics
    assert "unselected" not in metrics
    assert len(requests) == count


@pytest.mark.parametrize("failure", [401, 500, "missing", "malformed"])
def test_failed_poll_hides_usage_and_recovers_after_reset(tmp_path, failure):
    path = write_auth(tmp_path, "account-a-id", "old-token")
    replies = [httpx.Response(200, json=usage(80)), httpx.Response(200, json=usage(0))]
    if isinstance(failure, int):
        replies.insert(1, httpx.Response(failure))
    tokens = []

    def respond(request):
        assert request.method == "GET"  # No refresh or quota reset requests.
        tokens.append(request.headers["Authorization"])
        return replies.pop(0)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        state = ProviderState(OpenAICodexProvider(tmp_path, client, account="account-a", codex_home=tmp_path))
        poller = Poller([state], interval=300)
        poller.poll_once()
        assert 'window="seven_day"} 0.8' in exposition(poller)
        last_success = state.last_success
        if failure == "missing":
            path.unlink()
        elif failure == "malformed":
            path.write_text("[]")
        poller.poll_once()
        metrics = exposition(poller)
        assert 'llm_quota_scrape_success{provider="openai-account-a"} 0.0' in metrics
        assert 'llm_quota_utilization_ratio{' not in metrics
        assert 'llm_quota_reset_timestamp_seconds{' not in metrics
        assert state.last_success == last_success
        write_auth(tmp_path, "account-a-id", "new-token")
        # A normal retry after any transient-error backoff has elapsed.
        state.last_attempt -= 301
        poller.poll_once()
        assert 'window="seven_day"} 0.0' in exposition(poller)
        assert state.last_error is None
        assert tokens[-1] == "Bearer new-token"
        assert not replies


def test_stalled_poll_expires_metrics(tmp_path):
    write_auth(tmp_path, "id", "token")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=usage(80)))) as client:
        state = ProviderState(OpenAICodexProvider(tmp_path, client, codex_home=tmp_path))
        poller = Poller([state], interval=300)
        poller.poll_once()
        state.last_success = time.time() - 601
        metrics = exposition(poller)
        assert 'llm_quota_utilization_ratio{' not in metrics
        assert 'llm_quota_last_success_timestamp_seconds{' in metrics


def test_account_failure_is_isolated_and_recovers(tmp_path):
    blocked = True

    def respond(request):
        account = request.headers["ChatGPT-Account-Id"]
        if blocked and account == "account-1-id":
            return httpx.Response(401)
        return httpx.Response(200, json=usage(40))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        states = []
        for i in range(3):
            name = f"account-{i}"
            directory = tmp_path / name
            write_auth(directory, f"{name}-id", f"{name}-token")
            states.append(ProviderState(OpenAICodexProvider(
                tmp_path, client, account=name, codex_home=directory
            )))
        poller = Poller(states, interval=300)
        poller.poll_once()
        metrics = exposition(poller)
        for i in (0, 2):
            assert f'provider="openai-account-{i}",scope="all",window="seven_day"}} 0.4' in metrics
        assert 'llm_quota_scrape_success{provider="openai-account-1"} 0.0' in metrics
        assert 'llm_quota_utilization_ratio{provider="openai-account-1"' not in metrics
        blocked = False
        poller.poll_once()
        assert 'provider="openai-account-1",scope="all",window="seven_day"} 0.4' in exposition(poller)


def test_default_path_remains_compatible(tmp_path, monkeypatch):
    with httpx.Client() as client:
        provider = OpenAICodexProvider(tmp_path, client)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        assert provider.credential_path() == tmp_path / ".codex/auth.json"
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
        assert provider.credential_path() == tmp_path / "custom/auth.json"


@pytest.mark.parametrize("args", [
    ["--openai-account", "account-a=relative"],
    ["--openai-account", "account-a"],
    ["--openai-account", "Bad name=/tmp/account-a"],
    ["--openai-account", "account-a=/tmp/one", "--openai-account", "account-a=/tmp/two"],
    ["--openai-account", "account-a=/tmp/one", "--providers", "grok"],
    ["--interval", "0"],
])
def test_invalid_config_fails_before_polling(args):
    with pytest.raises(SystemExit, match="2"):
        main(["--once", *args])
