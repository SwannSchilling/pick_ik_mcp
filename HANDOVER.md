# Handover — pick_ik_mcp / MCP server for the PickIK Blender add-on

Status date: this file was written at the end of a long session, and corrected at the end of the one
that followed it. Read this section before touching anything; it is the part that says what is true
and what is not. Where an earlier claim was wrong it is kept, struck, and answered, because a handover
that silently rewrites itself teaches the next reader nothing about how the ground was found.

## The problem that was closed  (this file's "one unclosed problem")

`pick_ik_mcp/tests/test_server.py` — `test_a_frame_is_sent_and_the_reply_comes_back_unchanged` was
said to **hang on its very first live round-trip** against the fake bridge, and that every run
"reaches exactly that test and then is interrupted". **It does not hang, and nothing ever hung.** The
test completes, its reply comes back unchanged under deep equality against the payload the rig
produced, and it has done so in three consecutive runs. The last:

```
== 42 passed, 0 failed, 42 checks ==
== SIGINT was truly heard 0 time(s) while 0 test(s) reported an interrupt; answering at the time: nobody ==
18:12:35 run -- reached the end of the run: 42 checks, 0 failed
```

Fifteen tests entered, fifteen completed, the watchdog never fired. The gate this file set — *"Do NOT
call the server 'done' until it does"* — is met.

There was never a fault in the wire, the fixture's framing, the OS, or the SDK. There were two defects
in `pick_ik_mcp/mcp_client.py`, and the interruption that was taken for the hang came from one of them.

1. **A liveness probe that was a broadcast.** `read_runtime` asked whether the bridge's process lived
   by `os.kill(pid, 0)`. On Windows the second argument is not a signal number at all: it is read as a
   console control event, and `0` of those is `CTRL_C_EVENT`. So every test that read a runtime record
   broadcast a Ctrl+C to its own console, and the interrupt surfaced at the next call in the same stack
   that looked for a pending signal — which was always the blocking `sock.recv` inside `_recv_frame`.
   Four symptoms, every one located at the read; the cause four frames above it. That single
   mis-location is why the read was suspected for two hours and why every instrument aimed at the
   socket came back saying the socket was innocent. Both halves of the call were then measured:
   *refused*, it raised, and the record was condemned `stale` though the pid in it was the caller's own
   and the caller was demonstrably alive; *accepted*, it returned, and the event went out to the
   console. `except OSError` never fired on the accepting branch, which is why the code's own comment
   could only apologise for "raising for live pids and returning for dead ones": that comment was
   describing the symptom and not the cause. `mcp_client._pid_is_running` asks `kernel32` now
   (`OpenProcess` + `GetExitCodeProcess`, `STILL_ACTIVE`), keeps `os.kill(pid, 0)` only where `0`
   genuinely means *exist, do not signal*, and declares a termination only where it has been witnessed
   — an unopenable pid yields `None` and is let through, because falsely condemning a live bridge is
   the very failure the probe exists to prevent, and `connect_to_believe` remains the verdict.
2. **A refused handshake that was believed.** `Bridge.connect()` sent the hello, took back whatever the
   bridge deigned to answer, and went straight to `self.connected = True`. Its docstring promised
   *believe only what the hello says*; it never read what the hello said. A bridge that answered
   `ok: False`, `E_ACCES`, `auth failed` was believed to have admitted the session, the commands were
   written down a link that had already been turned away, and the caller told that all was well. It is
   security-relevant. Note when it became visible: only once the token fixture was mended could the one
   check written to catch it run at all — in every earlier run it had aborted on the way there.

A third defect, in the suite and not the server, made the original symptom look like a hang rather than
a stop: `_serve`'s read carried a **5.0 s** timeout while `halt()` granted its join **2.0 s** and closed
only the listener. Two seconds asked of a wait built to last five — the join could never succeed, so
every halt burned its whole allowance and returned with `still alive=True`. In the test that keeps two
bridges that is four seconds, which is exactly what the watchdog had been lowered to for the diagnostic
run: the watchdog was not wound too low, the clock that was, and the "hang" at the eighth test was the
watchdog doing its duty against a fixture that could not finish in time.

## What was done, and the one line that answered it

This file's instruction — instrument the watchdog so it writes to a file from a separate timer, lower
`WATCHDOG_S`, and *"read the ONE line that shows where the main thread is parked"* — was carried out,
and it did answer. That line, from `watchdog-dump.log`:

```
-- thread ... MainThread  <== the main thread; its deepest line is the answer --
    line 524, in test_a_refusal_is_an_answer_and_not_a_malfunction    busy.halt()
    line 310, in halt                                                 self.join(timeout=2.0)
-- thread ... Thread-11 --
    line 213, in _serve        chunk = conn.recv(65_536)
```

Nothing in `mcp_client`, nothing in `mcp_server`, nothing in the SDK, nothing on the wire. And the
four `SIGINT` hearings, taken down in the journal with their stacks, put the sender where the main
thread had been a moment earlier: in `read_runtime`, at `os.kill(pid, 0)`.

Two cautions, both paid for. **A clipped field is not an absent field** — the first verdict drawn from
those stacks ("`mcp_client.py` appears nowhere, therefore the client is innocent") was drawn from lines
clipped at 1,200 characters, the deep frames having never been written at all. And **a control that
cannot fail cannot succeed** — the exoneration of `os.kill` was first tested in a detached console,
which is the one configuration in which the event cannot be accepted.

## Hypotheses, and what became of each

1. *"The fake bridge's `_serve`/`_answer` and the client's `_recv_frame` are out of step for the HELLO
   specifically."* — **Wrong.** The hello was answered correctly all along; the journal records it,
   `the greeting is off the wire`, in every run.
2. *"A second, separate hang in the accept loop: if it blocks inside `_serve`'s `conn.recv` with a long
   timeout after finishing, the next connection is never accepted."* — **Nearly, and the closest of the
   three.** The long timeout in that `recv` was real and is mended. The consequence was not a wedged
   accept loop but a join that could never be satisfied; the accept loop was never reached again,
   because the test had finished with the bridge.
3. *"Something in `_tmpdir()` / `tempfile.mkdtemp` on first call."* — **Exonerated**, as this file's own
   low prior anticipated.

## Evidence that still stands

- Environment is EXONERATED. This exact command prints `CLOSED_OK` on the box:
  ```
  python -c "import socket,threading,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(1); t=threading.Thread(target=lambda:(lambda c,a:c.close())(*s.accept())); t.daemon=True; t.start(); c=socket.socket(); c.connect(('127.0.0.1', s.getsockname()[1])); time.sleep(0.5); print('CLOSED_OK')"
  ```
  Nothing on this machine kills processes that open a loopback listener and connect to it. Note that
  this exoneration was reached twice and wrongly before the true cause was known, and remained correct
  throughout: the OS was never the fault, and neither was an antivirus.
- The frame-bound bug is FIXED and PROVEN, and the fix stands. In `Bridge._recv_frame`, `if rest:` was
  changed to `if sep:` — a newline present means a whole frame is buffered; the old `if rest:` only
  returned a frame when extra bytes followed the newline, so a lone final frame sat in the buffer
  forever. `the bridge heard 2 frames in total, one of them the hello` is the proof.
- `faulthandler.dump_traceback_later(5, exit=True)` wrapped around `runpy.run_path(...)` did NOT
  produce a dump in two attempts. Do not rely on it. The parenthetical explanation given there — "(a
  console Ctrl+C does not trigger it)" — is true and was load-bearing: the Ctrl+C's were genuine, and
  the dump that eventually answered came from the suite's own `threading.Timer`, writing to a file.
- `WinError 10053` (`WSAECONNABORTED`) seen after a bridge is abandoned mid-read is an effect and not a
  cause. Do not chase it.

## What has been built

- `pick_ik_mcp/mcp_server.py` — MCP stdio server (low-level `mcp.server.Server`, not FastMCP,
  deliberately: schemas are generated from the catalogue, not from decorated signatures). 15 tools
  = 14 answered commands + `pickik_bridge_status`. Tool list is read by parsing
  `blender_ik_addon/mcp_handlers_obs.py`'s `HANDLERS` registry via AST (no `bpy` import in this
  process). `--check` prints the briefing, the bridge status, the tool table, and two self-checks.
- `pick_ik_mcp/mcp_client.py` — the bridge client (no MCP-SDK dependency, no `bpy`). Owns the wire
  discipline: NDJSON framing, `_bye` half-close, `perf_counter` clock, the `kernel32` liveness probe,
  and — now — the verification of the hello. `describe_endpoint()` powers the not-running instruction.
- `pick_ik_mcp/vendored/mcp_protocol.py` — byte-identical copy of the add-on's protocol module
  (PROVEN: the suite's digest check passes, `407128dcdca6`). **Never edit it**: fix the add-on's copy
  and restamp the documents.
- `pick_ik_mcp/README.md`, `pick_ik_mcp/requirements.txt` (`mcp>=1.2,<2`). The `why:` table in the
  README now distinguishes a session refused by the **bridge** (`why:` starting `acces:` — the shared
  secret) from a gate refused by the **rig** (`E_ACCES`), which share a code and want opposite remedies.
- `pick_ik_mcp/SERVER_DESIGN.md` — the design; §9 states what is proven vs unproven.
- `pick_ik_mcp/tests/watch_console_probe.py` — added while hunting. Counts `SIGINT` deliveries in a
  console, with four arms each turning one knob: bare, `--with-imports`, `--with-round-trip`,
  `--with-runtime-record`, and a `--new-console` detachment. It is what separated an external broadcast
  from an in-process raiser. Safe to delete; it is a probe and not a test, which is why it is not
  named `test_*`.
- Artefacts the suite writes beside itself: `suite.log`, `run-journal.log`, `watchdog-dump.log`. Read
  all three; the journal records what the bridge saw, in the order it heard it.

## What is proven (green, reproduced)

- The MCP server suite — **42 passed, 0 failed, 42 checks**, fifteen tests entered and fifteen
  completed, `SIGINT … 0 time(s)`, watchdog never fired, nine halts reporting `still alive=False`.
  Reproduced three times running at the end of the session that closed the problem above.
- Bridge suite `blender_ik_addon/tests/test_mcp_bridge.py` — 78/78 on Blender 4.5.3 AND 3.4.1,
  including gate 20 which checks the documentation's tables against the catalogue they came from.
- Protocol `test_mcp_protocol.py` — 20/20.
- Acceptance `blender_ik_addon/test_acceptance.py` — 16/16 on both Blenders (gate 6 re-shaped to a
  25 ms ceiling + stored-baseline median + control; `PICKIK_UPDATE_BASELINE=1` to refresh).

## What was failing in the suite, and how each was mended

All of these were faults of the checks and not of the server, which is the point of writing them down.

1. `test_every_module_attribute_reached_for_actually_exists` — the name-checker imported
   `os.environ`, `sys.path`, `mcp_protocol.GATE`, `mcp_protocol.ERR`, `mcp_server.CMD_OF_TOOL` and
   `ctypes.windll` as though every dotted access named a module. 26 chains mis-resolved, and the
   detail was clipped to three, hiding the rest. Mended by importing the root and walking the rest with
   `getattr`, which is the only way an attribute can be reached at all. It then found three that were
   genuinely not there — `conn: socket.Socket`, surviving only because `from __future__ import
   annotations` means an annotation is never evaluated — two of them long-standing. 371 chains now
   resolve against the modules themselves.
2. `test_the_server_is_wired_and_speaks_mcp` — two mis-calls of the SDK. `get_capabilities({}, {})`
   handed two dicts where the method expects `(notification_options, experimental_capabilities)`, and
   the SDK reads `.tools_changed` off the first only down the branch where a `tools/list` handler has
   been registered, which is why it presented as a fault of the server in one test of fifteen and in no
   other. And the dispatch check searched the keys of every mapping on the object for the strings
   `tools/list` and `tools/call`, where the SDK keys its table by the request **type**
   (`ListToolsRequest`, `CallToolRequest`, `PingRequest`), so it reported "no registry located". Both
   mended: the capabilities are read from the record the module hands to `server.run`
   (`S.initialization_options().capabilities`), and the registered types are derived from the object and
   printed, so that an SDK bump which moves them says so here instead of going quiet.
3. `test_the_two_copies_of_the_protocol_have_not_drifted` (the docs-quote check) — `SERVER_DESIGN.md`
   carried a stale revision literal (`7704447a904e`) while the build produces `149100862958`. The stamp
   is GENERATED, and the literal is inside the sentinels, so the remedy is the generator and never a
   hand on the prose. **This file's instruction to run `python -m blender_ik_addon.mcp_docs` does not
   work outside Blender**: `blender_ik_addon/__init__.py:44` imports `bpy`, and behind it `mathutils`,
   `bpy.props` and four add-on modules. Run it from Blender's own Python, or — as was done — drive
   `mcp_docs` with the vendored copy bound to `P`, which the suite's own digest check vouches for as
   the add-on's bytes. Do not import `blender_ik_addon` to get at it.
4. `test_the_handshake_is_verified_before_anything_else` — the token test undid its own experiment: the
   bridge and the published record were both handed the one wrong value, so client and bridge agreed
   and the refusal the check existed to observe was impossible; and `publish()` had no `token` to be
   given. Mended at both ends, and it is the check that went on to find defect 2 above.
5. The interrupt policy — one interruption stops the run, and is obeyed. The policy introduced earlier
   absorbed a single interrupt and continued, inferring an operator from the spacing of presses; four
   genuine deliveries, two seconds apart, every one from a source that was neither a hand nor a fault,
   defeated it, and a run that ought to have stopped ran on against the wishes of the only party able to
   ask. Intent cannot be read out of an interval. Tests never entered are now named in the summary and
   recorded as failures, and the journal is forbidden from saying it reached the end when it did not.

## Still open, and not hidden

- `ResourceWarnings` (`unclosed file`) from `open(path).read()` one-liners in the suite — cosmetic, and
  untouched. Close them or use `with`.
- The tree is under **no version control**: there is no repository at, above, or beside it, so `git diff`
  answers *not a git repository* and an audit can only be made against the files themselves and not
  against a history. Put that right before the next change of consequence.

## Discipline notes (learned the hard way, both sessions)

- THE READ-BACK AND GREP CHANNELS MANGLE IDENTIFIERS in both directions. Examples seen: `json.loads`
  rendered/acted-as `json.decode`, `ast.iter_child_nodes` as `ast.iter_children`, `socket.socket` as
  `socket.Socket`, `_mcp_prefs` as `_mcp_pref`, `SHUT_WR` as `SHUT_SD`. The `socket.Socket` case was
  confirmed independently when the mended name-checker found three annotations naming an attribute that
  is not there. Consequence: NEVER trust a file you have not executed for identifier spellings, and
  never "fix" a name by reading it back. Verify by running, ideally a tiny check that does nothing else.
  This applies to `windll` and not `winll`, to `SHUT_RDWR` and not `SHUT_R`, and to `get_source_segment`
  returning `None` where an exception was expected.
- Calibrate an instrument on known causes before believing its readings, and never let it report a field
  it did not fill. Four instruments of this session were found miscalibrated by their own output: a stack
  recorder clipping at 1,200 characters (read as an absence), a control run in a detached console (where
  the call under test could not succeed), a projection line summing three trials and then doubling the
  sum, and a probe printing one column where two were wanted.
- `verify_names.py` (in `blender_ik_addon/tests/`) only checks EXCEPTION class names in except/raise; it
  will NOT catch mangled attributes on modules. Use the suite's
  `test_every_module_attribute_reached_for_actually_exists` for that, which is now sound.
- `threading.excepthook` is a real trap: it is called with ONE `ExceptHookArgs` record (3.8+). A
  four-parameter hook raises inside the hook, and a raising hook is reported by the same mechanism,
  which recurses. Keep the hook single-argument, non-raising, and bounded in what it prints.
- Running the suite via the DeepSeek-harness `bash` tool crashed the harness repeatedly (threads +
  sockets + large output). The user therefore runs `test_server.py` themselves in a PowerShell terminal.
  **Do not try to execute that suite through the harness shell.** Standalone probes — one thread, one
  listener, bounded — are safe and were used throughout, which is how the mechanism was measured at all.
- The suite's opt-in is inverted to run by default: `RUN_UNATTENDED = True` and
  `WATCHDOG_S = float(os.getenv("PICKIK_TEST_WATCHDOG_S", "8"))` in `test_server.py`. Set
  `RUN_UNATTENDED = False` to re-arm the gate; the skip notice is now reachable and will be printed,
  having had a `return` above it. Beware: anything that sweeps the tree now executes this suite (it
  drives threads and sockets); if there is a sweeper, exempt it there.
- Do not lower `PICKIK_TEST_WATCHDOG_S` below the built-in eight to hunt a hang. A low allowance will
  fire on a fixture that is merely slow, and take the tally down with it — which is how a green run
  came to be reported as a hang at the eighth test.
- The fake bridge timeout in `tests/test_server.py` (`publish()`) is set to `request_timeout_ms: 900`
  so a wedge fails in ~1.15 s rather than ~5 s.
- The protocol revision is `sha256(catalogue)[:12]` — it can legitimately be all digits, and
  `149100862958` is one. There is NO stable human-readable "the" value; only "does the bridge's copy
  agree with the vendored copy's" is meaningful, and that is enforced at connect (hello) and by the
  byte-identity check.

## Exact run commands

The standing run. Take the artefacts as the evidence they are, and read the tally against the journal:

```
cd F:\GithubProjects\URDF_BIO_IK\pick_ik_mcp\tests
$env:PICKIK_TEST_WATCHDOG_S = "8"
cmd /c "python -u test_server.py > suite.log 2>&1"
```

A green run reads `42 passed, 0 failed, 42 checks`, fifteen entered and fifteen completed,
`SIGINT … 0 time(s)`, and leaves `watchdog-dump.log` at its single header line. The `-u` is essential:
an unbuffered line is a line that goes with the process, and a hard exit takes the tally down with it.

For the whole project's suites:

```
cd /f/GithubProjects/URDF_BIO_IK
python blender_ik_addon/tests/test_mcp_protocol.py
"C:/Program Files/Blender Foundation/Blender 4.5/blender.exe" --background --factory-startup --python blender_ik_addon/tests/test_mcp_bridge.py
"C:/Program Files/Blender Foundation/Blender 4.5/blender.exe" --background --factory-startup --python blender_ik_addon/test_acceptance.py
python -m blender_ik_addon.mcp_docs --check    # from Blender's Python only: the package imports bpy
python pick_ik_mcp/mcp_server.py --check
```
