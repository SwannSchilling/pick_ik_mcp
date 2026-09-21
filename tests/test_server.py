"""Tests for the MCP server: what an agent may be told, and what the server may never do.

    python pick_ik_mcp/tests/test_server.py

No Blender here, and deliberately so: the fake bridge below speaks the real codec out of
`mcp_protocol`, over a real loopback socket, so the framing, the handshake and the reply shapes are all
exercised for true while nothing has to be believed about them. The `mcp` SDK is only needed to *serve*;
the properties tested here are the ones that hold whether or not a client is attached.

The suite is arranged around four promises made in SERVER_DESIGN.md. A promise with no check against it
is a hope, so each has a gate:

  1. the server never supplies a gate               -> the AST gate, plus frames-on-wire assertions
  2. the server never starts or stops the bridge    -> the tool table and the not-running instruction
  3. the reply shape is preserved verbatim          -> deep equality against the bridge's own payload
  4. the reply is bounded, and marked so            -> the truncation marker, never silence
"""
from __future__ import annotations

import ast
import difflib
import faulthandler
import io
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback

#: Crash-time capture, enabled before the packages are imported so that an import which never returns
#: is caught too. The fault this suite carries reports as a death without a traceback: the run stops
#: inside a test, prints nothing further, and never reaches its summary. That is the shape of a death
#: at the level below Python, where there is no handler left to raise and the buffered stdout goes with
#: the process. This facility writes the stacks of every thread to stderr, unbuffered, so the record
#: survives the death that swallows everything else. The handover's two failed attempts used
#: `dump_traceback_later`, which is a timer for a thread that is wedged; this is the signal path for a
#: process that has already gone, and it is verified on this box to capture a fatal signal and to leave
#: `SIGINT` on `default_int_handler`, so the interrupt path below is undisturbed.
faulthandler.enable(all_threads=True)

_HERE = os.path.dirname(os.path.realpath(__file__))
_PKG = os.path.normpath(os.path.join(_HERE, os.path.pardir))
_REPO = os.path.normpath(os.path.join(_HERE, os.path.pardir, os.path.pardir))
for _p in (_PKG, _REPO, os.path.join(_PKG, "vendored")):
    if _p not in sys.path:
        sys.path.append(_p)

import mcp_protocol as P                              # noqa: E402  the vendored copy, as the server reads it
import mcp_client as C                                # noqa: E402
import mcp_server as S                                # noqa: E402

CHECKS: list = []
TOKEN = "sekret-tok-3n" + "8" * 24                    # never a real one, never to be echoed

#: The fake bridge that is live right now, for the watchdog's report, and for nothing else. Which side
#: of the wire is wedged is the question this suite exists to answer, and the answer differs by the end;
#: the one thing that must not happen is for the probe to disturb the thing being probed, so it is only
#: ever a passive reference, read from outside, and never written to.
_LIVE_FAKE: list = [None]

#: A journal, kept beside the two reports. Where the account that matters is of a process that died
#: without speaking, nothing held in-process can describe it afterwards, so the record is made as it
#: goes: one line per event, at the moment of the event, out of a raw descriptor. A buffered line is a
#: line that dies with the process, and that is precisely the fault under investigation -- the summary
#: line of this very suite has already gone missing from a log once, for the want of a flush.
JOURNAL = os.getenv("PICKIK_TEST_JOURNAL", os.path.join(_HERE, "run-journal.log"))


def _journal(event: str, message: str = "") -> None:
    """Write one line of the journal and let it fail only ever so quietly.

    Downside, recorded once where it can be read: writing a line at each event moves the timing of
    whatever races between those events. The events here are a handful per test, and the fault has
    reproduced at every run so far, so the observer is taken not to disturb the observed -- if a run
    ever behaves differently from the run before it under the same command, this is the first thing to
    doubt, and `PICKIK_TEST_JOURNAL` pointed at the void is how to undoubt it.
    """
    line = f"{time.strftime('%H:%M:%S')} pid{os.getpid()} {event}"
    if message:
        line = f"{line} -- {_clip(message, 400)}"
    try:
        fd = os.open(JOURNAL, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            payload = (line + "\n").encode("utf-8", "replace")
            while payload:
                written = os.write(fd, payload)               # partial writes are normal, not fatal
                if written <= 0:
                    break
                payload = payload[written:]
        finally:
            os.close(fd)
    except BaseException:                                     # a journal must never become the fault
        pass


def _record_of_the_bridge(fake, event: str, message: str = "") -> None:
    """What the fake bridge saw, when it saw, in the order it saw it, in both directions."""
    if fake is not None:
        fake.log.append(f"{event}: {message}")
        del fake.log[:-50]                                    # kept, but never unbounded
    _journal(f"bridge {event}", message)


#: Nothing this suite prints may be unbounded. It talks to sockets, it renders replies, and one of
#: them deliberately builds a megabyte of scene data. A thread that dies printing tracebacks, read by a
#: buffering consumer that keeps everything, is how you take down the process that spawned the shell.
#: Every diagnostic below therefore clips.
#:
#: The arity of the hook is not decoration either: a thread hook is called with ONE ExceptionHookArgs
#: record (3.8+). Written for four parameters, it raises inside the hook -- and a raising hook is
#: reported by the very mechanism that invoked it, which calls the hook again. That is not a
#: diagnostic, that is a flood, and it took the harness with it.
CLIP_LIMIT = 2000
CHECK_DETAIL_LIMIT = 240
#: How many thread deaths get reported before the rest are only counted. A thread that raises inside a
#: tight loop would otherwise print a clipped-but-still-long traceback on every single iteration, and
#: "long, times, many" is precisely the shape of a flood.
MAX_THREAD_REPORTS = 6
_thread_reports = [0]


def _clip(text, limit: int = CLIP_LIMIT) -> str:
    text = str(text)
    return text if len(text) <= limit else \
        text[:limit] + f" ... [{len(text) - limit} more characters were clipped]"


def _report_thread_exception(args):
    """Report a helper thread's death briefly, a few times, and never raise while doing it.

    The fake bridge lives on a daemon thread; if it dies the client simply stops hearing from it and
    every later check fails for a reason the report never mentions, which reads as a hang and costs an
    afternoon. So the death is announced -- but announced safely, because this very hook is what once
    took the harness down by being written with the wrong arity and no bound.
    """
    try:
        if _thread_reports[0] >= MAX_THREAD_REPORTS:
            _thread_reports[0] += 1
            return
        _thread_reports[0] += 1
        body = "".join(traceback.format_exception(
            getattr(args, "exc_type", None), getattr(args, "exc_value", None),
            getattr(args, "exc_traceback", None)))
        print(f"!! a helper thread raised (report {_thread_reports[0]} of {MAX_THREAD_REPORTS}):",
              _clip(body), flush=True)
    except BaseException:                       # a diagnostic that itself raises is worse than none
        pass


threading.excepthook = _report_thread_exception


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), _clip(detail, CHECK_DETAIL_LIMIT)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f" -- {_clip(detail, CHECK_DETAIL_LIMIT)}" if detail else ""), flush=True)


# --------------------------------------------------------------------------- the fake bridge --
#: How long a read is asked to wait before the loop beneath it gets the chance to look at the stopper.
#: The accept loop above already polls at a quarter of a second; the read loop beneath was asking five,
#: and a stopper set in the meantime could not be heard until the whole five seconds had been waited.
#: The two were measured apart and the arithmetic of them put two seconds of join against five seconds
#: of read, which is a sum that can never be paid: every halt that found a thread mid-read returned
#: with the thread still alive, two seconds the later.
READ_POLL_S = 0.25
HALT_JOIN_S = 2.0


class FakeBridge(threading.Thread):
    """One end of a loopback socket that speaks `pickik-bridge/1` for true.

    It records what it received, so that "the server did not send that" is an assertion and not a
    hope, and it can be told to refuse in one particular way, so that a refusal can be told apart
    from a malfunction.
    """

    def __init__(self, *, proto_rev: str | None = None, reply_over: dict | None = None,
                 token: str = TOKEN) -> None:
        super().__init__(daemon=True)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))          # an ephemeral port, which we then publish
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self.token, self.proto_rev = token, proto_rev or P.proto_rev()
        self.received: list = []                       # every frame decoded off the wire, in order
        self.reply_over = dict(reply_over or {})       # cmd -> a whole reply object to send instead
        self.refuse = None                             # (code, message) to answer everything with
        self.accepted = 0
        #: The connection presently being served, if any. Named for `halt` alone: a stopper that is set
        #: does not interrupt a `recv` already in progress, and the listener that is closed reaches no
        #: connection already accepted, so without this the read below could only be waited out.
        self._conn: socket.socket | None = None
        self.stopper = threading.Event()
        self.log: list = []
        _LIVE_FAKE[0] = self                          # the watchdog reads this; it never writes it

    def _log(self, event: str, message: str = "") -> None:
        _record_of_the_bridge(self, event, message)

    def run(self) -> None:
        self._sock.settimeout(0.25)
        self._log("run", "entered the accept loop")
        while not self.stopper.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                self._log("run", f"the listener went away: {type(exc).__name__}: {exc}")
                return
            self.accepted += 1
            self._log("accept", f"connection {self.accepted} from {_addr}")
            self._conn = conn                       # named for halt, which is the only one that wakes it
            with conn:
                self._serve(conn)
            self._conn = None
            self._log("accept", f"closed connection {self.accepted}, back to the accept loop")
        self._log("run", "left the accept loop on the stopper")

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        conn.settimeout(READ_POLL_S)
        while not self.stopper.is_set():
            try:
                chunk = conn.recv(65_536)
            except TimeoutError:
                #: Silence is not a closed link, and a hush upon the wire is not a departure from it.
                #: `TimeoutError` is a subclass of `OSError`, so the two below were one handler, and a
                #: client that stopped to think about what to send next was timed out of the read loop
                #: and back to the accept loop, where it was not wanted. A poll is what the loop is for,
                #: and the stopper is the single thing it is built to obey.
                continue
            except OSError as exc:
                self._log("serve", f"left the read loop on {type(exc).__name__}: {exc}")
                return
            if not chunk:
                self._log("serve", "the peer closed, leaving the read loop")
                return
            buf += chunk
            while True:
                line, sep, rest = buf.partition(b"\n")
                if not sep:
                    break
                buf = rest
                try:
                    frame = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._log("serve", f"bad frame of {len(line)} bytes, answered with PROTO")
                    conn.sendall(P.encode({"ok": False, "error": {"code": P.ERR.PROTO,
                                                                "message": "bad frame"}}))
                    continue
                #: Entered before the frame is kept and before it is answered: when the death is in the
                #: answer, the record still has to say what was being answered when it came.
                seen = frame.get("cmd") or ("hello" if isinstance(frame.get("hello"), dict) else None)
                self._log("recv", f"{seen} id={frame.get('id')} of {len(line)} bytes, "
                              f"{len(buf)} still buffered after it")
                self.received.append(frame)
                self._answer(conn, frame)
        self._log("serve", "left the read loop on the stopper")

    def _answer(self, conn: socket.socket, frame: dict) -> None:
        cmd, req_id = frame.get("cmd"), frame.get("id")
        args = frame.get("args") or {}
        #: The greeting is a frame of its own -- {"hello": {...}} with the secret under "auth", per
        #: mcp_protocol.hello and the arrow in the integration plan. This site used to answer it in the
        #: command envelope, because that was the shape the client happened to send, and so the
        #: stand-in agreed with the client instead of with the bridge -- and a witness that agrees
        #: with the defendant has not witnessed anything. It now takes what the real bridge takes and
        #: refuses what the real bridge refuses, in the real bridge's own words, so a client assembled
        #: to the wrong shape fails here precisely as it fails there.
        if isinstance(frame.get("hello"), dict):
            hs = frame["hello"]
            if str(hs.get("auth", "")) != self.token:
                self._log("answer", "auth failed, refusing the session")
                conn.sendall(P.encode(P.error_response(req_id, P.ERR.ACCES, "auth failed")))
                return
            if str(hs.get("proto_rev", "")) != self.proto_rev:
                self._log("answer", f"proto_rev mismatch, refusing: bridge {self.proto_rev} "
                               f"!= client {hs.get('proto_rev')!r}")
                conn.sendall(P.encode(P.error_response(
                    req_id, P.ERR.PROTO,
                    f"proto_rev mismatch: bridge {self.proto_rev} != client {hs.get('proto_rev')!r}")))
                return
            #: Bracketed on both sides of the write, because a death between these two lines can only be
            #: a death inside `sendall`, and the hypotheses otherwise have to be told apart by their
            #: effect on the client, which is a far weaker record. The hello is what hypothesis 1 names.
            self._log("answer", "hello ok, sending the greeting")
            conn.sendall(P.encode({"hello": {"protocol": P.PROTOCOL, "proto_rev": self.proto_rev,
                                            "server": "fake",
                                            "hw": {"present": False, "enabled": False}}}))
            self._log("answer", "the greeting is off the wire")
            return
        if cmd == "hello":
            self._log("answer", "a hello arrived inside a command envelope; the bridge answers this "
                               "with E_PROTO and so, now, does this")
            conn.sendall(P.encode(P.error_response(req_id, P.ERR.PROTO, "expected a hello frame")))
            return
        if self.refuse is not None:
            code, message = self.refuse
            self._log("answer", f"answering {cmd} with the standing refusal {code}")
            conn.sendall(P.encode({"ok": False, "id": req_id, "cmd": cmd,
                                    "error": {"code": code, "message": message},
                                    "retry_safe": code in (P.ERR.BUSY, P.ERR.AGAIN)}))
            self._log("answer", f"the refusal {code} is off the wire")
            return
        if cmd in self.reply_over:
            over = dict(self.reply_over[cmd])
            over.setdefault("id", req_id)
            over.setdefault("cmd", cmd)
            over.setdefault("ok", True)
            self._log("answer", f"answering {cmd} from the override")
            conn.sendall(P.encode(over))
            self._log("answer", f"the override for {cmd} is off the wire")
            return
        self._log("answer", f"answering {cmd} with the echo")
        conn.sendall(P.encode({"ok": True, "id": req_id, "cmd": cmd,
                                "data": {"answered": cmd, "echo": dict(args)}}))
        self._log("answer", f"the echo of {cmd} is off the wire")

    # -- the record the bridge would have published ------------------------------------------------
    def publish(self, directory: str, *, proto_rev: str | None = None,
                host: str = "127.0.0.1", token: str | None = None) -> str:
        #: The token in the record and the token in the bridge are two different things, and the hand that
        #: writes one has no business assuming the other. `self.token` is what the bridge will compare an
        #: incoming hello against; what goes into the file is what a client is told to believe. Holding
        #: them equal makes the record unfalsifiable, and a test that wants the client to be wrong has no
        #: way to make it so. The value is still never logged, which is why this parameter is absent from
        #: the line beneath.
        path = os.path.join(directory, "bridge.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"host": host, "port": self.port, "token": token or self.token,
                      "proto_rev": proto_rev or self.proto_rev, "pid": os.getpid(),
                      "request_timeout_ms": 900, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
        #: What the record tells a client, since "it connected, but nobody answered" has two halves and
        #: the client's half is read out of this file. The token is named by its key, never its value.
        self._log("publish", f"{os.path.basename(path)}: host={host} port={self.port} "
                       f"proto_rev={proto_rev or self.proto_rev} request_timeout_ms=900")
        return path

    def halt(self) -> None:
        self._log("halt", "setting the stopper, closing the listener, waking the read")
        self.stopper.set()
        try:
            self._sock.close()
        except OSError:
            pass
        #: Woken, if it were sleeping. The stopper above is not heard by a `recv` already in progress,
        #: and a listener that is closed reaches no connection already accepted, so the read has to be
        #: shut from this side: upon Windows it is `shutdown` and not `close` that recalls a blocked
        #: read to attention, and the `close` follows after, being idempotent and taken in the hope of it.
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self.join(timeout=HALT_JOIN_S)
        self._log("halt", f"joined; still alive={self.is_alive()}")
        #: Dropped only after the join, so that a halt which itself wedges in the join still leaves a
        #: bridge the watchdog can interrogate. Losing the subject is how a hang stops being reportable.
        if _LIVE_FAKE[0] is self:
            _LIVE_FAKE[0] = None


def _the_code_of(payload) -> str:
    """The one code worth reporting about a payload, and never the secret it is protecting."""
    if not isinstance(payload, dict):
        return f"<not a mapping, but {type(payload).__name__}>"
    if payload.get("code"):
        return str(payload["code"])
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return "-"


def call(name: str, arguments: dict | None = None, *, runtime_file: str) -> dict:
    S._close_bridge()
    #: Bracketed either side of the dispatch, because everything above this line is the fixture and
    #: this line is the server under test: a BEGIN with no matching END below is the answer to which
    #: half of the round trip the run went, and the two halves answer differently.
    _journal("client", f"dispatching {name} against the record {os.path.basename(runtime_file)}")
    payload = S.dispatch(name, dict(arguments or {}), runtime_file=runtime_file)
    _journal("client", f"{name} came back ok={payload.get('ok') if isinstance(payload, dict) else '?'} "
                  f"with the code {_the_code_of(payload)}")
    return payload


def capture(function, *args, **kw):
    """Run `function` and return what it wrote, so that "the token is not echoed" can be tested."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        function(*args, **kw)
    finally:
        sys.stdout, sys.stderr = saved
    return out.getvalue() + err.getvalue()


# ------------------------------------------------------------------------ 1. the AST gate ------
def test_gate_keys_are_never_supplied() -> None:
    """The four promises, first: nothing in this package may write `confirm` or `arm`.

    A grep for the words would find the prose, the schemas and the tests, which is noise; the
    statement that matters is the one that *writes* the key. So the whole package is parsed and every
    dict key, keyword, and subscript assignment is inspected, wherever it is, and it is permitted in
    exactly two places: the schema declaration in `_build_tools`, and the module-level declaration of
    OPERATOR_ONLY_KEYS, which exists only so that this absence can be asserted.
    """
    allowed_functions = {"_build_tools"}
    offenders: list = []
    for name in ("mcp_server.py", "mcp_client.py"):
        path = os.path.join(_PKG, name)
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        enclosing: dict = {}

        def walk(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enclosing[child] = child.name
                    walk(child, child.name)
                    enclosing.pop(child, None)
                elif isinstance(child, ast.Assign):
                    if _is_operator_declaration(child):
                        continue
                    for target in child.targets:
                        if isinstance(target, ast.Subscript):
                            _inspect_key_node(name, owner, target.slice, child.lineno, offenders)
                    for value_node in ast.walk(child.value):
                        _inspect_key_node(name, owner, value_node, child.lineno, offenders)
                elif isinstance(child, ast.keyword) or isinstance(child, ast.Dict):
                    _inspect_key_node(name, owner, child, child.lineno, offenders)
                elif isinstance(child, (ast.Call, ast.Call)):
                    for keyword in child.keywords:
                        _inspect_key_node(name, owner, keyword, child.lineno, offenders)
                    walk(child, owner)
                else:
                    walk(child, owner)

        def _is_operator_declaration(node: ast.Assign) -> bool:
            return any(isinstance(t, ast.Name) and t.id == "OPERATOR_ONLY_KEYS" for t in node.targets)

        def _inspect_key_node(where: str, owner: str | None, node, line: int, into: list) -> None:
            if owner in allowed_functions:
                return
            keys: list = []
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            elif isinstance(node, ast.keyword):
                keys = [node.arg]
            elif isinstance(node, ast.Constant):
                keys = [node.value]
            elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                keys = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            elif isinstance(node, ast.Call):                       # dict(confirm=True) and friends
                keys = [k.arg for k in node.keywords if k.arg]
            for key in keys:
                if key in S.OPERATOR_ONLY_KEYS:
                    into.append(f"{where}:{line} in {owner or '<module>'} writes {key!r}")

        walk(tree, None)
    check("no code path in the server can supply confirm or arm", not offenders,
          "; ".join(offenders[:4]) if offenders else "scanned the package, found the keys nowhere")


def test_no_auto_approval_setting_exists() -> None:
    """And not even a setting: there is no switch to turn to make the server approve on its own."""
    tainted = sorted(set(S.CONFIG_KEYS) & set(S.OPERATOR_ONLY_KEYS))
    looks_automatic = [k for k in S.CONFIG_KEYS if any(w in k for w in
                     ("auto", "approve", "bypass", "skip_gate", "force"))]
    check("there is no setting by which the server may approve a gate automatically",
          not tainted and not looks_automatic,
          f"config keys {sorted(S.CONFIG_KEYS)}, of which none authorises a gate")


# ------------------------------------------------------------------- 2. connect, do not start --
def test_the_tool_table_and_the_absence_of_a_start_button() -> None:
    names = [t.name for t in S.TOOLS]
    check("every tool is named pickik_ and a command", all(n.startswith("pickik_") for n in names),
          f"{len(names)} tools")
    forbidden = [n for n in names if any(w in n for w in
                 ("start", "stop", "shutdown", "enable", "disable", "hw_", "_arm"))]
    check("no tool starts, stops, enables or moves hardware", not forbidden,
          f"searched for a way in and found none: {names[:1]} ... {names[-1:]}" if not forbidden
          else f"found {forbidden}")
    answered = S._answered_from_addon()
    check("one tool per answered command, plus the status tool and nothing else",
          len(S.TOOLS) == len(answered) + 1 and set(S.CMD_OF_TOOL.values()) == set(answered),
          f"{len(answered)} answered + 1 = {len(S.TOOLS)}")
    hw = [c for c in answered if c.startswith("hw_") or P.lookup(c).gate == P.GATE.ARM]
    check("no hardware or motion command is exposed by this server", not hw,
          f"the {len(P.COMMANDS)} declared commands, of which {len(hw)} reachable ones are excluded")


def test_the_status_tool_tells_a_human_how_to_start() -> None:
    empty = os.path.join(os.getenv("LOCALAPPDATA", os.path.expanduser("~")), "Temp")
    missing = os.path.join(empty, f"pickik-absent-{os.getpid()}-bridge.json")
    if os.path.isfile(missing):
        os.remove(missing)
    payload = call("pickik_bridge_status", {}, runtime_file=missing)
    text = json.dumps(payload)
    check("the status tool answers with no bridge at all", payload.get("ok") is True,
          f"running={payload.get('running')} found={payload.get('found')}")
    check("and when it is not running it says what a human must do",
          payload.get("running") is False and "start" in text.lower()
          and ("preference" in text.lower() or "panel" in text.lower() or "blender" in text.lower()),
          str(payload.get("how_to_start") or payload.get("why") or "")[:88])
    check("the status tool reports the protocol in use", payload.get("proto_rev") == P.proto_rev(),
          str(payload.get("version")))


# --------------------------------------------- 3. the reply shape, preserved verbatim ----------
def test_a_frame_is_sent_and_the_reply_comes_back_unchanged() -> None:
    with _tmpdir() as tmp:
        fake = FakeBridge(reply_over={"get_state": {"ok": True, "data": {
            "q": [0.1, -0.2, 0.3], "success": True, "position_error": 0.0006843,
            "note": "read from the live rig"}}})
        fake.start()
        record = fake.publish(tmp)
        payload = call("pickik_get_state", {"brief": True}, runtime_file=record)
        check("the call reached the bridge as one frame", len(fake.received) == 2,
              f"[hello, {fake.received[-1].get('cmd') if fake.received else 'nothing'}]")
        check("the frame carries the bare command, not the tool name",
              fake.received[-1].get("cmd") == "get_state"
              and fake.received[-1].get("id") is not None,
              json.dumps(fake.received[-1])[:92] if fake.received else "nothing was sent")
        check("the deadline the catalogue names is put on the wire",
              fake.received[-1].get("args", {}).get("deadline_ms")
              == P.lookup("get_state").deadline_ms,
              f"args={fake.received[-1].get('args') if fake.received else {}}")
        check("what the bridge answered comes back unchanged",
              payload.get("data") == {"q": [0.1, -0.2, 0.3], "success": True,
                                     "position_error": 0.0006843, "note": "read from the live rig"},
              "deep equality against the payload the rig produced")
        check("the agent's arguments were forwarded verbatim",
              fake.received[-1].get("args", {}).get("brief") is True, "no proxy opinion added")
        fake.halt()


def test_a_refusal_is_an_answer_and_not_a_malfunction() -> None:
    with _tmpdir() as tmp:
        fake = FakeBridge()
        fake.refuse = (P.ERR.TIMEOUT, "the bridge did not answer in time; call get_state to reconcile")
        fake.start()
        record = fake.publish(tmp)
        payload = call("pickik_set_target", {"target_xyz_mm": [300, 150, 300]}, runtime_file=record)
        code = str((payload.get("error") or {}).get("code"))
        check("E_TIMEOUT keeps its code, its message and its meaning",
              code == P.ERR.TIMEOUT == "E_TIMEOUT"
              and "reconcile" in json.dumps(payload).lower(),
              f"code={code} recovery={str(payload.get('recovery'))[:44]}")
        check("E_TIMEOUT is not treated as a malfunction of this server",
              not str(payload.get("code", "")).startswith("E_MCP_"), "no E_MCP_ prefix was invented")
        check("and it is not re-issued behind anyone's back", len(fake.received) == 2,
              f"the bridge heard {len(fake.received)} frames in total, one of them the hello")
        fake.halt()

    with _tmpdir() as tmp:
        busy = FakeBridge()
        busy.refuse = (P.ERR.BUSY, "the rig is busy with a motion")
        busy.start()
        record = busy.publish(tmp)
        payload = call("pickik_solve_ik", {"target_xyz_mm": [0.3, 0.15, 0.3]}, runtime_file=record)
        check("E_BUSY is data, delivered as a result and not as an error of the server",
              (payload.get("error") or {}).get("code") == P.ERR.BUSY
              and payload.get("retry_safe") is True and not str(payload.get("code", "")).startswith("E_MCP_"),
              f"retry_safe={payload.get('retry_safe')}")
        check("a busy rig is not retried by the proxy", len(busy.received) == 2,
              f"{len(busy.received)} frames on the wire")
        busy.halt()


def test_a_tool_that_is_not_a_tool_is_answered_without_being_name_resolved() -> None:
    with _tmpdir() as tmp:
        fake = FakeBridge()
        fake.start()
        record = fake.publish(tmp)
        payload = call("pickik_get_rig_state", {}, runtime_file=record)      # close, but not a tool
        check("a tool that does not exist is refused by the server", 
              str(payload.get("code", "")).startswith("E_MCP_"), str(payload.get("code")))
        check("and nothing at all is sent for it, because names are not resolved dynamically",
              len(fake.received) == 0, f"the wire carried {len(fake.received)} frames")
        fake.halt()


# -------------------------------------------------- 4. bounded, and marked so, never silent -----
def test_a_reply_is_bounded_and_says_so() -> None:
    """The reply bound, and the one thing that makes a bound worth having: it says when it fired.

    The fixture is sized out of the two constants it has to sit between, and that relation is itself
    asserted below. A magic `* 400` here once asked for 1.6 MB, which is over the frame cap both the
    bridge and the client enforce -- so the client rejected the frame as `overflow`, the payload never
    arrived, and the first check below was failing for a reason that had nothing to do with what it
    claimed to be testing. Sizes that only matter are stated as sizes, not typed in.
    """
    unit = 4096
    blobs = max(1, (C.MAX_FRAME_BYTES - 60_000) // unit)     # under the wire's cap, by intent
    big = {"blobs": ["x" * unit] * blobs}
    encoded = len(json.dumps(big).encode("utf-8"))
    check("the fixture is big enough to exercise the reply bound and small enough to cross the wire",
          S.MAX_RESULT_BYTES < encoded < C.MAX_FRAME_BYTES,
          f"{encoded} bytes, between a {S.MAX_RESULT_BYTES} reply bound and a "
          f"{C.MAX_FRAME_BYTES} frame cap")
    with _tmpdir() as tmp:
        fake = FakeBridge(reply_over={"get_state": {"ok": True, "data": big}})
        fake.start()
        record = fake.publish(tmp)
        payload = call("pickik_get_state", {}, runtime_file=record)
        check("an unbounded read is not permitted into somebody else's context",
              payload.get("data") == big, f"the {encoded}-byte payload survived the wire intact")
        blocks = S._result(payload)
        text = blocks[0].text
        check("and the bound is applied when it is rendered", len(text.encode("utf-8"))
              <= S.MAX_RESULT_BYTES + 1024, f"{len(text)} characters rendered")
        check("the truncation is announced and not concealed",
              "bounded" in text and "dropped" in text and "..." in text,
              text[-96:] if text else "nothing was rendered")
        small = S._result({"ok": True, "data": {"q": [0.0] * 7}})
        check("a reply that fits is passed through with nothing added",
              "bounded" not in small[0].text and "dropped" not in small[0].text, "no false alarm")
        fake.halt()


# --------------------------------------------------------------- the handshake, refused early --
def test_the_handshake_is_verified_before_anything_else() -> None:
    with _tmpdir() as tmp:
        fake = FakeBridge(proto_rev="0123456789ab")                     # an elder protocol
        fake.start()
        record = fake.publish(tmp, proto_rev="fedcba98765")              # and says so in its record
        payload = call("pickik_get_state", {}, runtime_file=record)
        check("a bridge speaking another protocol is refused before it is connected to",
              str(payload.get("why", "")).startswith("proto_rev:") and fake.accepted == 0,
              f"{fake.accepted} connections accepted, reason: {str(payload.get('why'))[:56]}")
        fake.halt()

    with _tmpdir() as tmp:
        fake = FakeBridge()
        fake.start()
        record = fake.publish(tmp, host="10.20.30.40")                  # not loopback at all
        payload = call("pickik_get_state", {}, runtime_file=record)
        check("a route out of the loopback is refused",
              str(payload.get("why", "")).startswith("endpoint:") and fake.accepted == 0,
              str(payload.get("why"))[:72])
        fake.halt()

    with _tmpdir() as tmp:
        #: The bridge keeps the true token and the record is made to lie about it. Both were use given
        #: the one wrong value, which left client and bridge in perfect agreement and the refusal this
        #: check exists to observe impossible: a session cannot be refused for a token by a bridge that
        #: has never been told a different one. The fault is now on the record's side alone, where the
        #: test meant it, and `_answer` is read to refuse `E_ACCES` on exactly that disagreement.
        fake = FakeBridge()
        fake.start()
        record = fake.publish(tmp, token="a-different-token")
        payload = call("pickik_get_state", {}, runtime_file=record)
        check("an unauthorised session is refused by the bridge and said so",
              "auth" in json.dumps(payload).lower() or "acces" in json.dumps(payload).lower(),
              f"why={str(payload.get('why'))[:56]}")
        fake.halt()


def test_the_token_is_never_echoed() -> None:
    with _tmpdir() as tmp:
        fake = FakeBridge()
        fake.start()
        record = fake.publish(tmp)
        printed = capture(lambda: (print(json.dumps(call("pickik_bridge_status", {},
                                                       runtime_file=record))),
                                   print(json.dumps(call("pickik_get_state", {}, runtime_file=record))),
                                   print(S.main(["--check", "--runtime-file", record]))))
        check("the shared secret does not appear in anything the server prints",
              TOKEN not in printed, f"searched {len(printed)} characters of output")
        fake.halt()


# ------------------------------------------------------------------- the SDK wiring, in situ ---
def test_the_server_is_wired_and_speaks_mcp() -> None:
    try:
        server = S.build_server()
    except BaseException as exc:
        check("the server can be built", False, repr(exc))
        return
    check("the server can be built", True, f"{S.SERVER_NAME} {S.SERVER_VERSION}")
    #: What the server advertises is not asked out of the SDK. It is the record this module hands to
    #: `server.run` itself, which is the only account of "advertises" a client can ever be given, and the
    #: same reason this file refuses to pin `_request_handlers` a few lines below. What stood here asked
    #: the SDK instead, and asked it wrongly: the method is
    #: `get_capabilities(notification_options, experimental_capabilities)`, takes two positional
    #: arguments, and was handed two empty dicts. The SDK reads `.tools_changed` off the first -- but
    #: only down the branch where a `tools/list` handler has been registered, which a bare `Server` has
    #: not and `build_server()` has. That is why it stood up as a fault of the server in this one test
    #: of the fifteen and in no other, and why it took the registering to see that the server was
    #: innocent of it, the mis-call having been made in the checks.
    advertised = S.initialization_options().capabilities
    check("it advertises the tools capability", getattr(advertised, "tools", None) is not None,
          f"{type(advertised).__name__} with tools={advertised.tools!r}"[:150])
    # The SDK keys its dispatch table by the request *type*, and not by the wire's own words: what the
    # object holds is `{ListToolsRequest: .., CallToolRequest: .., PingRequest: ..}`. What stood here
    # searched the keys of every mapping on the object for the strings `tools/list` and `tools/call`,
    # which is not how they are ever spelled, and so reported "no registry located on the object" -- a
    # fault of the looking and not of the server, the two handlers having been registered all along by
    # `build_server` itself. The types are therefore taken as the SDK names them, by name, and what was
    # located is printed out, so that an SDK bump which moves them says so here instead of going quiet.
    located: set = set()
    for _name, value in vars(server).items():
        if isinstance(value, dict):
            located |= {str(getattr(one, "__name__", one)) for one in value}
    served = {"ListToolsRequest", "CallToolRequest"}
    check("it answers both tools/list and tools/call", served <= located,
          f"dispatch is registered for {', '.join(sorted(located)) if located else 'nothing'}"[:150])
    check("the instructions are the safety briefing, not a footer",
          "never an instruction" in S.INSTRUCTIONS and "E_TIMEOUT" in S.INSTRUCTIONS,
          f"{len(S.INSTRUCTIONS)} characters the client is told at initialize")


def test_an_unrecognised_option_is_not_ignored() -> None:
    for name, bad in (("a typo", {"auto_approve_all": True}),
                      ("a gate bypass", {"skip_gate": True}),
                      ("a misspelt key", {"runtime_fiel": "x"})):
        try:
            S.build_server(bad)
            check(f"an unrecognised option is refused ({name})", False, "it was accepted silently")
        except SystemExit as exc:
            check(f"an unrecognised option is refused ({name})", True, str(exc)[:72])


def test_the_answered_set_is_read_and_not_transcribed() -> None:
    answered = S._answered_from_addon()
    path = os.path.join(S.ADDON_DIR, "mcp_handlers_obs.py")
    src = open(path, encoding="utf-8").read()
    import re
    #: An independent reading of the same registry. The server reaches it by parsing the syntax tree;
    #: this reads it by pattern. Two methods agreeing is the check -- one method alone is only a
    #: restatement of itself. (The first version anchored every key to a four-space indent, so it saw
    #: only the first entry on each line: 14 keys in the tree, 5 by the pattern, and the disagreement
    #: was in the test and not in the add-on.)
    declared = set(re.findall(r"[\"']([a-z][a-z_]+)[\"']\s*:", src)) & set(P.COMMANDS)
    check("the answered set is read out of the add-on, not copied into the server",
          set(answered) == declared and all(c in P.COMMANDS for c in answered),
          f"{len(answered)} by the syntax tree, {len(declared)} by the pattern")
    check("the tool table and the answered set agree with each other",
          set(S.CMD_OF_TOOL.values()) == set(answered), f"{len(S.CMD_OF_TOOL)} names bound")


# ------------------------------------- the checks that make this suite's own faults visible ----
def test_every_module_attribute_reached_for_actually_exists() -> None:
    """Ask the interpreter, rather than remembering, because remembering is what broke this suite once.

    Ten of the first run's failures were not about the server at all: the code had been written with
    `json.decode` where there is `json.loads`, and `ast.iter_children` where there is
    `iter_child_nodes`. Neither could be found by looking -- reading the file back rendered the same
    names wrong, and grepping the standard library rendered those wrong too -- so a pair of eyes was
    worth nothing here, and ten greps less. What is done instead is to take the names out of the very
    syntax of the three files and resolve each one against the module it is reached through.
    """
    import importlib
    problems: list = []
    #: Each file is read where it actually is. The suite's own file sits in tests/ beside this checker and
    #: not in the package a level above, so asking for it at _PKG asked for a file that has never once
    #: existed: the checker whose whole purpose is to make the suite's own faults visible had itself
    #: never been read, and had been reporting that it could not be read in a detail clipped to three
    #: problems since the first run. A tool that examines the tests has to include the file it is writ in.
    for name in ("mcp_client.py", "mcp_server.py", os.path.basename(__file__)):
        path = (os.path.realpath(__file__) if name == os.path.basename(__file__)
                else os.path.join(_PKG, name))
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        except OSError as exc:
            problems.append(f"{name}: cannot be read ({exc})")
            continue
        imported = {a.asname or a.name.split(".")[0]: a.name
                    for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base, parts = node, []
            while isinstance(base, ast.Attribute):
                parts.insert(0, base.attr)
                base = base.value
            if not isinstance(base, ast.Name) or base.id not in imported:
                continue                       # an attribute of an object, which is not our business
            dotted, attr = imported[base.id], parts[-1]
            #: The chain below is walked, and not imported. `import_module` used to be asked for
            #: `sys.path`, for `os.environ`, and now for `ctypes.windll`, each of which is an attribute of
            #: a module and not one of which is a module: the checker was resolving the road and not the
            #: house, and reporting every street it could not import as an attribute that was not there.
            #: The root is therefore imported once, and what hangs off it is gone to and looked at, which
            #: is the single way a thing that is not a module can ever be reached.
            try:
                holder = importlib.import_module(dotted)
                for step in parts[:-1]:
                    holder = getattr(holder, step)
            except (ImportError, ValueError) as exc:
                problems.append(f"{name}:{node.lineno}: {dotted} will not import ({exc})")
                continue
            except AttributeError as exc:
                problems.append(f"{name}:{node.lineno}: {dotted}.{'.'.join(parts)} is not there ({exc})")
                continue
            if not hasattr(holder, attr):
                problems.append(f"{name}:{node.lineno}: {dotted}.{attr} is not there; nearest "
                               f"{difflib.get_close_matches(attr, dir(holder), n=2, cutoff=0.7)}")
    #: Reported to the sixth and not to the third. A detail that hides the faults it was struck to
    #: report is the same silence as a tally that counts the tests that never ran, and for the same
    #: reason: the reader sees an end where there is only a place where the writing stopped.
    check("every module attribute the code reaches for actually exists", not problems,
          " | ".join(_clip(p, 160) for p in problems[:6]) if problems
          else "resolved against the modules, not out of anybody's memory")


def test_the_two_copies_of_the_protocol_have_not_drifted() -> None:
    """The vendored copy must still be the add-on's own bytes, and no document may quote a revision this
    build does not produce.

    The handshake compares `proto_rev` at connect, and that is the protection that matters. A stale
    revision sitting in prose is a different fault and a quieter one: it teaches a reader to expect a
    value that never existed. Both are read here out of the files as they are, which is the only
    honest source for either.
    """
    import hashlib, re as pattern
    def digest(path: str) -> str:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]
    vendored = digest(os.path.join(_PKG, "vendored", "mcp_protocol.py"))
    original = digest(os.path.join(S.ADDON_DIR, "mcp_protocol.py"))
    check("the vendored protocol copy is still the add-on's own bytes", vendored == original,
          f"the two files digest to {vendored} and {original}")
    #: The guard hashes the dialect, and used to fold into it the bookkeeping that vars() carries on a
    #: class beside its members -- among those the module's own import name, which reads
    #: "blender_ik_addon.mcp_protocol" inside Blender and "mcp_protocol" out of the vendored copy. The
    #: two ends of one socket, on one file, on one day, then computed two revisions, and every
    #: handshake died at E_PROTO before a command was ever spoken. Nothing in this suite could see it,
    #: because both of its bridges were built from the same copy under the same name and so agreed
    #: with each other instead of with the pair they stand for. One file, reached two ways, must agree.
    import types as _types
    def _reached_as(name):
        path = os.path.join(_PKG, "vendored", "mcp_protocol.py")
        mod = _types.ModuleType(name); mod.__file__ = path
        exec(compile(open(path, encoding="utf-8").read(), path, "exec"), mod.__dict__)
        return mod
    bare, qualified = _reached_as("mcp_protocol"), _reached_as("blender_ik_addon.mcp_protocol")
    check("the drift guard depends on the dialect alone, not on how the module was reached",
          bare.proto_rev() == qualified.proto_rev() == P.proto_rev(),
          f"reached as mcp_protocol it says {bare.proto_rev()}, as the add-on's submodule "
          f"{qualified.proto_rev()}, and the running build says {P.proto_rev()}")
    doc = os.path.join(_PKG, "SERVER_DESIGN.md")
    quoted = set()
    try:
        quoted = set(pattern.findall(r"proto_rev[^0-9a-f]{0,6}([0-9a-f]{12})",
                                     open(doc, encoding="utf-8").read()))
    except OSError:
        pass
    stale = sorted(one for one in quoted if one != P.proto_rev())
    check("no document quotes a revision this build does not produce", not stale,
          f"the build produces {P.proto_rev()}"
          + (f"; the docs also carry {stale}" if stale else ", and the docs agree"))


def _tmpdir():
    """A context manager for one private directory, removed on exit."""
    import contextlib, tempfile

    @contextlib.contextmanager
    def make():
        path = tempfile.mkdtemp(prefix="pickik-test-")
        try:
            yield path
        finally:
            for entry in os.listdir(path):
                try:
                    os.remove(os.path.join(path, entry))
                except OSError:
                    pass
            os.rmdir(path)

    return make()


#: How long one test may take before it is called out and the run ends. A socket suite that wedges on a
#: read would otherwise sit there waiting, and a run that waits looks, from outside, exactly like a
#: crash -- so the suite decides instead, says which test it was, where each thread is, and leaves.
#: Eight seconds is the built-in default: ample for a check whose farthest destination is a loopback
#: socket, and short enough that a reply which is merely slow is not mistaken for one never coming.
#: The environment may still overrule it, which is worth keeping for the day a machine is slow and the
#: report needs to be read without a timer interrupting it.
WATCHDOG_S = float(os.getenv("PICKIK_TEST_WATCHDOG_S", "8"))


#: Where the watchdog's report goes, and the reason it is a file and not a line of stdout: the one
#: shape a hang can have that would defeat an in-process reporter is a main thread parked on a write
#: into a pipe nobody is draining, and a print() from the timer would then queue behind the very
#: blockage it was sent to describe. A descriptor opened here belongs to no buffer, no lock, and no
#: other thread. Redirect it with PICKIK_TEST_WATCHDOG_DUMP if the tree cannot be written to.
WATCHDOG_DUMP = os.getenv("PICKIK_TEST_WATCHDOG_DUMP", os.path.join(_HERE, "watchdog-dump.log"))


#: How many interrupts had already been waiting when the run began. A KeyboardInterrupt that arrives
#: while a person is watching is a request to stop; one that has been waiting in the process since
#: before the run began is an event, and the two want opposite things done to the run. The runner below
#: has not been able to tell them apart, which is how eight tests went unexecuted under a tally that
#: read as thirteen -- and which is why the count is taken before any test runs, where it can be had.
_startup_interrupts: list = [0]


def _note_startup_interrupt(signum, frame) -> None:
    """Count the interrupt, and do not obey it: this is the place it is counted, not the place stopped."""
    _startup_interrupts[0] += 1


def _sweep_pending_interrupt() -> tuple:
    """Let an interrupt that has been waiting since before the run began arrive here, where it is
    countable, rather than at the first blocking call, where it reads exactly like a person.

    Two things come back, and they answer different questions. What was *found* on entry answers who was
    answering `SIGINT` when we got here: an import ahead of us -- the SDK, the validator, anything with a
    command line in it -- may have installed its own handler, and then a Ctrl+C does not mean what the
    runner assumes it means. How many *arrived* answers whether the fault predated the tests at all,
    which is the distinction the last three runs have been unable to make. The sleeps are not a wait for
    an event that may still come; they are the look CPython takes for one already here.
    """
    try:
        found = signal.getsignal(signal.SIGINT)
    except BaseException as exc:
        return ("unreadable", f"the disposition could not be read: {exc!r}")
    arrived = "not counted"
    try:
        signal.signal(signal.SIGINT, _note_startup_interrupt)
        for _ in range(150):
            time.sleep(0.002)                                   # the look for a pending interrupt
        arrived = _startup_interrupts[0]
    except BaseException as exc:
        arrived = f"the sweep itself went wrong: {type(exc).__name__}: {exc}"
    finally:
        try:
            signal.signal(signal.SIGINT, found)                 # put back exactly what was found
        except BaseException:
            pass
    return (f"{type(found).__name__}:{getattr(found, '__name__', '?')}", arrived)


#: How many times a signal was truly heard over the whole of the run, and who was answering it at the
#: instant of each hearing. This is the discriminator that does not need a theory: a signal genuinely
#: delivered must pass through whatever handler is installed, so a KeyboardInterrupt that appears where
#: the count did not move is not a signal at all but a raise out of Python code, and the two are faults
#: of a different order. The disposition is taken at the instant of delivery and not before the run,
#: because a library may install its own answer late, on first use, and then a Ctrl+C means what that
#: library says that it means.
_signals_heard: list = [0]
_dispositions_at_delivery: list = []
_prior_sigint_handler = None
#: How many tests reported an interrupt, to be set against how many signals were truly heard. The gap
#: between the two numbers, not either number alone, is what the next run is being run to read.
interrupted_tests: list = [0]


def _note_and_obey_interrupt(signum, frame) -> None:
    """Record the hearing in full, then raise -- so the run may respond as it would have done.

    The thread and the stack at the hearing are the whole of the distinction still open. A signal raised
    from inside the process is heard upon the thread that raised it, within that thread's own call stack,
    and the stack therefore names the raiser. A signal from the console is heard upon the main thread,
    parked where it happened to be standing, and the stack names nothing but the wait. The one is a bug
    with an author; the other is the terminal, and there is nothing in the package to correct.
    """
    _signals_heard[0] += 1
    who, where = "<unknown>", ""
    try:
        who = threading.current_thread().name
        if frame is not None:
            #: The frame the interpreter was executing when the signal was checked, handed over by the
            #: machinery itself, and the only datum that reaches beneath the C boundary. Calibrated so:
            #: taking the current frame out of `_current_frames` instead is useless here, because at the
            #: moment of the hearing that frame is this handler's own, the handler having been called out
            #: of C -- both known causes then left the same record, and nothing could be told apart.
            where = "".join(traceback.format_stack(frame))
        else:
            own = getattr(sys, "_current_frames", lambda: {})().get(threading.current_thread().ident)
            where = "".join(traceback.format_stack(own)) if own is not None else "<no frame given>"
    except BaseException as exc:                            # a record that raises is no record at all
        where = f"<the hearing could not be taken: {type(exc).__name__}>"
    _dispositions_at_delivery.append(f"upon {who}")
    try:
        #: The deepest frame is the whole of the evidence, and it goes into the record itself rather than
        #: only into the journal, because a datum that has to be read out of a second file with a filter
        #: over its lines is a datum that can be misread -- as one just was, the stack riding upon the
        #: lines that the filter dropped. `format_stack` renders the most recent call last, so the last
        #: `File` line is where the hearing actually was.
        deepest = [one.strip() for one in where.splitlines() if one.strip().startswith("File ")]
        _dispositions_at_delivery[-1] = f"upon {who} :: {deepest[-1] if deepest else '<no frame>'}"
    except BaseException:
        pass
    _journal("signal", f"SIGINT heard, number {_signals_heard[0]}, upon the thread {who}")
    _journal("signal", f"  the stack at the hearing, most recent last:\n{_clip(where, 1_200)}")
    raise KeyboardInterrupt()


def _what_the_bridge_saw() -> str:
    """The live fake bridge's own account of this test, gathered without disturbing it.

    Which end is wedged separates the hypotheses, and the readings are plain: never accepted means the
    accept loop is the wedge; the hello heard and nothing after it means the time went into the command
    frame or its reply; frames heard with no reply read means the client is where to look. Every field
    is guarded, because a probe that raises inside the one diagnostic that matters is a probe that
    should not have been written.
    """
    fake = _LIVE_FAKE[0]
    if fake is None:
        return "no fake bridge was live for this test, so there was nothing on the wire to be seen"
    try:
        heard = [str(frame.get("cmd")) for frame in list(fake.received) if isinstance(frame, dict)]
        stopper = fake.stopper
        steps = [f"      {one}" for one in list(fake.log)[-14:]]
        return (f"accepted={fake.accepted} frames_heard={len(heard)} {heard} "
                f"alive={fake.is_alive()} stopper_set={stopper.is_set() if stopper else 'n/a'}\n"
                + ("      the bridge kept no log of anything it heard" if not steps
                   else "    its own account, in the order it heard it:\n" + "\n".join(steps)))
    except BaseException as exc:
        return f"the bridge could not be read: {exc!r}"


def _arm_watchdog(label: str) -> threading.Timer:
    def fire():
        # A Timer is already a thread of its own, so a main thread asleep in recv() -- and every
        # blocking socket call releases the GIL, which is what makes this reach at all -- cannot keep it
        # from running. What no in-process observer can reach is a wedge that never yields the GIL, and
        # for that shape the remedy has stayed what it was: Ctrl+C, which the runner below hears.
        fd, report = -1, []
        try:
            frames = getattr(sys, "_current_frames", lambda: {})()
            names = {one.ident: one.name for one in threading.enumerate()}
            main = threading.main_thread().ident
            report.append(f"\n==== WATCHDOG: {label} did not finish within {WATCHDOG_S} s "
                          f"[pid {os.getpid()}, at {time.strftime('%Y-%m-%dT%H:%M:%S')}] ====")
            for ident, frame in frames.items():
                report.append(
                    f"\n-- thread {ident} {names.get(ident, '?')}"
                    + ("  <== the main thread; its deepest line is the answer" if ident == main else "")
                    + " --\n" + _clip("".join(traceback.format_stack(frame)), 1_400))
            report.append("\n-- what the bridge saw --\n" + _what_the_bridge_saw())
            payload = "\n".join(report).encode("utf-8", "replace")
            fd = os.open(WATCHDOG_DUMP, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
            while payload:
                written = os.write(fd, payload)                    # partial writes are normal, not fatal
                if written <= 0:
                    break
                payload = payload[written:]
        except BaseException as exc:                                # report what you can, then go
            report.append(f"!! the watchdog could not write its report: {exc!r}")
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except BaseException:
                    pass

        #: The file is the record; what follows is only a courtesy to the terminal. A courtesy must not
        #: be able to take the run hostage, so the leave is armed on a timer of its own first: should a
        #: print into a wedged stdout block here, this still goes off and the run still ends.
        guarantor = threading.Timer(2.0, lambda: os._exit(3))
        guarantor.daemon = True
        guarantor.start()
        try:
            print(f"!! WATCHDOG: {label} did not finish within {WATCHDOG_S} s. The thread report is in "
                  f"{WATCHDOG_DUMP}, and it is reproduced below.", flush=True)
            for line in report:
                print(_clip(line, 1_600), flush=True)
        except BaseException as exc:
            try:
                print(f"!! the watchdog wrote {WATCHDOG_DUMP} and then could not speak: {exc!r}",
                      flush=True)
            except BaseException:
                pass
        os._exit(3)

    timer = threading.Timer(WATCHDOG_S, fire)
    timer.daemon = True
    timer.start()
    return timer


#: This suite drives threads and sockets. In this shape it took the harness down more than once while
#: it was being written, and until it has completed a run in front of somebody who can read its report
#: it is not a suite to be reached by a blanket "run everything". So it is opt-in: nothing here runs
#: unless a person asks for it by name, and the refusal says why out loud instead of exiting quietly --
#: a suite that skips without notice is how a broken suite comes to be believed green.
RUN_FLAG = "PICKIK_RUN_SERVER_TESTS"
#: Whether the suite runs when nobody asks it to. Hardcoded on: a suite that needs an environment set
#: up before it can be run is a suite that does not get run, and this one has to be run, because
#: nothing written about the server is believed until this has passed.
#:
#: This turns the gate the other way round; it does not remove it. The reason it was built has not
#: gone away -- the file drives threads and sockets, and in an earlier shape it took a harness down.
#: Set RUN_UNATTENDED = False below, or set PICKIK_RUN_SERVER_TESTS=0 in the environment, and the
#: suite declines again until somebody asks for it by name. Both are kept where a reader can see them.
RUN_UNATTENDED = True
AFFIRMATIVE = ("1", "true", "yes", "on")
REFUSING = ("0", "false", "no", "off")

#: `_last_stop_ask` stood here, and the clock in it was what decided who was a person and who a fault,
#: judged from the spacing of presses. It is taken away. The measure was made, four deliveries two
#: seconds apart were read as a fault of the code, and a run that ought to have stopped ran on; an
#: interrupt is therefore obeyed, without being timed, so there is nothing left for a clock to decide.


def _declined() -> bool:
    """True when this run is being declined, with the reason said where it cannot be missed.

    A word from the environment always outscores the built-in, in either direction: the constant is a
    default for people who simply run the file, and a default must never outvote somebody who has said
    no on the command line.
    """
    word = os.getenv(RUN_FLAG, "").strip().lower()
    #: Taken in one place and returned in the same place, which is the whole of the difference between a
    #: notice and a dead letter. What stood here was a `return` above the notice, so the notice was
    #: unreachable and a re-armed gate exited quietly: a person who set the constant to make the suite
    #: ask leave would be told nothing, and would not know why. The word from the environment still
    #: outscores the built-in, in either direction, and a default must never outvote somebody who has
    #: said no on the command line.
    if word in REFUSING:
        declined = True
    elif word in AFFIRMATIVE:
        declined = False
    else:
        declined = not RUN_UNATTENDED
    if declined:
        print(
            f"\n== SKIPPED, DELIBERATELY: {os.path.basename(__file__)} ==\n"
            "   It drives threads and sockets. Two of its early crashes took the harness down: a thread\n"
            "   hook written with the wrong arity, and diagnostics that were not bounded. Both are mended,\n"
            "   and the run has since completed in front of somebody, twice over and once again; the ban\n"
            "   on running it through that shell stands, because it was the shell and not the suite.\n"
            f"   To run it, and it will then tell you what it found: set {RUN_FLAG}=1 in the environment\n"
            "   and invoke it from a terminal, where the report can be read.\n", flush=True)
    return declined


def main() -> int:
    if _declined():
        return 0                                            # a deliberate skip is not a failure
    print(f"== pickik mcp server: {S.SERVER_VERSION} ==", flush=True)
    #: Reset the record before the run and say where it is kept. A report nobody knows the name of is a
    #: report nobody reads, and a stale one left over from the last run is worse than none, because it
    #: says where the last hang was and not where this one was.
    try:
        with open(WATCHDOG_DUMP, "w", encoding="utf-8") as log:
            log.write(f"== watchdog log for {S.SERVER_VERSION}, begun "
                      f"{time.strftime('%Y-%m-%dT%H:%M:%S')} ==\n")
    except OSError as exc:
        print(f"!! the watchdog has nowhere to write: {exc!r}", flush=True)
    print(f"   thread reports, should there be any, go to {WATCHDOG_DUMP}", flush=True)
    #: The journal, same discipline as the watchdog's report: reset, so the lines in it belong to this
    #: run and not to the hang before it, and its name said out loud, so it is read and not missed.
    try:
        with open(JOURNAL, "w", encoding="utf-8") as log:
            log.write(f"# journal for {S.SERVER_VERSION}, begun "
                      f"{time.strftime('%Y-%m-%dT%H:%M:%S')}; the events, in the order they happened\n")
    except OSError as exc:
        print(f"!! the journal has nowhere to write: {exc!r}", flush=True)
    print(f"   the event journal, and the bridge's own log, go to {JOURNAL}", flush=True)
    #: Taken before the first test, because an interrupt found waiting there is a different fault from
    #: one made during the round trip, and the two have been read as one for three runs now.
    disposition, arrived = _sweep_pending_interrupt()
    print(f"   SIGINT was answered by {disposition} on entry; interrupts already waiting: {arrived}",
          flush=True)
    _journal("run", f"startup sweep: SIGINT answered by {disposition}, already waiting: {arrived}")
    #: Installed for the duration of the run and taken down again after it, so that the count is the
    #: run's own and the machine's disposition is left as it was found.
    global _prior_sigint_handler
    try:
        _prior_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _note_and_obey_interrupt)
        _journal("run", "SIGINT is being watched for the duration of the run")
    except BaseException as exc:
        print(f"!! SIGINT could not be watched, so the count below will read as nothing: {exc!r}",
              flush=True)
    tests = [test_every_module_attribute_reached_for_actually_exists,
             test_the_two_copies_of_the_protocol_have_not_drifted,
             test_gate_keys_are_never_supplied, test_no_auto_approval_setting_exists,
             test_the_tool_table_and_the_absence_of_a_start_button,
             test_the_status_tool_tells_a_human_how_to_start,
             test_a_frame_is_sent_and_the_reply_comes_back_unchanged,
             test_a_refusal_is_an_answer_and_not_a_malfunction,
             test_a_tool_that_is_not_a_tool_is_answered_without_being_name_resolved,
             test_a_reply_is_bounded_and_says_so, test_the_handshake_is_verified_before_anything_else,
             test_the_token_is_never_echoed, test_the_server_is_wired_and_speaks_mcp,
             test_an_unrecognised_option_is_not_ignored, test_the_answered_set_is_read_and_not_transcribed]
    #: Which tests were actually entered. Kept so that a run cut short can say what is missing from it,
    #: and not merely how many checks it happened to make: the last such run read "13 checks" while eight
    #: tests had never been entered at all, and nothing on the page said so.
    executed: list = []
    stopped_early: list = [None]
    for test in tests:
        _journal("test", f"BEGIN {test.__name__}")
        executed.append(test.__name__)
        timer = _arm_watchdog(test.__name__)
        try:
            test()
            _journal("test", f"END {test.__name__}")
        except KeyboardInterrupt:        # heard, counted, and believed as a person only when pressed twice
            _journal("test", f"INTERRUPTED {test.__name__}")
            interrupted_tests[0] += 1
            #: An interrupt is obeyed. Not absorbed, and not weighed against a guess about who sent it.
            #:
            #: The handler used to assume a person and break, which spared eight tests under a tally that
            #: read as thirteen: a suite reporting itself nearly green while most of it had never been
            #: entered. The remedy then went too far, inferring an operator from the spacing of presses --
            #: two within two seconds meant a person, one meant a fault of the code, and the run went on.
            #: Measured, that rule misfired on its own ground: four genuine deliveries, two seconds apart,
            #: every one from a source that was neither a hand nor a fault, and the run that ought to have
            #: stopped ran to the end of the page against the wishes of the only party able to ask. Intent
            #: cannot be read out of an interval. So one interruption stops, and what it cost is reported
            #: below in the open, which is the whole of the difference between a short halt and a fault.
            where = _clip(traceback.format_exc(), 1_800)
            _journal("interrupt", f"{test.__name__} was raised out of: {where}")
            print(f"!! where the interrupt came from, as the interrupting frame reported it:\n{where}",
                  flush=True)
            check(test.__name__, False, "interrupted while it was running; see the frame above")
            skipped = [one.__name__ for one in tests[tests.index(test) + 1:]]
            stopped_early[0] = (test.__name__, skipped)
            _journal("test", f"STOP HEARD at {test.__name__}, {len(skipped)} left unexecuted")
            print(f"!! one interruption stops the run: {test.__name__} was cut short, and {len(skipped)} "
                  f"test(s) after it will not execute", flush=True)
            break
        except Exception as exc:                              # a test that raises is a failed test
            _journal("test", f"RAISED {test.__name__}: {type(exc).__name__}: {exc}")
            check(test.__name__, False, f"{type(exc).__name__}: {exc}")
        finally:
            timer.cancel()
            S._close_bridge()
    #: A run that was cut short is reported as a run that was cut short, before the tally is struck and
    #: not after: the whole complaint against the version that read "13 checks" was that the twelve tests
    #: it had never entered were absent from the page as well as from the run. Each is entered here as a
    #: failure, in the company of the tests that genuinely failed, so that the two cannot be told apart
    #: by anyone reading the summary -- which is the only reader the summary ever has.
    if stopped_early[0] is not None:
        cut, never_entered = stopped_early[0]
        print(f"!! THE RUN DID NOT FINISH. It was interrupted at {cut}; these {len(never_entered)} "
              f"test(s) were never entered, and are recorded below as failures rather than hidden: "
              f"{', '.join(never_entered) or '(none)'}", flush=True)
        for one in never_entered:
            check(one, False, "never executed: the run was interrupted before it was reached")
    failed = [n for n, ok, _ in CHECKS if not ok]
    #: Flushed, which the first version of this line was not, and that is the reason a run can have
    #: left a log holding every check and no tally at the end of it: an unflushed line is a line that
    #: goes with the process, and this is the one line a reader looks for first.
    print(f"\n== {len(CHECKS) - len(failed)} passed, {len(failed)} failed, {len(CHECKS)} checks ==",
          flush=True)
    for name, _ok, detail in ((n, o, d) for n, o, d in CHECKS if not o):
        print(f"  FAILED {name} -- {detail}", flush=True)
    if stopped_early[0] is None:
        _journal("run", f"reached the end of the run: {len(CHECKS)} checks, {len(failed)} failed")
    else:
        #: The sentence the journal must never be allowed to say when it is not true, which is what the
        #: last such entry did: a run interrupted at its first live round trip, journalled as having
        #: reached the end. Not on this watch.
        _journal("run", f"DID NOT reach the end of the run: cut short at {stopped_early[0][0]}; "
                        f"{len(CHECKS)} checks were recorded, {len(failed)} of them failed")
    #: The answer to the question the last four runs have been circling: were those interrupts signals
    #: at all. Nothing heard here, with the handler installed, means nothing was a signal.
    heard = _signals_heard[0]
    print(f"== SIGINT was truly heard {heard} time(s) while {interrupted_tests[0]} test(s) reported an "
          f"interrupt; answering at the time: {sorted(set(_dispositions_at_delivery)) or 'nobody'} ==",
          flush=True)
    _journal("run", f"heard {heard} signal(s) against {interrupted_tests[0]} reported interrupt(s)")
    try:
        signal.signal(signal.SIGINT, _prior_sigint_handler)   # leave the machine as it was found
    except BaseException:
        pass
    faulthandler.disable()                     # a clean exit should not be able to look like a fault
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
