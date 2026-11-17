# mcp-gateway

A **gateway that fronts several MCP servers** behind one endpoint, adding the controls an organisation needs before agents get real tools: namespacing, per-client policy, default deny, rate limits, size limits and an audit trail.

When every agent connects directly to every tool server, each server must do its own authentication, limits and logging, and the agent sees every tool. This gateway centralises that. It speaks MCP on both sides (newline-delimited JSON-RPC over stdio on the upstream side, in-process sessions on the client side) and includes a real subprocess client with timeouts and crash recovery.

Standard library only. 31 tests, including a real stdio subprocess.

```bash
pip install -e .
mcpgate                       # demo: viewer, operator and unknown clients
python -m unittest discover -s tests -v
```

## What it enforces

| Control | Behaviour |
|---|---|
| **Namespacing** | `ops__kpi`, `docs__search`: tools from different servers never collide |
| **Per-client visibility** | A client's `tools/list` shows only tools its policy permits. Unpermitted tools are invisible, not just blocked |
| **Allow and deny patterns** | `fnmatch` patterns on namespaced names; deny always wins |
| **Roles** | `viewer` may call only tools annotated `readOnlyHint: true`. `operator` may also call writes |
| **Default deny** | Unknown clients see and can call nothing. A tool with **no** read-only annotation is treated as a write |
| **Rate limit** | Token bucket per client, refilling over time |
| **Size limits** | Arguments in and results out are capped; truncated results say so |
| **Audit** | One record per call: client, tool, outcome, latency, and a **hash** of the arguments, never the values |
| **Isolation** | An upstream exception, timeout or crash becomes a tool error. A server that fails to list is skipped without hiding the others |

Enforcement happens at **call** time too, not only in listing: a viewer that guesses `ops__approve` is refused and audited (there is a test for exactly that).

## Design decisions

1. **Hide, and also enforce.** Filtering the tool list reduces what the model is tempted to try; checking again on every call is what makes it a security control.
2. **Annotations are claims by the server.** The gateway treats a missing `readOnlyHint` as "write", because trusting an absent hint would let any unannotated tool through to viewers. For servers you do not control, set policy by allow-list as well.
3. **Audit what happened without storing secrets.** Argument hashes let you correlate and detect repeats without logging customer data.
4. **Crash handling in the stdio client.** A reader thread feeds a queue so calls time out rather than hang; end-of-stream is detected immediately so a dead server fails fast and is restarted on the next call. (A test found the first version waiting out its full timeout after a crash.)
5. **Failure isolation over availability tricks.** The gateway does not retry tool calls after a crash, because retrying a write could run it twice.

## Limits

- Client identity is a string handed to `Session`. A real deployment needs authentication (for example tokens or mTLS) before a session is created.
- Policies are static in code. Dynamic policy, per-tool argument validation and human approval for destructive calls are natural next steps (see `agentflow-hitl`).
- stdio upstreams only. Remote servers would need an HTTP transport upstream.
- Tool results are passed through as text; scanning results for injected instructions is not implemented.

Related: [`ops-mcp-server`](https://github.com/MohitBhatnnagar/ops-mcp-server) (a server you could put behind this), [`ai-governance-framework`](https://github.com/MohitBhatnnagar/ai-governance-framework), [`agentic-ai-patterns`](https://github.com/MohitBhatnnagar/agentic-ai-patterns).

## License

MIT
