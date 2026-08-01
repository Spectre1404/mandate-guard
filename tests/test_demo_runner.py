"""Demo runner wiring — the `--prava` run-scoped override.

The safety property under test: `fake` is the default, and choosing `real` is a
per-invocation decision that nothing can inherit by accident. A test that
accidentally hit the sandbox would spend unpublished quota.
"""

import pytest

from backend.demo import build_prava, run_scenario


def test_fake_is_the_default_and_points_at_no_network():
    client, label, teardown = build_prava("fake")
    try:
        assert client.base_url == ""
        assert "fake_prava" in label
        # A TestClient transport, not a requests.Session: no socket can be opened.
        assert client.session.__class__.__name__ == "TestClient"
    finally:
        teardown()


def test_run_scenario_defaults_to_fake():
    import inspect

    assert inspect.signature(run_scenario).parameters["prava"].default == "fake"


def test_real_mode_reads_the_configured_base_url_without_calling_it(monkeypatch):
    monkeypatch.setenv("PRAVA_BASE_URL", "https://sandbox.api.prava.space")
    monkeypatch.setenv("PRAVA_SECRET_KEY", "sk_test_not_a_real_key")

    from backend import config

    config.settings.cache_clear()
    try:
        client, label, teardown = build_prava("real")
        assert client.base_url == "https://sandbox.api.prava.space"
        assert "REAL SANDBOX" in label
        # A real requests.Session -- but nothing has been sent.
        assert client.session.__class__.__name__ == "Session"
        teardown()
    finally:
        config.settings.cache_clear()


def test_an_unknown_prava_mode_is_refused():
    with pytest.raises(ValueError, match="unknown prava mode"):
        build_prava("staging")


def test_blocked_against_real_is_refused_rather_than_silently_pointless():
    """A gate FAIL never reaches Prava, so pointing it at the sandbox proves nothing."""
    with pytest.raises(ValueError, match="never calls Prava"):
        run_scenario("blocked", out_dir="/tmp/unused", prava="real")


def test_unknown_scenario_is_refused():
    with pytest.raises(ValueError, match="unknown scenario"):
        run_scenario("chaos", out_dir="/tmp/unused")
