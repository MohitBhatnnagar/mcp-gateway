import os
import sys
import unittest

from mcpgate import ClientPolicy, Gateway, Session, StaticUpstream, StdioUpstream, TokenBucket

HERE = os.path.dirname(__file__)
RO = {"readOnlyHint": True}


def upstreams():
    ops = StaticUpstream("ops", {
        "kpi": ({"description": "KPI", "annotations": RO}, lambda a: "SLA 97%"),
        "approve": ({"description": "Approve", "annotations": {"readOnlyHint": False}}, lambda a: "approved"),
        "mystery": ({"description": "unannotated"}, lambda a: "ran"),
        "big": ({"description": "big output", "annotations": RO}, lambda a: "x" * 50_000),
        "boom": ({"description": "fails", "annotations": RO}, lambda a: 1 / 0),
    })
    docs = StaticUpstream("docs", {"search": ({"description": "Search", "annotations": RO}, lambda a: "3 results")})
    return [ops, docs]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = kw.pop("clock", Clock())
    pols = kw.pop("policies", {
        "analyst": ClientPolicy("viewer", allow=["*"]),
        "lead": ClientPolicy("operator", allow=["ops__*", "docs__*"], deny=["ops__mystery"]),
        "narrow": ClientPolicy("operator", allow=["docs__*"]),
    })
    return Gateway(kw.pop("ups", upstreams()), pols, clock=clock, **kw), clock


def session(gw, client):
    s = Session(gw, client)
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return s


def call(s, name, args=None):
    return s.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                     "params": {"name": name, "arguments": args or {}}})["result"]


def names(s):
    return {t["name"] for t in s.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})["result"]["tools"]}


class VisibilityTests(unittest.TestCase):
    def test_tools_are_namespaced(self):
        gw, _ = make()
        self.assertIn("ops__kpi", names(session(gw, "lead")))

    def test_viewer_sees_only_read_only_tools(self):
        n = names(session(make()[0], "analyst"))
        self.assertEqual(n, {"ops__kpi", "ops__big", "ops__boom", "docs__search"})

    def test_unannotated_tool_is_treated_as_a_write(self):
        n = names(session(make()[0], "analyst"))
        self.assertNotIn("ops__mystery", n)

    def test_operator_sees_writes_but_deny_wins(self):
        n = names(session(make()[0], "lead"))
        self.assertIn("ops__approve", n)
        self.assertNotIn("ops__mystery", n)

    def test_allow_list_restricts_servers(self):
        self.assertEqual(names(session(make()[0], "narrow")), {"docs__search"})

    def test_unknown_client_sees_nothing(self):
        self.assertEqual(names(session(make()[0], "stranger")), set())


class EnforcementTests(unittest.TestCase):
    def test_viewer_cannot_call_a_write_tool_even_if_it_guesses_the_name(self):
        gw, _ = make()
        r = call(session(gw, "analyst"), "ops__approve")
        self.assertTrue(r["isError"])
        self.assertIn("read-only", r["content"][0]["text"])
        self.assertEqual(gw.audit[-1]["outcome"], "denied")

    def test_operator_can_call_write_tool(self):
        self.assertEqual(call(session(make()[0], "lead"), "ops__approve")["content"][0]["text"], "approved")

    def test_denied_tool_cannot_be_called(self):
        self.assertTrue(call(session(make()[0], "lead"), "ops__mystery")["isError"])

    def test_not_in_allow_list(self):
        self.assertIn("allow-list", call(session(make()[0], "narrow"), "ops__kpi")["content"][0]["text"])

    def test_unknown_client_cannot_call(self):
        gw, _ = make()
        r = call(session(gw, "stranger"), "ops__kpi")
        self.assertTrue(r["isError"])
        self.assertIn("unknown client", r["content"][0]["text"])
        self.assertEqual(gw.audit[-1]["outcome"], "denied")

    def test_unknown_tool(self):
        self.assertIn("unknown tool", call(session(make()[0], "lead"), "ops__nope")["content"][0]["text"])

    def test_allowed_call_returns_result(self):
        self.assertEqual(call(session(make()[0], "analyst"), "docs__search")["content"][0]["text"], "3 results")


class LimitTests(unittest.TestCase):
    def test_rate_limit_per_client(self):
        gw, clock = make(policies={"c": ClientPolicy("viewer", allow=["*"], rate_per_minute=3)})
        s = session(gw, "c")
        outcomes = [call(s, "docs__search")["isError"] for _ in range(5)]
        self.assertEqual(outcomes, [False, False, False, True, True])
        self.assertEqual(gw.audit[-1]["outcome"], "rate_limited")

    def test_rate_limit_refills_over_time(self):
        gw, clock = make(policies={"c": ClientPolicy("viewer", allow=["*"], rate_per_minute=60)})
        s = session(gw, "c")
        for _ in range(60):
            call(s, "docs__search")
        self.assertTrue(call(s, "docs__search")["isError"])
        clock.t += 2  # one token per second
        self.assertFalse(call(s, "docs__search")["isError"])

    def test_clients_have_separate_buckets(self):
        gw, _ = make(policies={"a": ClientPolicy("viewer", allow=["*"], rate_per_minute=1),
                               "b": ClientPolicy("viewer", allow=["*"], rate_per_minute=1)})
        call(session(gw, "a"), "docs__search")
        self.assertTrue(call(session(gw, "a"), "docs__search")["isError"])
        self.assertFalse(call(session(gw, "b"), "docs__search")["isError"])

    def test_token_bucket_never_exceeds_capacity(self):
        clock = Clock()
        b = TokenBucket(2, clock)
        clock.t += 10_000
        self.assertEqual([b.take() for _ in range(3)], [True, True, False])

    def test_argument_size_cap(self):
        gw, _ = make(max_arg_bytes=100)
        r = call(session(gw, "analyst"), "docs__search", {"q": "x" * 500})
        self.assertIn("exceed", r["content"][0]["text"])

    def test_result_truncation(self):
        gw, _ = make(max_result_chars=1000)
        text = call(session(gw, "analyst"), "ops__big")["content"][0]["text"]
        self.assertTrue(text.endswith("[truncated by gateway]"))
        self.assertLess(len(text), 1100)

    def test_policy_validation(self):
        with self.assertRaises(ValueError):
            ClientPolicy("admin")
        with self.assertRaises(ValueError):
            ClientPolicy("viewer", rate_per_minute=0)


class AuditAndIsolationTests(unittest.TestCase):
    def test_audit_has_hash_not_values(self):
        gw, _ = make()
        call(session(gw, "analyst"), "docs__search", {"q": "SECRET-TERM-42"})
        self.assertNotIn("SECRET-TERM-42", str(gw.audit))
        self.assertEqual(len(gw.audit[-1]["args_hash"]), 16)

    def test_same_args_same_hash_different_args_different_hash(self):
        gw, _ = make()
        s = session(gw, "analyst")
        call(s, "docs__search", {"q": "a"}); call(s, "docs__search", {"q": "a"}); call(s, "docs__search", {"q": "b"})
        h = [a["args_hash"] for a in gw.audit]
        self.assertEqual((h[0] == h[1], h[0] == h[2]), (True, False))

    def test_upstream_exception_becomes_tool_error_and_is_audited(self):
        gw, _ = make()
        r = call(session(gw, "analyst"), "ops__boom")
        self.assertTrue(r["isError"])
        self.assertIn("ZeroDivisionError", r["content"][0]["text"])
        self.assertEqual(gw.audit[-1]["outcome"], "upstream_error")

    def test_broken_upstream_does_not_hide_the_others(self):
        class Broken:
            name = "broken"

            def list_tools(self):
                raise ConnectionError()
        gw, _ = make(ups=upstreams() + [Broken()])
        self.assertIn("docs__search", names(session(gw, "analyst")))

    def test_upstream_names_validated(self):
        a = StaticUpstream("same", {})
        with self.assertRaises(ValueError):
            Gateway([a, StaticUpstream("same", {})], {})
        with self.assertRaises(ValueError):
            Gateway([StaticUpstream("bad__name", {})], {})


class ProtocolTests(unittest.TestCase):
    def test_requires_initialize(self):
        s = Session(make()[0], "analyst")
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r["error"]["code"], -32600)

    def test_initialize_negotiates_version(self):
        s = Session(make()[0], "analyst")
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")

    def test_notifications_get_no_response(self):
        self.assertIsNone(Session(make()[0], "analyst").handle({"jsonrpc": "2.0", "method": "notifications/x"}))

    def test_bad_messages(self):
        s = session(make()[0], "analyst")
        self.assertEqual(s.handle({"id": 1, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"], -32601)
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 5}})
        self.assertEqual(r["error"]["code"], -32602)


class StdioUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.up = StdioUpstream("echo", [sys.executable, os.path.join(HERE, "echo_server.py")], timeout=10)

    def tearDown(self):
        self.up.close()

    def test_lists_and_calls_a_real_subprocess(self):
        gw, _ = make(ups=[self.up], policies={"c": ClientPolicy("viewer", allow=["*"])})
        s = session(gw, "c")
        self.assertEqual(names(s), {"echo__echo"})  # 'crash' has no readOnlyHint, so a viewer cannot see it
        self.assertEqual(call(s, "echo__echo", {"text": "hi"})["content"][0]["text"], "echo: hi")

    def test_crashed_upstream_is_an_error_then_restarts(self):
        gw, _ = make(ups=[self.up], policies={"op": ClientPolicy("operator", allow=["*"])})
        s = session(gw, "op")
        r = call(s, "echo__crash")
        self.assertTrue(r["isError"])
        self.assertEqual(call(s, "echo__echo", {"text": "again"})["content"][0]["text"], "echo: again")


if __name__ == "__main__":
    unittest.main()
