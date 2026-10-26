"""Local view server: serves the diagram UI and the snapshot it renders from.

Deliberately separate from `tool_server/app.py`. That app is the deterministic
half of the *agent* — Retell calls it mid-conversation, it is public via a
tunnel, and it must stay boring and fast. This app is a local operator tool that
holds your Retell API key. Merging them would put an admin tool on the same
tunnel as production tool traffic.

Why a server at all, rather than a static HTML file like `web/index.html`?

* The Retell API key must never reach the browser. Requests go server-side
  through the same `retell_client` the CLI uses.
* Same-origin means no CORS negotiation with `api.retellai.com`.
* The normalizer is Python (`snapshot.py`), and there is exactly one of it.

Run:
    python -m frontdesk.cli diagram
    python -m frontdesk.viewserver --agent-id agent_xxx --port 8100
"""

from __future__ import annotations

import argparse
import webbrowser
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .config import ROOT
from .retell_client import RetellError
from .snapshot import build_snapshot

WEB_DIR = ROOT / "web" / "diagram"
INDEX_FILE = WEB_DIR / "index.html"

app = FastAPI(title="Frontdesk — diagram", version="0.1.0")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if not INDEX_FILE.exists():
        return HTMLResponse(
            "<h1>web/diagram/index.html is missing</h1>", status_code=500
        )
    return HTMLResponse(INDEX_FILE.read_text())


@app.get("/api/snapshot")
def snapshot(agent_id: str | None = Query(default=None),
             refresh: bool = Query(default=False),
             calls_limit: int = Query(default=50, ge=1, le=1000)) -> Any:
    """The one endpoint. Everything the UI shows comes from here.

    A Retell failure is surfaced as a 502 with the API's own message rather than
    a 500 with a traceback — most failures here are a missing key, a revoked
    key, or an agent id that does not exist, and the UI should say which.
    """
    try:
        return build_snapshot(agent_id, calls_limit=calls_limit, refresh=refresh)
    except RetellError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": str(exc), "status": exc.status, "kind": "retell"},
        )
    except SystemExit as exc:  # resolve_agent_id uses SystemExit for "you choose"
        return JSONResponse(
            status_code=400, content={"error": str(exc), "kind": "config"}
        )


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "web_dir": str(WEB_DIR), "index_present": INDEX_FILE.exists()}


def serve(agent_id: str | None = None, *, host: str = "127.0.0.1",
          port: int = 8100, open_browser: bool = True,
          calls_limit: int = 50) -> None:
    """Start the server. Blocking, so the CLI calls this last."""
    import uvicorn

    url = f"http://{host}:{port}/"
    if agent_id:
        url += f"?agent_id={agent_id}"
    print(f"\n  diagram : {url}")
    print(f"  web dir : {WEB_DIR}")
    if not INDEX_FILE.exists():
        print(f"  \033[31mWARNING\033[0m {INDEX_FILE} not found — the API still works.")
    if open_browser:
        webbrowser.open(url)
    print("\n  Ctrl-C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m frontdesk.viewserver")
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--calls-limit", type=int, default=50)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    serve(args.agent_id, host=args.host, port=args.port,
          open_browser=not args.no_open, calls_limit=args.calls_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
