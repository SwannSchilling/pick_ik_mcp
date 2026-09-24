"""How many of the servers are running, which of them are stale, and how to take the stale ones away.

The question this module exists to answer is the one an operator asks at eleven at night: there are
servers about, how many are they, and how do I kill them. Answering it needs neither Blender nor the
SDK, so it lives out of the server proper and beside the checks, and it can be run before Blender is
up, which is the hour when the answer is most wanted.

Three questions, and they are not one question:

  ORPHAN     -- has the client that spawned it gone away?  A server whose parent lives is that parent's
                to reap: close the window, its stdin closes, and it exits of its own accord. To kill
                one from under a live parent may interrupt an agent in the middle of a tool call whose
                effect has already reached the arm, so a live parent keeps our hand off it.
  STALE      -- was it born before the source it is running was last written?  Then it is a fossil
                executing a binary that has since been replaced, and that is the thing which has fooled
                more than one diagnosis in this project.
  SQUATTING  -- does it hold an established socket upon the port the bridge listens on?  The bridge
                admits one client, so a squatter is not merely untidy: it is the reason the next caller
                is refused.

Only ORPHAN together with STALE is killed without being asked for, and the dry run is the default, for
the consequence of getting this wrong is somebody's live session. Nothing here speaks to the bridge:
it is asked of through the record the bridge publishes, which is read off the disk and costs no seat.

Nothing is imported that is not of the standard library, and nothing here is to be allowed to change
that: this is the one part of the package which has to work when the rest of it is broken.
"""
import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import time

SCRIPT = os.path.abspath(__file__)
HERE = os.path.dirname(SCRIPT)
#: The columns asked of the process table and of the socket table. Read off the objects with
#: Get-Member, and not remembered: a column that is not upon the object comes back as an empty field,
#: which is worse than an error, for an error is heard and an empty field is believed.
PCOLS = ("ProcessId", "ParentProcessId", "Name", "CommandLine", "CreationDate")
SCOLS = ("State", "LocalAddress", "LocalPort", "RemoteAddress", "RemotePort", "OwningProcess")
HEAD = ("$ErrorActionPreference = 'SilentlyContinue'; $ProgressPreference = 'SilentlyContinue'; ")


class CannotQuery(RuntimeError):
    """The instrument failed to answer, which is not the selfsame thing as the answer being none. That
    no servers are running is a finding, and that nothing was looked at is a broken thermometer, and a
    caller which confounds the two will tell the operator to restart Blender while Blender is up."""


def part(*pieces):
    """A name whose parts carry an underscore between them, put together at run time. The underscore is
    a piece of its own, so that a channel which deletes such characters may have nothing to delete."""
    return "".join(pieces)


ASK_KW = lambda: {part("capture", "_", "output"): True}               # noqa: E731  the keyword, fetched at run
def ask(cmd, *, timeout=45):
    """Ask the PowerShell, and read the answer as delimited text."""
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    try:
        done = subprocess.run(argv, **dict(ASK_KW(), timeout=timeout))
    except (OSError, subprocess.SubprocessError) as exc:
        raise CannotQuery("could not ask: %s: %s" % (type(exc).__name__, exc)) from exc
    out = getattr(done, "stdout", b"") or b""
    rc = getattr(done, "returncode", 0)
    if rc not in (0, None) and not out.strip():
        err = (getattr(done, "stderr", b"") or b"").decode("utf-8", errors="replace")[:220]
        raise CannotQuery("the query failed, exit %s: %s" % (rc, err))
    text = decode(out)
    body = [row for row in text.splitlines() if row.strip() and not row.startswith("#")]
    if not body:
        return []
    try:
        return list(csv.DictReader(io.StringIO("\n".join(body)), delimiter="|"))
    except csv.Error as exc:
        raise CannotQuery("the answer is not delimited text: %s" % exc) from exc


def decode(raw):
    """Read the encoding off the position of the NULs, and do not guess at it: text in UTF-16 little
    endian puts every NUL upon an odd offset and nothing between them, which is a fact about the bytes
    and not a hope about the sender."""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" not in raw:
        return raw.decode("utf-8", errors="replace")
    half = max(1, len(raw) // 2)
    odd = sum(1 for i in range(1, len(raw), 2) if raw[i:i + 1] == b"\x00")
    return raw.decode("utf-16-le" if odd >= half * 0.9 else "utf-8", errors="replace")


def select(*columns):
    """Take the columns and hand them over delimited by a character which cannot turn up inside a
    command line."""
    return " | Select-Object -Property " + ",".join(columns) + " | ConvertTo-Csv -NoTypeInformation -Delimiter '|'"


def ages(path):
    """The age member of a stat, fetched by name and not written out, for the reason given at the head."""
    try:
        return getattr(os.stat(path), part("st", "_", "mtime"))
    except (OSError, ValueError):
        return None


def youngest():
    """The youngest of the sources a server may be running, with its name. Weighed as an epoch and
    compared against a birth which is an epoch, for a local age against a UTC birth is off by the
    offset, reads as innocent, and condemns a fresh build as a fossil."""
    top, name = None, ""
    for folder in (HERE, os.path.join(HERE, "vendored")):
        try:
            leaves = os.listdir(folder)
        except OSError:
            continue
        for leaf in sorted(leaves):
            if not leaf.endswith(".py") or leaf.startswith("test"):
                continue
            stamp = ages(os.path.join(folder, leaf))
            if stamp is not None and (top is None or stamp > top):
                top, name = stamp, leaf
    return top, name


def when(who):
    """The birth of a process, in seconds since the epoch, or None when it would be a guess. A CIM
    datetime is of the form 20260921201512.123456+120 and is of the local timezone, which the trailing
    offset tells. The digits are taken by their groups and given to mktime, which is of the timezone
    we want, and no method of a datetime is called upon a value which may be a string."""
    text = str(who or "").strip()[:24]
    found = re.match(r"^(\d{4})[-/ ]?(\d{2})[-/ ]?(\d{2})[ T-]?(\d{2})[:]?(\d{2})[:]?(\d{2})", text)
    if not found:
        return None
    groups = [int(found.group(n)) for n in range(1, 7)]
    try:
        return time.mktime(tuple(groups) + (0, 0, -1))
    except (OSError, OverflowError, ValueError, TypeValueError):
        return None


def show(seconds):
    """A birth, for a human being to read, in UTC."""
    try:
        return datetime.datetime.utcfromtimestamp(float(seconds)).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError, TypeValueError):
        return "?"


def record(record_file=""):
    """What the bridge last published, with the secret popped before it is spoken, as ever a secret
    payload is popped. A diagnostic that prints a record with its token in it has leaked the token into
    the log, and no virtue in the rest of the line makes up for that."""
    try:
        from mcp_client import read_runtime                           # late, for the sake of the cycle
        rec = read_runtime(record_file) if record_file else read_runtime()
    except Exception as exc:                                           # noqa: BLE001  a record is a hint
        return {"found": False, "why": "%s: %s" % (type(exc).__name__, exc)}
    if not isinstance(rec, dict):
        return {"found": False, "why": "no record was there to read"}
    rec.pop("token", None)                                             # the rule of every secret payload
    return {"found": True, "host": rec.get("host"), "port": rec.get("port"), "pid": rec.get("pid"),
            "proto_rev": rec.get("proto_rev"), "blender": rec.get("blender"),
            "pumped_at": rec.get("pumped_at"), "carries_pumped_at": "pumped_at" in rec}


def processes():
    """The process table, in the one query. Queried once and not twice, for it is wanted twice over --
    to pick the servers out of, and to tell which parents are yet there -- and a second query would see
    a table changed between the two, which is how a process comes to be called an orphan while its
    parent sits there still."""
    table = {}
    for row in ask(HEAD + "Get-CimInstance -ClassName Win32_Process" + select(*PCOLS)):
        pid = str(row.get("ProcessId") or "").strip()
        if pid.isdigit():
            table[int(pid)] = row
    return table


def sockets(port):
    """The processes holding an established socket upon the port the bridge listens on. Both halves are
    asked of, for one connection is of two rows: a filter upon the listening half alone has been seen
    to report none while a client was sat at the bridge, and it is the client half which names the
    server."""
    if not port:
        return set()
    rows = ask(HEAD + "Get-NetTCPConnection | Where-Object { $_.LocalPort -eq " + str(int(port))
               + " -or $_.RemotePort -eq " + str(int(port)) + " }" + select(*SCOLS))
    hold = set()
    for row in rows:
        if str(row.get("State") or "").strip().lower() != "established":
            continue
        who = str(row.get("OwningProcess") or "").strip()
        if who.isdigit():
            hold.add(int(who))
    return hold


def mypids(table):
    """The pids of this query and of the parents above it, for the query must not eat itself: a shell
    which runs the command, a node which runs the shell and a harness which runs the node do all carry
    the text they were bidden to run in their command lines, and so they match the pattern while being
    nothing of the servers. They are disclosed, not silently dropped, for a count which hides its
    shadows is a count which cannot be reckoned with."""
    chain, pid = set(), os.getpid()
    for _ in range(24):
        if pid not in table:
            break
        chain.add(pid)
        parent = str(table[pid].get("ParentProcessId") or "").strip()
        if not parent.isdigit():
            break
        pid = int(parent)
    return chain


def verdict(who, row, table, top, topname, mine, now, every):
    """The verdict upon one row, formed in the one place it is formed, so that a check may hand it rows
    out of a table and see the whole without one process being enumerated. No operating system is
    called in here, which is why it is out of the query, and why it may be trusted when the query may
    not."""
    pid, ppid = int(who), 0
    parent = str(row.get("ParentProcessId") or "").strip()
    if parent.isdigit():
        ppid = int(parent)
    name = str(row.get("Name") or "")
    command = str(row.get("CommandLine") or "")
    where = when(row.get("CreationDate"))
    low = command.lower().replace("\\", "/")
    tags = []
    if pid in mine:
        tags.append("SELF")
    if ppid not in table:
        tags.append("ORPHAN")
    if where is None:
        tags.append("NO-BIRTH")
    elif top is None:
        tags.append("NO-SOURCE")                                        # the scale was broken, not the thing
    elif where < top:
        tags.append("STALE")
    if not re.match(r"(?i)^python", name):
        tags.append("NOT-PYTHON")                                       # disclosed, and not counted
    if "blender" in low:
        tags.append("NAMES-BLENDER")                                    # not ours to kill, by any route
    if "mcp_server.py" not in low:
        tags.append("NOT-OURS")
    elif not every and HERE.lower().replace("\\", "/") not in low:
        tags.append("NOT-OF-HERE")
    age = None if where is None else round((now - where) / 3600.0, 1)
    kin = "gone"
    if ppid in table:
        kin = str(table[ppid].get("Name") or "?")
    #: A negative tag is a bar to killing, however stale and however orphaned: the flag may not call
    #: something "ours to kill" and then a reason, in the same breath, call it not ours to kill. The two
    #: must agree, for a flag which disagrees with its own reason is a lying flag and a lying flag is the
    #: instrument fault this whole work is set against.
    blocked = {"SELF", "NOT-PYTHON", "NOT-OURS", "NOT-OF-HERE", "NAMES-BLENDER", "NO-BIRTH", "NO-SOURCE"}
    killable = "ORPHAN" in tags and "STALE" in tags and not (blocked & set(tags))
    return {"pid": pid, "ppid": ppid, "name": name, "command": command, "parent": kin,
            "birth": None if where is None else show(where), "age_h": age, "tags": tags,
            "killable": killable, "why_not": why_not(tags, where, top), "youngest": topname}


def why_not(tags, where, top):
    """Why our hand stays off this, in words, for the operator who is deciding and not for the machine
    which is only counting."""
    if "SELF" in tags:
        return "it is this very query, or the shell which ran it"
    if "NAMES-BLENDER" in tags:
        return "it names Blender, which is not ours to kill by any route"
    if "NOT-PYTHON" in tags:
        return "it is not a python runner: its name is but a shadow cast by this query"
    if "NOT-OURS" in tags:
        return "it names our script somewhere about, but it is not our server"
    if "NOT-OF-HERE" in tags:
        return "it is a server of another checkout, and --every was not said"
    if "NO-BIRTH" in tags:
        return "its birth was not told, so it cannot be proved a fossil"
    if "NO-SOURCE" in tags:
        return "no source could be weighed, so the age of it cannot be judged"
    if "STALE" not in tags:
        return "it is not older than the youngest of the sources: it runs the code as written"
    if "ORPHAN" not in tags:
        return "its parent is yet there: close that window and it exits of its own accord"
    return ""


def servers(table=None, *, every=False, now=None):
    """Every process of the table whose command line names our server script, with its verdict. Matched
    by finger print and not by name alone: a process called python.exe proves nothing, while a command
    line naming this checkout proves much. `every` widens the match to any checkout, for a second
    working copy may be serving as well."""
    table = processes() if table is None else table
    now = time.time() if now is None else float(now)
    top, topname = youngest()
    mine = mypids(table)
    needle = os.path.basename(SCRIPT).lower()
    found = []
    for who in sorted(table):
        command = str(table[who].get("CommandLine") or "")
        if not command or needle not in command.lower():
            continue
        found.append(verdict(who, table[who], table, top, topname, mine, now, every))
    return found


def kill_list(found, *, older=None, squatters=(), even=False):
    """Which of these may be taken away, which may not, and the reason for every one. ORPHAN with
    STALE, that is the rule. A live parent, a fresh binary, a name that is not ours, or a name that
    names Blender keeps the hand off. `even` is the one widening offered, for a squatter upon the
    bridge's one seat is a several offence and is removed only when it is asked for in terms."""
    squat = {int(one) for one in (squatters or ()) if str(one).lstrip("-").isdigit()}
    go, spare = [], []
    for row in found:
        tags = set(row.get("tags") or ())
        may = row.get("killable")
        if may is not False and may is not True:
            may = {"ORPHAN", "STALE"} <= tags and not {
                "SELF", "NOT-PYTHON", "NOT-OURS", "NOT-OF-HERE", "NAMES-BLENDER", "NO-BIRTH",
                "NO-SOURCE"} & tags
            row["killable"] = may
        if not may:
            spare.append((row, row.get("why_not") or "it is neither orphaned nor stale"))
            continue
        if row["pid"] in squat and not even:
            spare.append((row, "it is orphaned and stale, but it squats upon the bridge's one seat: say "
                              "--even-squatting to have it removed as well"))
            continue
        if older is not None and (row.get("age_h") or 0.0) < float(older):
            spare.append((row, "it is younger than the bound of %s h that was set" % older))
            continue
        go.append(row)
    return go, spare


def do_kill(rows, *, dry=True):
    """Take them away, by the one route which is safe upon this platform.

    Not os.kill, which does not terminate a process upon Windows at all but signals the console group,
    the family of Ctrl+C: a kill of that kind, written in a tool of this kind, has been known to carry
    off the very Blender it was meant to query. The call which terminates one process and no other is
    the taskkill with the force flag; the finger print is taken again at the instant of the strike, for
    a pid is reused and a number read a minute ago may name a different process by the minute it is
    struck.
    """
    told = []
    for row in (rows if isinstance(rows, (list, tuple)) else [rows]):
        pid = int(row["pid"])
        command = str(row.get("command") or "")
        if "mcp_server.py" not in command.lower().replace("\\", "/") or "blender" in command.lower():
            told.append({"pid": pid, "did": False, "said": "refused upon the finger print"})
            continue
        if dry:
            told.append({"pid": pid, "did": False, "said": "a dry run: nothing was killed"})
            continue
        again = {one["pid"]: one for one in servers()}
        fresh = again.get(pid)
        if fresh is None:
            told.append({"pid": pid, "did": False, "said": "it is gone already"})
            continue
        if "mcp_server.py" not in str(fresh.get("command") or "").lower():
            told.append({"pid": pid, "did": False, "said": "refused: the pid now names another process"})
            continue
        try:
            done = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                 **dict(ASK_KW(), timeout=20))
            said = (getattr(done, "stdout", b"") or getattr(done, "stderr", b"") or b"")
            rc = getattr(done, "returncode", 1)
            told.append({"pid": pid, "did": rc == 0, "said": said.decode("utf-8", errors="replace").strip()
                                 or "it answered with exit status %s" % rc})
        except (OSError, subprocess.SubprocessError) as exc:
            told.append({"pid": pid, "did": False, "said": "%s: %s" % (type(exc).__name__, exc)})
    return told


def fmt(found, *, rec, port, squatters):
    """The report, for a human being to read. The count is in the first of the lines, for that is the
    question which was asked."""
    counted = [row for row in found
               if not {"SELF", "NOT-PYTHON", "NOT-OURS", "NOT-OF-HERE"} & set(row.get("tags") or ())]
    shadows = [row for row in found if row not in counted]
    out = ["== the servers of " + os.path.basename(HERE) + " =="]
    for row in sorted(counted, key=lambda one: -(one.get("age_h") if one.get("age_h") is not None else -1)):
        here = "  SQUATTING" if row["pid"] in set(squatters or ()) else ""
        out.append("  pid {:<7} ppid {:<7} {:>8} {:20} {:<46} {}{}".format(
            row["pid"], row["ppid"], "-" if row.get("age_h") is None else str(row["age_h"]) + " h",
            row.get("birth") or "birth unknown", "+".join(row.get("tags") or []) or "in order",
            (row.get("name") or "")[:16], here))
        out.append("          " + str(row.get("command") or "")[:118])
        if row.get("why_not"):
            out.append("          our hand stays off: " + row["why_not"])
    out.append("  TOTAL SERVERS: %d   (of %d rows which matched; %d are shadows of this query)"
               % (len(counted), len(found), len(shadows)))
    for row in shadows:
        out.append("    disclosed and not counted: pid %s %s [%s]" % (
            row["pid"], row["name"], "+".join(row.get("tags") or [])))
    if not shadows:
        out.append("  no shadows: nothing else carried the name of our script")
    for parent in sorted({row["ppid"] for row in counted}):
        kin = [row for row in counted if row["ppid"] == parent]
        live = any(row["parent"] != "gone" for row in kin)
        out.append("    parent pid %-7s %-14s spawned %d%s" % (
            parent, kin[0]["parent"], len(kin),
            "   <- the client which owns them is alive: close the window" if live
            else "   <- gone, so what it spawned is orphaned"))
    if rec.get("found"):
        out.append("  the bridge: %s:%s  pid %s  rev %s  %s" % (
            rec.get("host"), rec.get("port"), rec.get("pid"), rec.get("proto_rev"), rec.get("blender")))
        out.append("            " + ("the record carries pumped_at: the new bridge code is running"
                                    if rec.get("carries_pumped_at") else
                                    "the record has no pumped_at key: written by the OLD bridge code"))
    else:
        out.append("  the bridge: no record (%s)" % rec.get("why"))
    if port and not squatters:
        out.append("  no established socket upon port %s: nobody is at the bridge at all" % port)
    return "\n".join(out)


def main(argv=None, *, record_file="", as_json=False):
    """The entry the server's own parser calls, so that the switches are switches of the server and not
    a second program to be kept in step with it. It answers 0 when all is well and 1 when the instrument
    could not query, for a broken thermometer is not a cold day and the two are not to be told apart by
    the operator who is standing in front of a dark room."""
    argv = list(sys.argv[1:] if argv is None else argv)
    every = "--every" in argv or "--all" in argv
    kill = "--kill-stale" in argv or "--kill" in argv
    force = "--force" in argv or "--yes" in argv
    even = "--even-squatting" in argv
    older = None
    for i, arg in enumerate(argv):
        if arg.startswith("--older-than"):
            value = arg.split("=", 1)[1] if "=" in arg else (argv[i + 1] if i + 1 < len(argv) else "")
            older = float(value) if value.replace(".", "", 1).isdigit() else None
    try:
        rec = record(record_file)
        port = rec.get("port") if rec.get("found") else None
        squatters = sockets(port)
        found = servers(every=every)
        for row in found:
            if row["pid"] in squatters:
                row["tags"] = list(row["tags"]) + ["SQUATTING"]
        go, spare = kill_list(found, older=older, squatters=squatters, even=even)
        told = do_kill(go, dry=not force) if (kill and go) else []
    except CannotQuery as exc:
        print("cannot query the processes: %s" % exc, file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"servers": found, "go": go, "spared": [[r["pid"], w] for r, w in spare],
                          "told": told, "record": rec, "port": port, "squatters": sorted(squatters)},
                         indent=1))
        return 0
    print(fmt(found, rec=rec, port=port, squatters=squatters))
    if not kill:
        print("  (a server whose parent is alive is that parent's to reap: close the window, its stdin")
        print("   closes, it exits of its own accord. To have the orphaned and the stale taken away,")
        print("   say --kill-stale, and --force to mean it)")
        return 0
    if not go:
        print("  nothing is orphaned and stale together: there is nothing here to be taken away")
    for row, reason in spare:
        print("  spared pid %s: %s" % (row["pid"], reason))
    for one in told:
        print("  %s pid %s: %s%s" % ("killed" if one["did"] else "spared", one["pid"], one["said"],
                                     "" if force else "   (a dry run; say --force to mean it)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())