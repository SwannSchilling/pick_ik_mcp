# MCP server design — `pick_ik_mcp` (Phase 1, for sign-off)

Status: **design, no code yet.** The add-on side (`mcp_protocol.py`, `mcp_bridge.py`,
`mcp_handlers_obs.py`, prefs + panel) is implemented and green; this is the remaining Phase 1 half.
Contract source: `blender_ik_addon/MCP_INTEGRATION_PLAN.md` §4 (wire), §5 (classes), §6 (catalogue),
§7 (gates), §9 (security), §12/§13/§14 (phasing, config, decisions).

## 1. Shape

Two processes, one direction of call.

```
LLM agent  <->  pick_ik_mcp/mcp_server.py   <->  mcp_bridge.py inside Blender
 (MCP/stdio)     system Python, imports `mcp`      Blender's Python, imports `bpy`
                 NEVER imports bpy                   NEVER imports mcp
```

The server is a *thin, honest proxy*: it maps MCP tool calls onto wire frames and back. It holds no
IK logic, no limits, no model — everything numeric stays in `pick_ik_c.dll` behind the bridge, so the
server cannot drift from the thing that actually moves the arm.

`mcp` and `bpy` **MUST NOT** co-exist in one interpreter (§2.1): `mcp` pulls `anyio`/`pydantic`, and a
pydantic model that re-validates an incoming frame must never be able to reach a `bpy` RNA pointer.
The socket is the only coupling.

## 2. Discovery, and what wins when the two sources disagree

The server reads `runtime_file` (`~/.pickik/bridge.json`), written by the bridge on bind:

| field | use |
|---|---|
| `host`, `port` | where to connect. **Overrides** `config.host`/`config.port` |
| `proto_rev` | compared to the server's own **before** connecting |
| `token` | the `hello.auth` value |
| `pid`, `blender`, `started_at` | staleness report and the operator's "is this the session I think it is" |

Precedence, deliberately: **the live runtime record wins over static config** for host/port/token.
Config is an intent; the runtime file is what a running bridge actually bound. A stale file is the
way an agent ends up driving the wrong session, so:

- no runtime file ⇒ fail with "start the bridge: PickIK panel > MCP bridge > Start", never a guess.
- `pid` not alive ⇒ say the record is stale and refuse (do not silently connect to a port that has
  been taken over by something else).
- `proto_rev` differs ⇒ refuse **before** connecting, naming both revisions. The bridge also refuses
  at the handshake; failing one step earlier means the operator learns it from a readable message
  instead of from a tool error inside the agent's turn.
- `host` not loopback ⇒ refuse outright (§9: loopback only, no exceptions, no config override).

## 3. Vendoring and the drift guard

`vendored/mcp_protocol.py` is a byte copy of the add-on's file; `proto_rev()` is a hash of the
command catalogue + schemas, so drift is a *number*, not a memory.

Three guards, cheapest first:
1. **At connect** — compare `proto_rev` (above). Catches operator-side drift.
2. **At server start (dev mode only)** — if the add-on tree is reachable, compare the two files'
   hashes and warn on inequality. Catches "vendored copy not refreshed after a contract change".
3. **In the gate suite** — `test_mcp_bridge.py` pins `import mcp_protocol` to the **add-on** copy, so
   a test can never pass by testing the stale vendored one (§3, gate 14).

## 4. Tool mapping

One tool per Phase 1 command, names from §6.1/§6.2 with the `pickik_` prefix, `cmd` carried in the tool
metadata. Input schema is derived from the same catalogue the bridge enforces — the server does not
own a second copy of the argument rules, it *mirrors* them for the agent's benefit and lets the bridge
be the authority. Unknown `cmd` is refused by the bridge (`E_INVAL`), never name-resolved (gate 15).

The tool list *is* the catalogue, so the table below is generated from it rather than typed into it:
`mcp_docs.py` writes the block, and `tests/test_mcp_bridge.py` fails the suite if this file and the
catalogue ever disagree — a hand-copied table of numbers can be wrong in exactly the way a reader
cannot see. The server exposes one tool per command it answers, named `pickik_<cmd>`; the prefix is
presentation-layer only, the wire still carries the bare §6 `cmd`, and §6's tables stay valid as
written. The 14 Phase 1 tools are the rows marked `yes` below.

<!-- CATALOGUE-BEGIN:tools -->

| cmd | class | exec | gate | answered here |
|---|---|---|---|---|
| `build_rig` | write | tick | — | yes |
| `delete_rig` | write | tick | `confirm` | yes |
| `export_urdf` | write | tick | — | yes |
| `get_robot_info` | read | tick | — | yes |
| `get_state` | read | tick | — | yes |
| `get_target` | read | tick | — | yes |
| `hw_analyze_frame` | pure | worker | — | — |
| `hw_check` | hw | worker | — | — |
| `hw_configure` | write | tick | `confirm` | — |
| `hw_disconnect` | hw | worker | `confirm` | — |
| `hw_get_info` | read | tick | — | — |
| `hw_install` | hw | worker | `confirm` | — |
| `hw_live_start` | hw | worker | `arm` | — |
| `hw_live_stop` | hw | worker | — | — |
| `hw_live_update` | hw | worker | `arm` | — |
| `hw_motors_move` | hw | worker | `arm` | — |
| `hw_motors_set_zero` | hw | worker | `arm` | — |
| `hw_motors_stop` | priority | receiver | — | — |
| `hw_read_telemetry` | hw | worker | — | — |
| `hw_send_frame` | hw | worker | `arm` | — |
| `hw_status` | read | tick | — | — |
| `set_continuous` | write | tick | — | yes |
| `set_joint_angles` | write | tick | — | yes |
| `set_solver` | write | tick | — | yes |
| `set_solver_config` | write | tick | — | yes |
| `set_target` | write | tick | — | yes |
| `solve_ik` | write | tick | — | yes |
| `status` | read | tick | — | yes |
| `validate_pose` | pure | worker | — | yes |

_Generated from `mcp_protocol`: proto_rev `5bc953006c6e`, 29 commands, 14 answered in this build. Nothing between the sentinels is hand-written; edit `mcp_docs.py` or the catalogue instead._

<!-- CATALOGUE-END:tools -->

`solve_ik` is the one command whose class the **arguments** pick (§6, verified against
`P.classify`): `dry_run`+`seed_q` ⇒ `pure`/worker and genuinely concurrent; `dry_run` alone ⇒
`read`/tick, because seeding from `rig.last_q` needs the main thread; `execute` ⇒ `write`/tick;
`execute`+`solver:"memetic"` ⇒ `write` but on the worker, applying on the tick. The server forwards
and never re-decides: a proxy that guessed the class would be able to put a mutating call on the
concurrent lane, which is the one classification error that matters.

**Phase 1 coverage is complete on the add-on side**, and checkable rather than asserted: the catalogue
holds 29 commands, 14 have handlers in this build, and the 15 that do not are precisely the `hw_*`
group — no non-hardware command is left unimplemented. So the server's Phase 1 tool list *is* the
handler list; a tool exposed for an `hw_*` command would only be told by the bridge that it is absent.

Three properties the mapping **MUST** keep, because they are where the safety lives:

1. **Deadlines are carried, not invented.** `spec.deadline_ms` from the catalogue goes into the frame
   as `args["deadline_ms"]`; the server's own socket wait is `deadline_ms + grace` (grace is transport
   only). A tool that waits forever is how a UI-adjacent bridge turns one slow solve into a hung agent.
2. **The server MUST NOT auto-satisfy a gate.** It never injects `confirm=True` or
   `arm=True`/the phrase, and has no "auto-approve" setting. A gate the proxy can silently satisfy is
   not a gate — it is decoration with a name in the schema. The keys are ordinary declared tool
   arguments, so a human in the loop can approve and a model can see exactly what it is asserting.
3. **`E_TIMEOUT` is outcome-unknown, and says so.** The reply carries `retry_safe` from
   `P.OUTCOME_UNKNOWN` and the bridge's own wording ("call get_state to reconcile"); the server passes
   both through and never re-issues a mutating command behind the agent's back.

## 5. Errors, verbatim

`error.code` is the stable §4.5 code and is **preserved** — translated into MCP tool-error content
with the code, message and `retry_safe` in structured fields, not flattened into prose. An agent that
cannot tell `E_RANGE` from `E_INTERNAL` from `E_BUSY` cannot choose correctly between retargeting,
giving up, and waiting, so the taxonomy is the useful part of the answer.

## 6. The three queue/transport decisions the server must not blur

These are bridge properties. They are spelled out here because a proxy that quietly re-implements them
is how a safe system becomes unsafe after the fact — and each is already implemented and gated on the
bridge side, so what the server mostly owes is *not breaking* them.

**(a) The e-stop rides a priority lane, never a queue.** `hw_motors_stop` is dispatched **inline on the
receiver thread** — not enqueued, not busy-checked, never `E_BUSY`, and it works while a write is
parked in the tick; it raises `abort_flag`, and the streaming loops poll that flag rather than being
interrupted (plan §5.3). Proven by gate 16: answered with the pump switched off entirely, off the main
thread, ahead of a parked write, with `pending_mutating=1` and `abort=True`. The Phase 1 server exposes
no `hw_*` tool, so its obligation is narrow and real: the frame it sends is `{id, cmd, args}` and it
**MUST NOT** invent a queue, a retry, or a "busy, try later" of its own — an e-stop the server
re-issued later is an e-stop that did not happen. (The lane and the flag are Phase 1 plumbing precisely
so that Phase 3 does not retrofit a priority path onto a bare FIFO.)

**(b) One authenticated client; a second is refused.** The bridge admits one session; a second `hello`
gets `E_ACCES "a client is already connected"`. Auth is required — token compared constant-time,
generated when unset, `insecure_no_auth` only ever in mutual exclusion with the hardware. Gate 15 pins
all of it: wrong token refused, `proto_rev` drift refused, one client admitted *once*, rate-capped, and
no token ever echoed. So the server holds exactly one socket and **MUST NOT** reconnect silently: a
silent reconnect can let an agent resume a session the bridge has already failed-safe'd. Reconnect is an
operator act, surfaced through `pickik_bridge_status`.

**(c) `E_TIMEOUT` is outcome-unknown, and `get_state` is the stated recovery.** It does not mean "the
move did not happen"; it means "the answer is not knowable from here" (`P.OUTCOME_UNKNOWN`, plan §5).
The bridge's own message says *call `get_state` to reconcile*, the reply carries `retry_safe`, and the
server passes both through unchanged and **MUST NOT** re-issue a mutating command behind the agent's
back. The distinction is the entire value: an agent that reads a timeout as "nothing happened" will
retry an action whose effect it cannot see.

**(d) The queue drains to a budget, not to a count.** The tick drains while the elapsed time since the
drain began is under `tick_budget_ms`, and the budget is measured on `perf_counter` (§5.5), so the tick
stays responsive whatever the queue depth. The server's own socket wait is `deadline_ms + grace` and is
transport only: it is not a second budget, and it must never be the thing that decides how much work the
main thread takes. Gate 16 measures the drain entry-to-exit and shows `[4,4,4]` jobs per tick against a
4 ms budget, with the slowest tick at ~4.6 ms while a 58 ms solve runs off-thread.

## 7. What the server does NOT do in Phase 1

- **Not start or stop the user's bridge.** It connects, or it tells you to press Start. An agent that
  can open a listening socket is a worse posture than one that can only use an open one; a
  `pickik_bridge_status` tool reports what it sees without touching it.
- No resources, no prompts, no push — §12 puts those in Phase 2; `get_state`/`pickik_status` are
  snapshot-on-read and authoritative (decision 4).
- No `hw_*` tools at all: Phase 3, behind the §7 split gate, with gates 17–18 and the disconnect
  fail-safe. `pickik_status` reports `hw.enabled/present` so the agent can see they are absent rather
  than guessing from a tool list.
- Never logs the token (§9), including in `repr()`s of frames, exception texts included.

## 8. Dependencies and the two interpreters

`requirements.txt`: `mcp>=1.2,<2` pinned, stdlib otherwise. Runs on the **system** Python (3.13 here,
no `bpy`); the bridge runs on **Blender's** (3.11.11 on 4.5.3, 3.10.8 on 3.4.1, proven by the gate
suite). In-Blender modules stay 3.10-compatible for as long as 3.4 is claimed.

## 9. How it will be proven (not asserted)

`pick_ik_mcp/tests/test_server.py`:
1. **Against a fake bridge** (a stdlib socket speaking the real codec — no Blender): tool list matches
   the catalogue; an unknown `cmd` is refused and never name-resolved; `spec.deadline_ms` reaches the
   wire as `args["deadline_ms"]`; a `proto_rev` mismatch is refused *before* connecting; the token
   never appears in captured log output; `E_TIMEOUT` keeps its code and its reconcile wording.
2. **Live end-to-end**: spawn `blender --background`, start the bridge from the add-on, run a tool
   call through the real server, assert the nine §5 anchors and target A/B agreement within a micron —
   the same numbers gate 14 proves, now proven across the process boundary the server adds.
 3. The existing suites stay green: protocol 20/20, bridge 78/78 (4.5.3 + 3.4.1), acceptance 16/16.

**Status, said plainly.** Items 1 and 2 are *written*; items 1 and 2 have **not yet completed a run**, so
nothing in this section may be read as a result. While item 1 was being written it took the harness down
twice, and the two faults that caused it belong to the suite and not to the server: a thread exception
hook declared with four parameters where CPython hands one `ExceptHookArgs` record — which raised inside
the hook, and a raising hook is reported by the mechanism that invoked it, so reporting recursed — and
diagnostics that were not bounded, one of them printing a 1.6 MB payload while a buffering reader kept
all of it. Both are fixed; fixed and unverified is not the same as green. Until somebody has watched it
pass, the suite is gated: it declines to run at all unless `PICKIK_RUN_SERVER_TESTS=1` is in the
environment, and it says so out loud rather than exiting quietly, because a suite that skips without
notice is how a broken suite comes to be believed true.

A third fault, caught by reading rather than running: that same fixture asked for 1.6 MB, which is over
the 1 MiB frame cap that both `mcp_bridge._FRAME_MAX` and `mcp_client.MAX_FRAME_BYTES` enforce — so the
client rejected the frame as `overflow`, the payload never reached the assertion that claimed to be
checking it, and the check was failing for a reason unrelated to what it tested. Fixtures whose size is
the point are now computed from the two constants they must sit between, and that relation is itself
asserted, so the fixture cannot silently stop being a fixture.

## 10. Sign-offs taken (2026-09-07), and what keeps each one true

| decision | the rule | how it stops being a memory |
|---|---|---|
| **no auto-approval** | There is NO CODE PATH IN `pick_ik_mcp` ABLE TO EMIT `confirm` OR `arm`. Not "off by default": a default is a thing someone turns on once, and the first person to hit friction turns it on and the gate is gone for good. The two keys exist only as declared tool arguments that the agent itself fills. | A test asserts it structurally: the names `confirm` and `arm` occur nowhere in the package except in the argument-declaration tables, and a frame the server builds carries neither key unless it came in as a tool argument. |
| **connect-only** | The server never starts or stops the bridge. "A socket is listening" has to stay evidence that a human intended this session to exist — that is what the physical Start press buys, and an agent cannot reproduce it. `pickik_bridge_status` answers not-running plus THE INSTRUCTION for how to start it, so the agent can tell the operator what to do instead of failing opaquely. | A test asserts no start/stop tool appears in the tool list, and that the not-running status text names Start. |
| **verbatim payloads** | The bridge's `data` returns unchanged, so the scene stays the transcript and `get_state` can be compared against what was applied. Two riders: a BOUND on payload size, truncated with an explicit marker rather than silently, so one large reply cannot eat the agent's context; and the `E_*` CODE preserved as strictly as success shape, since the code is what lets the agent tell "out of workspace" from "gate closed" from "outcome unknown". | The truncation marker and the unmangled code are both asserted, against a deliberately huge reply and against every code in §4.5. |

One more, because it is the kind of thing that only bites later: **whatever comes back over the wire
is data for the agent to read, never instruction for it to follow.** A `message`, a `note`, or a mesh
filename authored inside Blender is untrusted text with respect to the agent's own instructions; the
server's job is to hand it over plainly labelled, not to obey it and not to act on it by proxy.

Naming: `pickik_` prefix on all fourteen, presentation-layer only — the wire still carries the bare §6
`cmd`, so plan §6's tables stay valid as written. The prefix is what keeps `get_state` and `solve_ik`
from colliding with whatever other robotics or CAD server the agent has connected beside this one.

Open in the contract, unchanged: `plan_id` lifetime for the Phase 3 `hw_motors_move_to` (§14 A), and
whether Phase 2 ever adds the coalesced push channel (§14 B).