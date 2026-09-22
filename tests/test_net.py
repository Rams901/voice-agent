"""Reachability semantics.

The distinction these tests protect is narrow and easy to get wrong: "an HTTP
response arrived" is not "the endpoint works".

It matters because `scripts/dev_tunnel.sh` gates a deploy on this. When any
response counted as healthy, Cloudflare's edge returned a **530 / error 1033**
for a hostname whose tunnel was not running, the script concluded the tunnel was
already up, skipped starting cloudflared, and deployed a URL with nothing behind
it. The gate has to mean 2xx or it is not a gate.
"""

from __future__ import annotations

import httpx
import pytest

from frontdesk import net


class _StubResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_a_2xx_is_reachable(status: int) -> None:
    assert net._is_success(status) is True


@pytest.mark.parametrize("status", [199, 301, 404, 502, 503, 530])
def test_anything_other_than_2xx_is_not_reachable(status: int) -> None:
    assert net._is_success(status) is False


def test_the_cloudflare_edge_error_that_caused_a_dead_deploy(monkeypatch) -> None:
    """The exact response that slipped through: 530 with error code 1033.

    It arrives over a valid TLS connection from Cloudflare's own edge, which is
    precisely why it looked reachable.
    """
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _StubResponse(530, "error code: 1033"),
    )

    ok, note = net.check("https://tools.example.com/health")

    assert ok is False, "a routed-but-not-serving hostname is not healthy"
    assert "HTTP 530" in note, "the status is still reported, only not trusted"


def test_a_healthy_endpoint_is_reachable(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _StubResponse(200, '{"status":"ok"}'))

    ok, note = net.check("https://tools.example.com/health")

    assert ok is True
    assert "HTTP 200" in note


def test_a_not_found_is_reported_not_reachable(monkeypatch) -> None:
    """A 404 means something answered but not what we asked for — the tool
    server is not mounted there. Distinct from a healthy endpoint."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _StubResponse(404, "Not Found"))

    ok, note = net.check("https://tools.example.com/health")

    assert ok is False
    assert "HTTP 404" in note
