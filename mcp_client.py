"""The server's side of the loopback link to the in-Blender bridge.

Nothing in here imports the MCP SDK and nothing in here imports `bpy`: this module is importable from
plain system Python, it is testable against a fake bridge with no Blender at all, and it is the only
place that knows how a frame is put on and taken off the socket.

Three things this file is responsible for, because they were each measured to be a trap on Windows:

* **Framing.** One JSON object per line, `\n`-delimited, UTF-8, no embedded newline (the codec in
  `mcp_protocol` refuses to encode one). Reads are buffered and split, never `recv`-per-request.
* **Closing.** `close()` after `sendall` can reset instead of flushing, and closing a socket that
  still holds unread bytes makes WinSock send an RST and discard the reply in flight. So a shutdown of
  the write side, then the read side, then `close()` -- see `_bye`.
* **The clock.** Every interval here is `perf_counter`, never `monotonic`: the latter is
  `GetTickCount64` on Windows and measures in 16 ms steps, which is four times the tick budget.
* **Liveness.** `os.kill(pid, 0)` is not a test here but a broadcast, because on Windows the second
  argument is read as a console control event and `0` of those is `CTRL_C_EVENT`. Ask `kernel32`
  instead, and believe only a termination that was seen -- see `_pid_is_running`, where both halves of
  that sentence were paid for in a run of the suite that kept being interrupted by nobody.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
for _cand in (os.path.join(_HERE, "vendored"), os.environ.get("PICKIK_ADDON_TREE", "").strip() or None):
    if _cand and os.path.isfile(os.path.join(_cand, "mcp_protocol.py")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

import mcp_protocol as P  # noqa: E402  (vendored copy, or the add-on tree when that is pinned)

#: Where the bridge publishes its live endpoint. `~`, never a hard-coded port: 9876 is only the
#: default behind the add-on's 'Port' property, edited in the PickIK sidebar box and not on the
#: preferences page, and reading this file is the only way to follow someone who moved it.
RUNTIME_FILE = os.path.expanduser(os.path.join("~", ".pickik", "bridge.json"))
CLIENT_NAME = "pickik-mcp-server"
GRACE_MS = 250              # transport patience on top of the bridge's own deadline
CONNECT_TIMEOUT_S = 3.0
HEADER_TIMEOUT_S = 5.0
MAX_FRAME_BYTES = 1_048_576


class BridgeGone(RuntimeError):
    """The link itself failed: refused, reset, timed out, or was never there.

    Distinct from a *rejected command*, which is a well-formed reply carrying an `E_*` code and is
    returned, not raised. Conflating the two is how a transport hiccup ends up reported to a surgeon
    as a refusal by the rig."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


def _bye(sock: socket.socket) -> None:
    """Half-close both directions before releasing the fd. See the module docstring."""
    try:
        for how in (getattr(socket, "SHUT_WR", 1), getattr(socket, "SHUT_RD", 0)):
            try:
                sock.shutdown(how)
            except OSError:
                break                       # already gone; nothing further to say
    finally:
        try:
            sock.close()
        except OSError:
            pass


#: Reading a process as alive without asking it to interrupt anybody.
#:
#: `os.kill(pid, 0)` is the received idiom for this, and on Windows it is a hazard and not a test: there
#: the second argument is not a signal number at all but a console control event, and `0` of it is
#: `CTRL_C_EVENT`. Measured on this machine, both ways round, and neither answer is about liveness:
#:
#: * **accepted**, and a Ctrl+C goes out to every process sharing the console. This is how a run of the
#:   suite came to be interrupted four times by a hand that was not on the keyboard, each interruption
#:   surfacing at the next blocking `recv` in the very same stack -- `read_runtime` fires the event, and
#:   `connect` is what then waits, so the fault always appeared to belong to the read and not to the
#:   probe that had just preceded it;
#: * **denied**, and it raises -- whereupon the record was condemned as `stale` though the pid in it
#:   was the caller's own, and the caller was demonstrably alive. That is the unreliability the comment
#:   at this place used to apologise for, and it was the symptom and not the cause.
#:
#: So Windows is asked through `kernel32` instead. The answer is taken in one direction only: `False`
#: is returned where the process is known to have terminated, and `None` where nothing can be told --
#: and `None` is deliberately not `False`, because a probe that guesses in the direction of stale is
#: precisely the failure this function exists to defeat. See `SERVER_DESIGN.md` §"pid, blender,
#: started_at" for why a dead bridge's record has to be recognised, and `connect_to_believe` for why a
#: doubtful one must still be attempted.
_STILL_ACTIVE = 259                            # STILL_ACTIVE, in the process's own account of itself
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000    # the least rights that can still tell us anything


def _pid_is_running(pid: int) -> bool | None:
    """True, False, or `None` where nothing can be told. Never guess in the direction of stale."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)                 # there alone, 0 does mean "exist, and do not signal"
        except OSError:
            return False
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32               # `windll`, and not `winll`, which is not
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None                     # not openable is not the same as not running
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None                 # no account of it, so no verdict from it
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)    # a handle left open is a handle nobody can close
    except (ImportError, AttributeError, OSError, ValueError):
        return None                         # a probe that cannot be run is a probe that tells nothing


def read_runtime(path: str = RUNTIME_FILE) -> dict | None:
    """The bridge's live endpoint record, or None when there is none worth reporting.

    Stale records are the failure mode this exists to defeat: a `bridge.json` left behind by a dead
    Blender is a file that looks exactly like a running one. So the pid is verified, and a caller
    still has to connect to believe.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    except (ValueError, OSError) as exc:
        raise BridgeGone("runtime-file", f"{path} is not readable as a runtime record: {exc}") from exc
    if not isinstance(rec, dict):
        raise BridgeGone("runtime-file", f"{path} does not hold an object")
    pid = rec.get("pid")
    if isinstance(pid, int) and pid > 0 and _pid_is_running(pid) is False:
        # Only a termination that has actually been seen condemns a record. A probe that cannot tell
        # returns None and is let through, because the two errors here cost differently: connecting to
        # a port where nothing listens is refused in a third of a second and says so plainly, whereas
        # refusing a bridge that is up and running leaves the operator with a panel that shows PickIK
        # live and a tool insisting that there is nothing there. This is a hint, and
        # connect_to_believe remains the verdict.
        raise BridgeGone("stale", f"{path} names pid {pid}, which has terminated") from None
    return rec


class Bridge:
    """One authenticated session with the bridge. Deliberately *not* auto-reconnecting.

    The bridge admits a single client and fail-safes on disconnect; a client that silently reconnects
    could let an agent resume a session that has already been failed-safe'd, which is the opposite of
    what an operator pressed Stop for. So a lost link raises, and starting a new one is a decision.
    """

    def __init__(self, host: str, port: int, token: str = "", *,
                 timeout_ms: int = 5000, proto_rev: str | None = None) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            # The bridge binds loopback only; a client pointed anywhere else is a misconfiguration or
            # an attempt to reach a remote Blender through a tool whose whole safety case is local.
            raise BridgeGone("endpoint", f"refusing to talk to a non-loopback host: {host!r}")
        self.host, self.port, self.token = host, int(port), token
        self.timeout_ms = int(timeout_ms)
        self.proto_rev = proto_rev or P.proto_rev()
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 0
        self.connected = False

    # -- lifecycle ---------------------------------------------------------------------------
    @classmethod
    def from_runtime(cls, path: str = RUNTIME_FILE, **kw) -> "Bridge | None":
        """Connect to wherever the bridge published itself, or return None if it published nothing."""
        rec = read_runtime(path)
        if not rec:
            return None
        return cls(str(rec.get("host", "127.0.0.1")), int(rec.get("port", 0)),
                   str(rec.get("token", "")), **kw)

    def __enter__(self) -> "Bridge":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def connect(self) -> dict:
        """Open, handshake, and believe only what the hello says. Raises BridgeGone."""
        if self.connected:
            raise BridgeGone("state", "already connected; the bridge admits one session")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(CONNECT_TIMEOUT_S)
            try:
                sock.connect((self.host, self.port))
            except OSError as exc:
                _bye(sock)
                raise BridgeGone("unreachable", f"{self.host}:{self.port} refused ({exc})") from exc
            sock.settimeout(self.timeout_ms / 1000.0 + GRACE_MS / 1000.0)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass                          # a delay we could not remove is not a reason to fail
            self._sock, self._buf = sock, b""
            #: The greeting comes from the authority, and is not assembled here. The dialect puts it
            #: under "hello", with the secret under "auth" and the revision beside it
            #: (mcp_protocol.hello, and MCP_INTEGRATION_PLAN.md §the wire, which shows the arrow going
            #: in as a hello frame and not as a command envelope); the bridge validates precisely that
            #: shape and answers anything else with E_PROTO "expected a hello frame". A hello is not a
            #: command: the two frames are not interchangeable however the fields are arranged, and this
            #: site had been sending the command envelope with cmd="hello", which no bridge ever
            #: accepted -- the first real round trip in the project's history is what found it.
            self._send(P.hello(proto_rev=self.proto_rev, token=self.token, client=CLIENT_NAME))
            hello = self._recv_frame()
            #: Believed, which is what the line above promises and this is the half that was missing.
            #: A greeting is what the bridge admits with; anything else is a refusal carrying a code --
            #: a secret it does not recognise, a revision it does not speak -- and what came back was
            #: until now carried straight down to `self.connected = True` without once being looked at.
            #: A refused handshake is not a connection: every command that followed was written down a
            #: link the bridge had already turned away, and the caller told that all was well. Raised
            #: inside the `try` above, so that the handler there closes the socket and the bridge is
            #: left as it was found. The kind is the bridge's own code with its prefix taken off, which
            #: is how `why` comes to read `acces: ...` and an operator can tell a wrong secret from a
            #: wrong revision without trusting to a second hand; the secret itself is named by neither,
            #: which is a check in the suite and not merely a courtesy.
            if not isinstance(hello.get("hello"), dict):
                error = hello.get("error") or {}
                code = str(error.get("code") or "unknown")
                reason = str(error.get("message") or "the bridge answered with neither a greeting nor a reason")
                kind = code.lower()
                if kind.startswith("e_"):
                    kind = kind[2:]
                raise BridgeGone(kind or "handshake",
                               f"the bridge refused the session: {code} {reason}") from None
        except BaseException:
            self.close()
            raise
        self.connected = True
        return hello

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self.connected = False
        if sock is not None:
            _bye(sock)

    # -- the wire ----------------------------------------------------------------------------
    def _send(self, obj: dict) -> None:
        sock = self._sock
        if sock is None:
            raise BridgeGone("state", "not connected")
        try:
            sock.sendall(P.encode(obj))
        except OSError as exc:
            raise BridgeGone("reset", f"the link died while writing ({exc})") from exc

    def _recv_frame(self) -> dict:
        """Next complete line, or raise. Never assumes one datagram per request."""
        sock = self._sock
        if sock is None:
            raise BridgeGone("state", "not connected")
        start = time.perf_counter()
        while True:
            line, sep, rest = self._buf.partition(b"\n")
            if sep:                                       # a newline present means a whole frame is here
                self._buf = rest
                return self._decode(line)
            if (time.perf_counter() - start) * 1000.0 > (self.timeout_ms + GRACE_MS):
                raise BridgeGone("timeout", "no complete frame before the deadline")
            try:
                chunk = sock.recv(65_536)
            except TimeoutError as exc:
                raise BridgeGone("timeout", "the bridge is silent past its deadline") from exc
            except OSError as exc:
                if exc.errno in (errno.EINTR,):
                    continue
                raise BridgeGone("reset", f"the link died while reading ({exc})") from exc
            if not chunk:                                   # orderly EOF: the other side is gone
                raise BridgeGone("closed", "the bridge closed the connection")
            self._buf += chunk
            if len(self._buf) > MAX_FRAME_BYTES:
                raise BridgeGone("overflow", f"a frame exceeded {MAX_FRAME_BYTES} bytes")

    @staticmethod
    def _decode(line: bytes) -> dict:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BridgeGone("protocol", f"a frame was not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise BridgeGone("protocol", "a frame was not a JSON object")
        return obj

    def _request(self, cmd: str, args: dict) -> dict:
        self._next_id += 1
        frame = {"id": f"{os.getpid()}-{self._next_id}", "cmd": cmd, "args": dict(args)}
        self._send(frame)
        while True:                                   # replies are matched by id; others are skipped
            obj = self._recv_frame()
            if obj.get("id") in (frame["id"], None):
                return obj
            # A frame for another id can only be a stray from a request we abandoned; ignore, do not
            # interpret, and certainly never let it be mistaken for this request's answer.

    # -- the two verbs the server is allowed to use ------------------------------------------
    def call(self, cmd: str, args: dict, deadline_ms: int | None = None) -> dict:
        """Send one command, return the reply object unchanged.

        A refusal (`ok: False`, `E_*`) is a *result*: it comes back as data with its code and its
        message intact, because an agent that cannot tell `E_ACCES` (a gate was not satisfied) from
        `E_INVAL` (the arguments were wrong) will retry the wrong thing. Only a broken link raises.
        """
        payload = dict(args)
        if deadline_ms is not None:
            payload.setdefault("deadline_ms", int(deadline_ms))
        return self._request(cmd, payload)

    def ping(self) -> dict | None:
        """Whether a bridge is there and answers, without ever raising."""
        try:
            with self:
                return {"ok": True}
        except BridgeGone as exc:
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}


def describe_endpoint(path: str = RUNTIME_FILE) -> dict:
    """What an operator would want to know when the tools say `no bridge`: where to look, what was
    found there, and what to do about it. Plainly worded, because a human reads this one."""
    try:
        rec = read_runtime(path)
    except BridgeGone as exc:
        return {"running": False, "runtime_file": path, "found": False,
                "why": f"the runtime record is unreadable: {exc.detail}",
                "how_to_start": "in Blender: press N for the 3D-view sidebar, open the PickIK category, "
                                   "panel 'PickIK arm7', tick 'MCP bridge' (the property that permits a start at all), "
                                   "then press Start. The server cannot start it for you"}
    if rec is None:
        return {"running": False, "runtime_file": path, "found": False,
                "why": "no bridge is publishing an endpoint",
                "how_to_start": "in Blender: press N for the 3D-view sidebar, open the PickIK category, "
                                   "panel 'PickIK arm7', tick 'MCP bridge' (the property that permits a start at all), "
                                   "then press Start. The server cannot start it for you"}
    probe = Bridge(str(rec.get("host", "127.0.0.1")), int(rec.get("port", 0)),
                   str(rec.get("token", ""))).ping()
    out = {"running": bool(probe.get("ok")), "runtime_file": path, "found": True,
           "host": rec.get("host"), "port": rec.get("port"), "pid": rec.get("pid"),
           "proto_rev": rec.get("proto_rev"), "started_at": rec.get("started_at")}
    if not out["running"]:
        out["why"] = f"a record exists but nothing answered: {probe.get('kind')} {probe.get('detail')}"
        out["how_to_start"] = ("press Start in the PickIK panel; if a stale record is left behind, "
                              "delete " + path)
    return out
