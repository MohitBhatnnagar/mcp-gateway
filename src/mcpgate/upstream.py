"""Upstream adapters: in-process (for tests and embedding) and a real stdio subprocess client."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Callable


class StaticUpstream:
    """In-process upstream: a dict of tool name -> (descriptor, function(arguments) -> str)."""

    def __init__(self, name: str, tools: dict[str, tuple[dict, Callable[[dict], str]]]):
        self.name, self._tools = name, tools

    def list_tools(self) -> list[dict]:
        return [{"name": n, **desc} for n, (desc, _) in self._tools.items()]

    def call_tool(self, name: str, arguments: dict) -> dict:
        try:
            return {"content": [{"type": "text", "text": self._tools[name][1](arguments)}], "isError": False}
        except KeyError:
            return {"content": [{"type": "text", "text": f"no such tool {name}"}], "isError": True}


class StdioUpstream:
    """Speaks MCP to a server subprocess over newline-delimited JSON-RPC.

    A reader thread feeds a queue so requests can time out instead of hanging the gateway.
    """

    def __init__(self, name: str, argv: list[str], timeout: float = 10.0, env: dict | None = None):
        self.name, self.argv, self.timeout, self.env = name, argv, timeout, env
        self._proc: subprocess.Popen | None = None
        self._dead = False
        self._q: queue.Queue = queue.Queue()
        self._id = 0
        self._lock = threading.Lock()

    def _start(self) -> None:
        self._dead = False
        self._proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, env=self.env)
        threading.Thread(target=self._read, daemon=True).start()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "mcpgate", "version": "0.1.0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                continue  # ignore noise on the stream
        self._dead = True
        self._q.put({"_eof": True})  # stdout closed: the process exited, so waiting is pointless

    def _send(self, msg: dict) -> None:
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            try:
                msg = self._q.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError(f"{self.name}: no response to {method}") from None
            if msg.get("_eof"):
                raise ConnectionError(f"{self.name}: process exited")
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "upstream error"))
                return msg["result"]

    def _ensure(self) -> None:
        if self._proc is None or self._dead or self._proc.poll() is not None:
            self._q = queue.Queue()
            self._start()

    def list_tools(self) -> list[dict]:
        with self._lock:
            self._ensure()
            return self._rpc("tools/list", {})["tools"]

    def call_tool(self, name: str, arguments: dict) -> dict:
        with self._lock:
            self._ensure()
            return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
