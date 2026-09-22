"""Where the tool server's public URL comes from.

This precedence is not cosmetic. `build_flow_payload` only emits tools `if
tools_base_url:`, and `update_conversation_flow` is a PATCH — so an unset value
sends `"tools": []`, which is a *present* key and therefore **clears every tool**
from the deployed flow. The function nodes keep their tool references and point
at nothing.

So "no URL configured" must never be the state on a machine that has a named
tunnel configured.
"""

from __future__ import annotations

from frontdesk import config


def _settings(monkeypatch, **env: str):
    for key in ("TOOLS_BASE_URL", "TOOLS_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return config.load_settings(require_api_key=False)


def test_the_named_tunnel_hostname_is_used_when_no_explicit_base_url(monkeypatch):
    """A named tunnel has one stable hostname, which makes it the right default.

    Without this fallback, a machine configured only with
    CLOUDFLARE_TUNNEL_TOKEN + TOOLS_PUBLIC_URL would report an empty
    tools_base_url, and the next bare `cli deploy` would wipe the tools.
    """
    settings = _settings(
        monkeypatch, TOOLS_PUBLIC_URL="https://retell-dms.helix-leads.com"
    )

    assert settings.tools_base_url == "https://retell-dms.helix-leads.com"
    assert settings.tools_enabled is True


def test_an_explicit_base_url_wins_over_the_public_url(monkeypatch):
    """`dev_tunnel.sh` exports the URL it actually opened. For a quick tunnel
    that is a fresh random hostname, and it must beat the configured one —
    otherwise the deploy would point at a hostname nothing is serving."""
    settings = _settings(
        monkeypatch,
        TOOLS_BASE_URL="https://random-words-here.trycloudflare.com",
        TOOLS_PUBLIC_URL="https://retell-dms.helix-leads.com",
    )

    assert settings.tools_base_url == "https://random-words-here.trycloudflare.com"


def test_neither_configured_means_no_tools(monkeypatch):
    """The genuinely unconfigured case, which is what the deploy guard watches
    for. It must be reachable, or the fallback would mask a real misconfiguration.
    """
    settings = _settings(monkeypatch)

    assert settings.tools_base_url == ""
    assert settings.tools_enabled is False


def test_a_trailing_slash_is_normalised(monkeypatch):
    """Tool paths are appended as `/tools/<name>`, so a double slash here would
    produce `//tools/...` in every deployed tool URL."""
    settings = _settings(monkeypatch, TOOLS_PUBLIC_URL="https://tools.example.com/")

    assert settings.tools_base_url == "https://tools.example.com"
