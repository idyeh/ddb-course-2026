"""A client that keeps request identifiers, retries, and unknown results honest.

The point of this class in the course is the third status: a timeout is `info`
(unknown), never `fail`. The request may have committed and only the reply was
lost.
"""

from __future__ import annotations

from typing import Any

from ..kernel import Kernel
from ..network import Network
from ..trace import Trace
from .kv import label


class Client:
    def __init__(
        self,
        client_id: str,
        kernel: Kernel,
        trace: Trace,
        network: Network,
        timeout: int = 220,
        max_redirects: int = 3,
    ) -> None:
        self.id = client_id
        self.kernel = kernel
        self.trace = trace
        self.network = network
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.history: list[dict[str, Any]] = []
        self._open: dict[str, dict[str, Any]] = {}  # request_id -> attempt
        trace.add_client(client_id)
        network.attach(client_id, self.receive)

    def submit(self, command: dict[str, Any], target: str) -> None:
        request_id = command["reqId"]
        attempt = {
            "opId": command["opId"],
            "reqId": request_id,
            "target": target,
            "calledAt": self.kernel.now,
            "redirects": 0,
            "resolved": False,
            "op": label(command),
            "command": command,
        }
        self._open[request_id] = attempt
        self.history.append(attempt)
        self.trace.record(self.kernel.now, "client.call", client=self.id,
                          opId=command["opId"], reqId=request_id, op=label(command),
                          target=target)
        self.network.send(self.id, target, {"type": "ClientRequest", "command": command,
                                            "opId": command["opId"], "reqId": request_id},
                          label=label(command))
        self.kernel.schedule(self.timeout, lambda: self._on_timeout(request_id))

    def receive(self, source: str, message: dict[str, Any]) -> None:
        if message.get("type") != "ClientReply":
            return
        attempt = self._open.get(message.get("reqId"))
        if attempt is None or attempt["resolved"]:
            return

        if message["status"] == "redirect":
            hint = message.get("leader")
            if hint and attempt["redirects"] < self.max_redirects:
                attempt["redirects"] += 1
                attempt["target"] = hint
                self.trace.record(self.kernel.now, "client.redirect", client=self.id,
                                  opId=attempt["opId"], reqId=attempt["reqId"],
                                  **{"from": source}, to=hint)
                self.network.send(self.id, hint,
                                  {"type": "ClientRequest", "command": attempt["command"],
                                   "opId": attempt["opId"], "reqId": attempt["reqId"]},
                                  label=attempt["op"])
            return

        self._resolve(attempt, "ok", result=message.get("result"),
                      index=message.get("index"), acked_locally=message.get("ackedLocally", False),
                      deduped=message.get("deduped", False))

    def _on_timeout(self, request_id: str) -> None:
        attempt = self._open.get(request_id)
        if attempt is None or attempt["resolved"]:
            return
        # Unknown, not failed. The write may already be committed.
        self._resolve(attempt, "info", detail="超时：结果未知 (unknown)")

    def _resolve(self, attempt: dict[str, Any], status: str, **fields: Any) -> None:
        attempt["resolved"] = True
        attempt["status"] = status
        attempt["returnedAt"] = self.kernel.now
        attempt.update(fields)
        self._open.pop(attempt["reqId"], None)
        self.trace.record(self.kernel.now, "client.return", client=self.id,
                          opId=attempt["opId"], reqId=attempt["reqId"], op=attempt["op"],
                          status=status, calledAt=attempt["calledAt"],
                          **{key: value for key, value in fields.items() if value is not None})

    def outcomes(self) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in attempt.items() if key != "command"}
            for attempt in self.history
        ]
