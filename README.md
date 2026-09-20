# `pick_ik_mcp` — the MCP server for the PickIK Blender add-on

An agent talks to this process; this process talks to Blender. Two interpreters, one loopback socket,
and a deliberate gap between them: nothing in here imports `bpy`, and nothing in Blender imports the
MCP SDK. That separation is the whole safety argument, so it is stated first.

```
agent  <->  mcp_server.py (system Python 3.13, stdio)   pickik-bridge/1 over 127.0.0.1   mcp_bridge.py (Blender's Python)
                  no bpy, no import of the add-on                      NDJSON, one frame per line        owns bpy, owns the tick
```

The contract lives one directory up, in `blender_ik_addon/MCP_INTEGRATION_PLAN.md`. This directory
holds the server that obeys it. `SERVER_DESIGN.md` here is the design of this half; the plan is the
authority where the two disagree.

## Install and run

From the repository root, with the **system** Python — not Blender's:

```
pip install -r pick_ik_mcp/requirements.txt
python pick_ik_mcp/mcp_server.py --check          # what an agent would be told; serves nothing
python pick_ik_mcp/mcp_server.py                  # serve over stdio (what a client launches)
```

`--check` is the first thing to run after any change: it prints the briefing the client receives, what
the bridge reports about itself, the tool table with each command's class, lane and gate, and two
self-checks. It needs no client and no running Blender.

Point your MCP client at it with `command` set to your python and `args` to
`["…/pick_ik_mcp/mcp_server.py"]`. Optional configuration is one file, `~/.pickik/mcp_config.json`
(`--config` to put it elsewhere), read once at start; an unrecognised key is a refusal rather than
something to ignore, because a typo in an option name is a mistake you should hear about. See
`mcp_config.example.json`, and `GETTING_STARTED.md` for the two caveats that used to be missing here:
`--check` **does not read that file at all**, so it cannot confirm that your settings took — only the
serving path reads it — and of the six keys the server accepts, **only `runtime_file` is consumed**, the
other five being accepted in silence and then never looked at. Because a key the server does not read is
a refusal, the example carries no `_comment` either, and is one key deep for that reason and not from
stinginess.

## What is exposed, and where the list comes from

Fifteen tools: one per command the add-on actually answers, plus `pickik_bridge_status`.

The list is **not** written here. It is read out of `mcp_handlers_obs.HANDLERS` by parsing that file's
syntax tree — the registry cannot be imported from this process, because importing it pulls `bpy` — and
each entry's class, lane, gate and deadline come from `mcp_protocol`, the single authority shared with
the bridge. A hand-copied tool list would drift the moment a handler landed, and a tool with no handler
behind it is an agent being refused by a proxy that invented the tool. So there is nothing here to drift.

Every tool is prefixed `pickik_`. That is presentation only: the wire carries the bare command name, so
the plan's tables stay true as written, and `get_state` here cannot collide with another server's
`get_state` in a client that connected both side by side.

Argument schemas are deliberately permissive. The bridge owns the shape of arguments and answers
`E_INVAL` when they are wrong. A second, stricter description of the same rules in this layer would be a
second place to be wrong, and an over-strict one would reject forms the bridge accepts.

## Four promises, and what keeps each

| promise | what holds it |
|---|---|
| the server never supplies a gate | `confirm` and `arm` are *declared* so an agent can send them and are copied verbatim if it does; nothing adds them. The suite reads this package's own syntax tree and fails if an assignment to either key appears outside the two declared sites |
| the server never starts or stops the bridge | there is no such tool and no setting that would add one. When no bridge is listening, `pickik_bridge_status` says so **and says what a human must press** |
| a reply arrives as it was written | the bridge's payload is passed through untouched, its `E_*` code preserved as strictly as a success shape, and `retry_safe` left intact. A refusal is an answer, not an error of this server |
| a reply cannot swallow the context | rendered output is bounded at `MAX_RESULT_BYTES`, and the bound **announces itself** in the text. A truncation that hides is a wrong answer, not a short one |

`E_TIMEOUT` is the one reply that must never be re-tried by the proxy: it means the outcome is unknown,
not that the motion did not happen. The recovery is `pickik_get_state`, and the server holds still.

## Verification status, stated plainly

Proven, and green as of this writing:

- `blender_ik_addon/tests/test_mcp_protocol.py` — **20/20**
- `blender_ik_addon/tests/test_mcp_bridge.py` — **78/78** on Blender 4.5.3 LTS *and* 3.4.1, including
  the gate that checks the documentation's tables against the catalogue they were copied from
- `blender_ik_addon/test_acceptance.py` — **16/16** on both versions, with the main-thread stall gate
  reading a hard ceiling, a stored baseline median, and a control that reports how noisy the machine was

**Not yet proven:** this package's own suite, `pick_ik_mcp/tests/test_server.py`. It is written, it
parses, and it has not completed a run. While it was being written it took the harness down twice; the
two faults that caused that — a thread exception hook written with the wrong arity, and diagnostics that
were not bounded — are fixed, but fixed and untested is not the same thing.

```
python pick_ik_mcp/tests/test_server.py
```

That is the whole invocation: `RUN_UNATTENDED = True` is built into the file, because a suite that needs
an environment prepared before it can be run is a suite that does not get run, and nothing written about
the server is believed until this has passed. The gate that once required `PICKIK_RUN_SERVER_TESTS=1`
was turned the other way round rather than removed, so it stays one edit away: set
`RUN_UNATTENDED = False` at the top of the file, or set `PICKIK_RUN_SERVER_TESTS=0`, and it declines
until asked by name. The reason is in the file's own comment, and it is a threads-and-sockets reason.

Run it from a terminal the first time. It opens sockets and spawns threads, and each test carries a
watchdog of eight seconds that names the test which stopped and where every thread was sitting.

## When a tool says no

| what you see | what it means | what to do |
|---|---|---|
| `running: false`, `found: false` | no bridge is publishing an endpoint | in Blender: press **N** for the 3D-view sidebar ▹ category **PickIK** ▹ panel **PickIK arm7** ▹ tick **Enable MCP bridge**, then press **Start**. The server will not do it for you |
| `why:` starting `proto_rev:` | the add-on and this server speak different revisions of the protocol | upgrade one of them deliberately; the runtime record says which is which |
| `why:` starting `acces:` | the **bridge** refused the session at the handshake: the secret in the runtime record is not the one the running bridge holds | **not** the gate refusal below, despite the same code, and the two want opposite remedies: restart the bridge in Blender so it publishes a fresh record, since one left behind by an earlier session is the usual cause |
| `E_ACCES` | a gate was not satisfied | the agent must type `confirm` (and `arm`, for motion) itself. Read the message: it names what is missing |
| `E_BUSY` | the rig is mid-motion | it is data, and it says whether retrying is safe. Read `retry_safe` |
| `E_TIMEOUT` | **outcome unknown** | call `pickik_get_state`. Do not re-issue the command that timed out |
| `E_MCP_*` | this server, or the link, not the rig | the `note` field says so; everything else came from Blender |

## Windows notes, because both were measured and not guessed

- The clock for every interval here is `perf_counter`. `monotonic` on Windows is `GetTickCount64` and
  resolves in 16 ms steps — four times the tick budget it is meant to be guarding.
- A socket is closed by shutting the write side, then the read side, then releasing the descriptor.
  `close()` straight after `sendall` can reset instead of flush, and closing with unread bytes in the
  queue makes WinSock send a reset and discard the reply already on its way.
- The endpoint is read from `~/.pickik/bridge.json`, never from a hard-coded port. 9876 is only the
  default behind the add-on's **Port** property, which is edited in the PickIK sidebar box at any time,
  and a stale record from a dead Blender looks exactly like a live one until you try to connect.
