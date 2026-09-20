"""Watch the console for Ctrl+C events that nobody in the room can have sent.

    python watch_console_probe.py                # counted in the window you are looking at
    python watch_console_probe.py --new-console   # counted in a console nobody is watching

Nothing here is under test, and nothing here opens a socket or a thread beyond the one this file needs.
It does one thing: install a handler that counts, says nothing, and does not raise, then wait. A SIGINT
that is heard here is a SIGINT that arrived at the process from outside any code in this repository, and
the only variable between the two invocations is whether a console was attached that a person was
looking at. Hearings in the first and not in the second are therefore hearings of that window -- its
QuickEdit, its scroll, its selection, or whatever else is attached to it -- and no change to the
package can be expected to still them.

Why the handler does not raise: a handler that raises turns an observation into an interruption, and
the observation is the whole reason for the file. The count, not the exception, is the datum.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

SECONDS_DEFAULT = 30
HEARTBEAT_S = 1.0
CREATE_NEW_CONSOLE = 0x00000010
#: One token, shared by the record that is written and the client that reads it, so that a disagreement
#: between the two can only be the one that matters. It is not a secret and is not echoed.
_PROBE_TOKEN = "sekret-tok-for-the-probe-only"


def _relaunch_in_a_console_of_its_own(arguments: list) -> int:
    """Start this same probe before a fresh console, wait for it, and bring back what it heard.

    The child writes to a file and not to the terminal it is now the owner of, because that terminal is
    a window nobody is looking at -- which is precisely the condition being established, and also the
    reason the record has to be carried back by hand.
    """
    record = os.path.join(os.getenv("TEMP", os.path.expanduser("~")), "pickik-console-watch.log")
    if os.path.isfile(record):
        os.remove(record)
    print(f"starting the probe before a console of its own; the record will be kept at\n  {record}",
          flush=True)
    child = [sys.executable, os.path.realpath(__file__), "--seconds", str(_seconds(arguments))]
    child += [one for one in ("--with-imports", "--with-round-trip", "--with-runtime-record")
              if one in arguments]
    # The child is given where to keep its record, and by the same name the parent means to read it
    # back out of: an arm that leaves no record proves nothing, however quiet its silence looks, and
    # one arm of this ladder did exactly that before it was mended.
    env = dict(os.environ)
    env["PICKIK_CONSOLE_WATCH_LOG"] = record
    try:
        done = subprocess.run(child, creationflags=CREATE_NEW_CONSOLE, env=env,
                             timeout=_seconds(arguments) + 30)
        print(f"the probe ended, exit {done.returncode}", flush=True)
    except BaseException as exc:
        print(f"!! the probe could not be started before its own console: {type(exc).__name__}: {exc}",
              flush=True)
        return 2
    if os.path.isfile(record):
        sys.stdout.write(open(record, encoding="utf-8", errors="replace").read())
        sys.stdout.flush()
    else:
        print("!! the probe left no record behind; nothing can be concluded from its silence", flush=True)
        return 2
    return 0


def _seconds(arguments: list) -> int:
    for i, one in enumerate(arguments):
        if one == "--seconds" and i + 1 < len(arguments):
            try:
                return max(5, min(600, int(arguments[i + 1])))
            except ValueError:
                pass
    return SECONDS_DEFAULT


def _bring_in_the_package(*_ignored) -> str:
    """Import all that the suite imports, and nothing besides, so that an SDK can be had on its own.

    The weight of it is the point: `mcp` brings `anyio` and `pydantic` in with it, and a library that
    arms itself against the console on import would be heard from here, whether or not a socket is ever
    opened. Silence here therefore says nothing about the sockets, and is a control and not a proof.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    pkg = os.path.normpath(os.path.join(here, os.path.pardir))
    repo = os.path.normpath(os.path.join(here, os.path.pardir, os.path.pardir))
    for one in (repo, pkg, os.path.join(pkg, "vendored")):
        if one not in sys.path:
            sys.path.append(one)
    try:
        import mcp                                                    # noqa: F401  the SDK
        import mcp_client                                             # noqa: F401
        import mcp_server                                             # noqa: F401
        return f"the package {getattr(mcp_server, 'SERVER_VERSION', '?')} and its SDK were brought in"
    except BaseException as exc:
        return f"the package could not be brought in: {type(exc).__name__}: {exc}"


def _one_live_round_trip(via_record: bool = False) -> str:
    """Make one genuine exchange over the wire, through the real client, before any watch begins.

    The read that follows is the very one every hearing has been taken at: the little bridge here answers
    the hello and then holds its tongue, so that the client is left waiting upon a socket for a command
    that is never coming. No suite, no timers, no watchdog, no thresholds -- which leaves, if a signal
    is heard during the wait, the wait is what invited it.
    """
    brought = _bring_in_the_package()
    if "could not" in brought:
        return brought
    try:
        import mcp_client as C
        import mcp_protocol as P
    except BaseException as exc:
        return f"the client could not be had: {type(exc).__name__}: {exc}"

    heard: list = []

    def serve(listener) -> None:
        try:
            while True:
                try:
                    conn, _addr = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                try:
                    raw = b""
                    while not raw.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                    if not raw:
                        return
                    try:
                        frame = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        return
                    heard.append(str(frame.get("cmd")))
                    if frame.get("cmd") == "hello":
                        conn.sendall(P.encode({"ok": True, "id": frame.get("id"), "cmd": "hello",
                                              "data": {"protocol": P.PROTOCOL,
                                                      "proto_rev": P.proto_rev(),
                                                      "server": "the probe"}}))
                    time.sleep(30.0)                       # stay quiet, and keep the client waiting
                except BaseException:
                    return
        except BaseException:
            return

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    threading.Thread(target=serve, args=(listener,), daemon=True, name="the-probe-bridge").start()
    time.sleep(0.1)
    outcome = "not attempted"
    try:
        if via_record:
            #: The record carries this process's own pid, exactly as the suite's fake bridge publishes it,
            #: and that is what makes the liveness probe inside `read_runtime` aim `os.kill(pid, 0)` at
            #: this very process. On Windows the second argument is not the signal zero of the manuals but
            #: `CTRL_C_EVENT`, so the call either fails -- and the record is then taken for stale though
            #: the pid lives, which is the unreliability the code's own comment apologises for -- or it is
            #: accepted, and a Ctrl+C goes out to every process attached to this console, this one among
            #: them. `from_runtime` is the door the server itself goes through, so nothing is staged here
            #: that the suite does not also do.
            directory = os.getenv("TEMP") or os.path.expanduser("~")
            record = os.path.join(directory, "pickik-probe-bridge.json")
            with open(record, "w", encoding="utf-8") as handle:
                json.dump({"host": "127.0.0.1", "port": port, "token": _PROBE_TOKEN,
                          "proto_rev": P.proto_rev(), "pid": os.getpid(),
                          "request_timeout_ms": 900,
                          "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, handle)
            bridge = C.Bridge.from_runtime(record, timeout_ms=900)
            if bridge is None:
                return "from_runtime found no record at the path it was given; nothing was connected to"
        else:
            bridge = C.Bridge("127.0.0.1", port, _PROBE_TOKEN,
                            timeout_ms=900, proto_rev=P.proto_rev())
        bridge.connect()
        try:
            bridge.call("get_state", {})                   # the blocking read the hearings are taken at
            outcome = "answered"
        except BaseException as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        bridge.close()
    except BaseException as exc:
        outcome = f"the round trip could not be made: {type(exc).__name__}: {exc}"
    finally:
        try:
            listener.close()
        except OSError:
            pass
    return (f"the hello was {heard or 'never heard'}; the command came back {outcome}, after the wait"
            + (" [by way of a published record carrying this process's own pid]" if via_record else ""))


def _heard(signum, frame) -> None:
    """Count the hearing, note where it was taken, and above all, do not raise."""
    _count[0] += 1
    where = "<no frame was given>"
    try:
        lines = [one.strip() for one in "".join(traceback.format_stack(frame)).splitlines()
                 if one.strip().startswith("File ")]
        where = lines[-1] if lines else "<no frame was executing>"
    except BaseException as exc:                          # a record that raises is no record at all
        where = f"<the hearing could not be taken: {type(exc).__name__}>"
    line = (f"{time.strftime('%H:%M:%S')} heard signal {signum}, number {_count[0]}, "
            f"while {where}\n")
    print(line, end="", flush=True)
    if _record[0] is not None:
        try:
            _record[0].write(line)
            _record[0].flush()
        except BaseException:
            pass


_count: list = [0]
_record: list = [None]


def _say(line: str) -> None:
    """Say whatever is worth saying where it can be read: the terminal, and the record if one is kept.

    A finding that is only ever spoken is a finding that cannot be carried back out of a console nobody
    is watching, which is the standing condition of the detached arm -- where, as was found, the report
    of the round trip went to a window no one would ever open, and the conclusion alone came home.
    """
    print(line, flush=True)
    if _record[0] is not None:
        try:
            _record[0].write(line + "\n")
            _record[0].flush()
        except BaseException:
            pass


def main(arguments: list) -> int:
    if "--new-console" in arguments:
        return _relaunch_in_a_console_of_its_own(arguments)

    log = os.getenv("PICKIK_CONSOLE_WATCH_LOG")
    if log:
        try:
            _record[0] = open(log, "w", encoding="utf-8")
            #: The header and the conclusion go into the record as well as to the terminal, because a file
            #: that is empty is a file that can be read two ways -- nobody heard anything, or nothing was
            #: ever wired to hear -- and the one is a result while the other is a broken arm. This line is
            #: what tells the two apart, and it is written first so that a truncated file still says so.
            _record[0].write(f"{time.strftime('%H:%M:%S')} watching for SIGINT, for "
                             f"{_seconds(arguments)} s, hands off the keyboard\n")
            _record[0].flush()
        except OSError as exc:
            print(f"!! no record can be kept at {log}: {exc}", flush=True)

    named = "a console of its own, which nobody is watching" if log else "this window"
    seconds = _seconds(arguments)
    print(f"watching for SIGINT in {named} for {seconds} s; keep your hands off the keyboard",
          flush=True)
    prior = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, _heard)
    except BaseException as exc:
        print(f"!! SIGINT could not be watched: {type(exc).__name__}: {exc}", flush=True)
        return 2
    #: The arms are run with the ear already open, so that a signal arriving during an import or during
    #: the wait itself is counted and not merely survived.
    if "--with-runtime-record" in arguments:
        _say("   " + _one_live_round_trip(via_record=True))
        _say(f"   {_count[0]} heard so far, this side of the wait")
    elif "--with-round-trip" in arguments:
        _say("   " + _one_live_round_trip())
        _say(f"   {_count[0]} heard so far, this side of the wait")
    elif "--with-imports" in arguments:
        _say("   " + _bring_in_the_package())
    took_one = False
    try:
        end = time.monotonic() + seconds
        last = 0.0
        while time.monotonic() < end:                       # a sleep is where the look for a signal is taken
            time.sleep(HEARTBEAT_S)
            if time.monotonic() - last >= 5.0:
                last = time.monotonic()
                left = max(0, int(end - time.monotonic()))
                print(f"   ... {left} s left, {_count[0]} heard so far", flush=True)
    except KeyboardInterrupt:
        took_one = True
        print("   (this window took a KeyboardInterrupt that the handler did not raise)", flush=True)
    finally:
        try:
            signal.signal(signal.SIGINT, prior)             # leave the machine as it was found
        except BaseException:
            pass
    verdict = (f"conclusion: {_count[0]} signal(s) heard in {seconds} s - "
               + ("a person, or something, is sending them" if _count[0] else "the console was quiet"))
    if took_one:
        verdict += ", and the window itself took one KeyboardInterrupt"
    print(verdict, flush=True)
    if _record[0] is not None:                              # the same words, in the file, for the reader
        try:
            _record[0].write(verdict + "\n")
            _record[0].flush()
            _record[0].close()
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
