"""Runtime configuration, loaded from the environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / ".frontdesk.json"


def _load_dotenv() -> None:
    """Minimal .env loader so the demo has no hard dependency on python-dotenv."""
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment wins over the file.
        os.environ.setdefault(key, value)


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_key: str
    tools_base_url: str
    dealer_id: str
    shared_secret: str

    @property
    def tools_enabled(self) -> bool:
        return bool(self.tools_base_url)

    @property
    def tool_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.shared_secret:
            headers["X-Frontdesk-Secret"] = self.shared_secret
        return headers


def load_settings(require_api_key: bool = True) -> Settings:
    api_key = os.environ.get("RETELL_API_KEY", "").strip()
    if require_api_key and not api_key:
        raise SystemExit(
            "RETELL_API_KEY is not set.\n"
            "Add it to .env (see .env.example) or export it in your shell."
        )

    # TOOLS_BASE_URL wins when set, because `dev_tunnel.sh` exports the URL it
    # actually opened (which for a quick tunnel is a fresh hostname).
    #
    # Otherwise fall back to TOOLS_PUBLIC_URL — the stable hostname of a named
    # tunnel. Without this, nothing is set, and a bare `cli deploy` sends
    # "tools": [] and clears every tool from the flow, leaving the function
    # nodes pointing at nothing. The named tunnel's hostname is exactly the
    # value that should be the default, and having a default also removes the
    # prior footgun of a *stale* TOOLS_BASE_URL outliving the tunnel it named.
    base = (
        os.environ.get("TOOLS_BASE_URL", "").strip()
        or os.environ.get("TOOLS_PUBLIC_URL", "").strip()
    )
    return Settings(
        api_key=api_key,
        tools_base_url=base.rstrip("/"),
        dealer_id=os.environ.get("DEALER_ID", "northgate-uk").strip(),
        shared_secret=os.environ.get("TOOLS_SHARED_SECRET", "").strip(),
    )
