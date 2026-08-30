"""A dependency-free HTTP server: static files from web/, JSON from /api/.

Binds to the loopback interface only. There is no state on the server side --
every request runs a fresh simulation, which is what keeps a run reproducible.
"""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import registry

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_SWEEP = 60


def run_scenario(scenario_id: str, raw_params: dict[str, Any]) -> dict[str, Any]:
    scenario = registry.get(scenario_id)
    params = scenario.params.resolve(raw_params)
    seed = int(params.get("seed", 0))
    payload = scenario.run(params, seed).to_dict()
    # Legal but questionable settings are reported, not rejected.
    payload["summary"]["advisories"] = scenario.params.advise(params)
    return payload


def compare(scenario_id: str, raw_params: dict[str, Any], axis: str,
            values: list[Any]) -> dict[str, Any]:
    scenario = registry.get(scenario_id)
    if not any(param.name == axis and param.axis for param in scenario.params.params):
        raise ValueError(f"{axis!r} 不是该演示的对比轴")
    sides = []
    for value in values[:2]:
        sides.append(run_scenario(scenario_id, {**raw_params, axis: value}))
    return {"axis": axis, "sides": sides}


def sweep(scenario_id: str, raw_params: dict[str, Any], first: int, count: int) -> dict[str, Any]:
    scenario = registry.get(scenario_id)
    count = max(1, min(int(count), MAX_SWEEP))
    rows = []
    for seed in range(int(first), int(first) + count):
        params = scenario.params.resolve({**raw_params, "seed": seed})
        result = scenario.run(params, seed)
        rows.append({
            "seed": seed,
            "violations": [check.name for check in result.checks if not check.ok],
        })
    failing = [row for row in rows if row["violations"]]
    return {"rows": rows, "checked": len(rows), "failing": len(failing)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter classroom console
        if self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    def end_headers(self) -> None:
        # Nothing is cached: editing a scenario or a view should show up on reload.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path.rstrip("/") == "/api/scenarios":
            self._json({"scenarios": [item.to_dict() for item in registry.all_scenarios()]})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        route = self.path.rstrip("/")
        try:
            if route == "/api/run":
                self._json(run_scenario(payload["scenario"], payload.get("params", {})))
            elif route == "/api/compare":
                self._json(compare(payload["scenario"], payload.get("params", {}),
                                   payload["axis"], payload["values"]))
            elif route == "/api/sweep":
                self._json(sweep(payload["scenario"], payload.get("params", {}),
                                 payload.get("from", 2026), payload.get("count", 20)))
            else:
                self._json({"error": f"未知接口 {route}"}, 404)
        except KeyError as error:
            self._json({"error": f"缺少参数或未知演示：{error}"}, 400)
        except ValueError as error:
            self._json({"error": str(error)}, 400)


def serve(port: int = 8000, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    registry.load_all()
    return ThreadingHTTPServer((host, port), Handler)
