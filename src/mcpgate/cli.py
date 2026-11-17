"""Demo: two in-process upstreams behind one gateway, with a viewer and an operator client."""
from __future__ import annotations

import sys

from .gateway import ClientPolicy, Gateway, Session
from .upstream import StaticUpstream


def main(argv: list[str] | None = None) -> int:
    ops = StaticUpstream("ops", {
        "kpi": ({"description": "KPI status", "annotations": {"readOnlyHint": True}}, lambda a: "SLA 97%"),
        "approve": ({"description": "Approve a request", "annotations": {"readOnlyHint": False}}, lambda a: "approved"),
        "mystery": ({"description": "no annotations"}, lambda a: "ran"),
    })
    docs = StaticUpstream("docs", {"search": ({"description": "Search docs", "annotations": {"readOnlyHint": True}},
                                              lambda a: "3 results")})
    gw = Gateway([ops, docs], {"analyst": ClientPolicy("viewer", allow=["*"]),
                               "lead": ClientPolicy("operator", allow=["ops__*", "docs__*"], deny=["ops__mystery"])})
    for client in ("analyst", "lead", "stranger"):
        s = Session(gw, client)
        s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        print(f"{client:9} sees:", [t["name"] for t in tools])
    s = Session(gw, "analyst")
    s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ops__approve", "arguments": {}}})
    print("analyst calls ops__approve ->", r["result"]["content"][0]["text"])
    print("audit:", [(a["client"], a["tool"], a["outcome"]) for a in gw.audit])
    return 0


if __name__ == "__main__":
    sys.exit(main())
