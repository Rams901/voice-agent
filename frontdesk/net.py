"""Reachability checks that do not trust the local resolver.

macOS — and several corporate DNS filters — variously negative-cache or block
newly created `*.trycloudflare.com` hostnames. The result is a false negative:
a tunnel that is working perfectly for Retell (which resolves from Cloudflare's
own network) looks dead from the laptop doing the deploy.

That failure mode is bad in both directions. It blocks a deploy that would have
worked, and it points the reader at the tool server when the tool server is
fine. So the check below tries the system resolver first, and on failure resolves
the name through Cloudflare's DNS-over-HTTPS endpoint and connects to that
address directly — keeping the real hostname for SNI and certificate
verification, so TLS still means something.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse
import urllib.request

DOH_ENDPOINT = "https://cloudflare-dns.com/dns-query"


def resolve_public(host: str, timeout: float = 8.0) -> str:
    """Resolve `host` over DoH, bypassing the system resolver entirely.

    Returns "" when the name does not exist at all. That is deliberately a
    different answer from "the local cache is stale", and the caller reports
    the two differently.
    """
    request = urllib.request.Request(
        f"{DOH_ENDPOINT}?name={host}&type=A",
        headers={"Accept": "application/dns-json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answers = json.load(response).get("Answer", [])
    except Exception:
        return ""
    return next((a["data"] for a in answers if a.get("type") == 1), "")


class _PinnedConnection(http.client.HTTPSConnection):
    """Dial a known IP while still verifying the certificate for the real host.

    The naive workaround — rewriting the URL to use the IP — breaks TLS,
    because SNI and the certificate would then be checked against the address
    rather than the name. Splitting the two keeps the guarantee.
    """

    def __init__(self, host: str, address: str, **kwargs: object) -> None:
        super().__init__(host, **kwargs)  # type: ignore[arg-type]
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
        )
        if isinstance(self._context, ssl.SSLContext):
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        else:  # pragma: no cover - only if someone disables TLS verification
            self.sock = sock


def _is_success(status: int) -> bool:
    """Only 2xx counts as reachable.

    Deliberately not "we received an HTTP response". Cloudflare's edge happily
    answers for a hostname it routes but cannot serve — a 530 with error code
    1033 is exactly that, and it arrives over a perfectly good TLS connection.
    Counting it as healthy made the deploy gate believe a tunnel was already
    running, so it skipped starting cloudflared and shipped a URL with nothing
    behind it. A response is not the same as a working endpoint.
    """
    return 200 <= status < 300


def check(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Fetch `url`. Returns (ok, note), where the note says how it got there.

    `ok` is True only for a 2xx. A non-2xx is still reported with its status, so
    the caller can tell "the tunnel is routed but not serving" apart from
    "nothing is listening" — different problems with different fixes.
    """
    import httpx

    try:
        response = httpx.get(url, timeout=timeout)
        return _is_success(response.status_code), (
            f"HTTP {response.status_code} {response.text[:120]}"
        )
    except Exception as direct_error:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        address = resolve_public(host)

        if not address or parsed.scheme != "https":
            return False, str(direct_error)

        try:
            connection = _PinnedConnection(
                host, address, port=parsed.port or 443, timeout=timeout
            )
            connection.request("GET", parsed.path or "/")
            response = connection.getresponse()
            body = response.read(200).decode("utf-8", "replace")
        except Exception as fallback_error:
            return False, f"{direct_error} / {fallback_error}"

        note = f"HTTP {response.status} {body[:120]}"
        if not _is_success(response.status):
            return False, note
        return True, (
            f"{note}\n"
            f"  via public DNS {address} — this machine's resolver did not "
            f"resolve the name, which Retell is unaffected by"
        )


if __name__ == "__main__":  # pragma: no cover - shell entry point
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m frontdesk.net <url>")
    ok, note = check(sys.argv[1])
    print(note)
    raise SystemExit(0 if ok else 1)
