"""MCP gateway: one endpoint in front of several MCP servers.

Why: giving an agent direct access to many tool servers means every server must implement its own
auth, limits and logging, and the agent sees every tool. The gateway centralises that:

  * namespacing        tools are exposed as "<server>__<tool>" so names never collide
  * per-client policy  allow/deny patterns and a role; viewers may only call read-only tools
  * default deny       unknown clients see no tools; tools without a readOnlyHint count as writes
  * rate limiting      token bucket per client
  * size limits        on arguments in, and results out
  * audit              every call logged with the argument HASH, never the values
  * isolation          an upstream crash or timeout becomes a tool error, not a gateway failure
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SEP = "__"


class Upstream(Protocol):
    name: str

    def list_tools(self) -> list[dict]: ...
    def call_tool(self, name: str, arguments: dict) -> dict: ...


@dataclass
class ClientPolicy:
    role: str = "viewer"                       # viewer: read-only tools only; operator: reads and writes
    allow: list[str] = field(default_factory=list)   # fnmatch patterns on namespaced tool names
    deny: list[str] = field(default_factory=list)
    rate_per_minute: int = 60

    def __post_init__(self):
        if self.role not in ("viewer", "operator"):
            raise ValueError("role must be 'viewer' or 'operator'")
        if self.rate_per_minute < 1:
            raise ValueError("rate_per_minute must be at least 1")


class TokenBucket:
    def __init__(self, per_minute: int, clock: Callable[[], float]):
        self.capacity, self.rate, self.clock = float(per_minute), per_minute / 60.0, clock
        self.tokens, self.last = float(per_minute), clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class Gateway:
    def __init__(self, upstreams: list[Upstream], policies: dict[str, ClientPolicy],
                 max_arg_bytes: int = 10_000, max_result_chars: int = 20_000,
                 clock: Callable[[], float] = time.time):
        names = [u.name for u in upstreams]
        if len(set(names)) != len(names) or any(SEP in n for n in names):
            raise ValueError("upstream names must be unique and must not contain '__'")
        self.upstreams = {u.name: u for u in upstreams}
        self.policies, self.clock = policies, clock
        self.max_arg_bytes, self.max_result_chars = max_arg_bytes, max_result_chars
        self.buckets = {c: TokenBucket(p.rate_per_minute, clock) for c, p in policies.items()}
        self.audit: list[dict] = []
        self._catalog: dict[str, tuple[str, dict]] | None = None

    # -- catalogue -------------------------------------------------------------
    def catalog(self, refresh: bool = False) -> dict[str, tuple[str, dict]]:
        """namespaced name -> (upstream name, tool descriptor). A failing upstream is skipped."""
        if self._catalog is None or refresh:
            cat = {}
            for up in self.upstreams.values():
                try:
                    tools = up.list_tools()
                except Exception:  # noqa: BLE001 - one broken server must not hide the others
                    continue
                for t in tools:
                    cat[f"{up.name}{SEP}{t['name']}"] = (up.name, t)
            self._catalog = cat
        return self._catalog

    def permitted(self, client: str, ns_name: str, tool: dict) -> tuple[bool, str]:
        pol = self.policies.get(client)
        if pol is None:
            return False, "unknown client"
        if _matches(ns_name, pol.deny):
            return False, "denied by policy"
        if not _matches(ns_name, pol.allow):
            return False, "not in allow-list"
        read_only = bool(tool.get("annotations", {}).get("readOnlyHint", False))  # absent hint = assume write
        if pol.role == "viewer" and not read_only:
            return False, "viewers may only call read-only tools"
        return True, "ok"

    def tools_for(self, client: str) -> list[dict]:
        out = []
        for ns, (_, t) in sorted(self.catalog().items()):
            if self.permitted(client, ns, t)[0]:
                out.append({**t, "name": ns})
        return out

    # -- calling ---------------------------------------------------------------
    def call(self, client: str, ns_name: str, arguments: dict) -> dict:
        t0 = self.clock()
        entry = {"ts": t0, "client": client, "tool": ns_name,
                 "args_hash": hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:16]}

        def done(outcome: str, text: str, is_error: bool) -> dict:
            self.audit.append({**entry, "outcome": outcome, "ms": round((self.clock() - t0) * 1000)})
            return {"content": [{"type": "text", "text": text}], "isError": is_error}

        cat = self.catalog()
        if ns_name not in cat:
            return done("unknown_tool", f"unknown tool: {ns_name}", True)
        ok, why = self.permitted(client, ns_name, cat[ns_name][1])
        if not ok:
            return done("denied", f"not permitted: {why}", True)
        if not self.buckets[client].take():
            return done("rate_limited", "rate limit exceeded; retry shortly", True)
        if len(json.dumps(arguments)) > self.max_arg_bytes:
            return done("too_large", f"arguments exceed {self.max_arg_bytes} bytes", True)
        up_name, tool = cat[ns_name]
        try:
            res = self.upstreams[up_name].call_tool(tool["name"], arguments)
        except Exception as exc:  # noqa: BLE001
            return done("upstream_error", f"upstream '{up_name}' failed: {type(exc).__name__}", True)
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if len(text) > self.max_result_chars:
            text = text[: self.max_result_chars] + "\n[truncated by gateway]"
        self.audit.append({**entry, "outcome": "error" if res.get("isError") else "ok",
                           "ms": round((self.clock() - t0) * 1000)})
        return {"content": [{"type": "text", "text": text}], "isError": bool(res.get("isError"))}


class Session:
    """One connected client. Speaks MCP; every request is evaluated against that client's policy."""

    def __init__(self, gateway: Gateway, client_id: str):
        self.gw, self.client, self.initialized = gateway, client_id, False

    def handle(self, msg) -> dict | None:
        def res(payload):
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": payload}

        def err(code, text):
            return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": code, "message": text}}

        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return err(-32600, "invalid request")
        method = msg["method"]
        if "id" not in msg:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        params = msg.get("params") or {}
        if method == "initialize":
            v = params.get("protocolVersion")
            return res({"protocolVersion": v if v in VERSIONS else VERSIONS[0],
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "mcpgate", "version": "0.1.0"}})
        if method == "ping":
            return res({})
        if not self.initialized:
            return err(-32600, "server not initialized")
        if method == "tools/list":
            return res({"tools": self.gw.tools_for(self.client)})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                return err(-32602, "name must be a string and arguments an object")
            return res(self.gw.call(self.client, name, args))
        return err(-32601, f"unknown method: {method}")
