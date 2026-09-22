"""A thin, typed-ish client for the Retell REST API.

Only the endpoints this project needs, with error bodies surfaced properly —
Retell returns a JSON `message` on failure and the default httpx repr hides it,
which makes debugging deploys needlessly painful.
"""

from __future__ import annotations

from typing import Any, Iterator

import httpx

BASE_URL = "https://api.retellai.com"


class RetellError(RuntimeError):
    """Raised for any non-2xx response, carrying the API's own message."""

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:600]}")


class RetellClient:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RetellClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RetellError(method, path, response.status_code, response.text)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ---------------------------------------------------------------- voices #
    def list_voices(self) -> list[dict[str, Any]]:
        return self._request("GET", "/list-voices")

    def pick_voice(self, prefer: str | None = None) -> str:
        """Choose a voice, preferring an explicitly requested one.

        Falls back to the first preset English voice so a deploy never fails
        just because a hard-coded voice id was retired.
        """
        voices = self.list_voices()
        if prefer:
            for voice in voices:
                if voice.get("voice_id") == prefer:
                    return prefer
        for voice in voices:
            if (voice.get("provider") in {"elevenlabs", "11labs"}
                    and voice.get("accent") in {"British", "American"}):
                return voice["voice_id"]
        return voices[0]["voice_id"]

    # ----------------------------------------------------------------- flows #
    def create_conversation_flow(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/create-conversation-flow", json=payload)

    def get_conversation_flow(self, flow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/get-conversation-flow/{flow_id}")

    def update_conversation_flow(self, flow_id: str,
                                 payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/update-conversation-flow/{flow_id}",
                             json=payload)

    def delete_conversation_flow(self, flow_id: str) -> None:
        self._request("DELETE", f"/delete-conversation-flow/{flow_id}")

    def list_conversation_flows(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", "/list-conversation-flows",
                             params={"limit": limit})

    # ---------------------------------------------------------------- agents #
    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/create-agent", json=payload)

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/get-agent/{agent_id}")

    def update_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/update-agent/{agent_id}", json=payload)

    def delete_agent(self, agent_id: str) -> None:
        self._request("DELETE", f"/delete-agent/{agent_id}")

    def list_agents(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", "/list-agents", params={"limit": limit})

    # ----------------------------------------------------------------- calls #
    def create_web_call(self, agent_id: str,
                        dynamic_variables: dict[str, str] | None = None,
                        metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"agent_id": agent_id}
        if dynamic_variables:
            body["retell_llm_dynamic_variables"] = dynamic_variables
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", "/v2/create-web-call", json=body)

    def get_call(self, call_id: str) -> dict[str, Any]:
        return self._request("GET", f"/get-call/{call_id}")

    def list_calls(self, limit: int = 50, **filters: Any) -> list[dict[str, Any]]:
        params = {"limit": limit, **filters}
        return self._request("POST", "/v2/list-calls", json=params)

    # -------------------------------------------------------------- testing #
    def create_test_case_definition(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/create-test-case-definition", json=payload)

    def list_test_case_definitions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", "/list-test-case-definitions",
                             params={"limit": limit})

    def delete_test_case_definition(self, case_id: str) -> None:
        self._request("DELETE", f"/delete-test-case-definition/{case_id}")

    def create_batch_test(self, case_ids: list[str],
                          response_engine: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/create-batch-test", json={
            "test_case_definition_ids": case_ids,
            "response_engine": response_engine,
        })

    def get_batch_test(self, batch_id: str) -> dict[str, Any]:
        return self._request("GET", f"/get-batch-test/{batch_id}")

    def list_batch_tests(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._request("GET", "/list-batch-tests", params={"limit": limit})

    def list_test_runs(self, batch_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/list-test-runs/{batch_id}",
                             params={"limit": limit})


def conversation_flow_engine(flow_id: str) -> dict[str, Any]:
    """The `response_engine` object that points an agent at a flow."""
    return {"type": "conversation-flow", "conversation_flow_id": flow_id}


def iter_pages(client: RetellClient, fetcher: str, limit: int = 100) -> Iterator[dict]:
    """Small convenience for listing agents/flows without pagination plumbing."""
    method = getattr(client, fetcher)
    yield from method(limit=limit)
