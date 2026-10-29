"""jarvis-work: an MCP server that tells JARVIS (or any Claude session) what you're working on.

Reads T3 Code's own database, read-only: ~/.t3/userdata/statev2.sqlite.
Only top-level sessions (threads) are listed; subagent threads are folded into their parent.

Tools:
  work_overview(hours=24)        what needs you, what is running, finished or failed
  session_detail(session)        one session by (part of) its title: status, asks, latest result
  search_sessions(query, days)   find sessions by words in their title or conversation

With --actions it is "jarvis-actions" instead, for JARVIS's brain only (see brain.py):
  start_session(project, message)  start a new T3 session (via the `t3` CLI)
  message_session(session, message) send a message to an existing session
  home_command(command)            control the TV or Xbox ("open FIFA on the Xbox"), via JARVIS's own commands
  follow_session(session)          JARVIS stays with this session: says when it finishes or needs you
  stop_following()                 ...and stops (starting or messaging a session follows it too)
JARVIS asks the user to say "yes" before the session tools run (and before switching anything off); this server
doesn't check that itself.

Speaks MCP over stdio (newline-delimited JSON-RPC), no dependencies.
  python3 tools/workmcp.py --test   prints the overview, for checking by hand
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import settings

DB = os.path.expanduser("~/.t3/userdata/statev2.sqlite")
T = "orchestration_v2_projection_"

STATUS = {"running": "working", "starting": "working", "preparing": "working", "queued": "queued",
          "waiting": "waiting on a background job", "completed": "finished", "failed": "failed",
          "cancelled": "stopped", "interrupted": "interrupted"}


def _db():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _when(iso):
    """'12 minutes ago' style, for speech."""
    if not iso:
        return "never"
    then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    s = (datetime.now(timezone.utc) - then).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{round(s / 60)} minutes ago"
    if s < 86400:
        h = round(s / 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = round(s / 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


def _clip(text, n, keep_end=False):
    """Shorten to n characters. keep_end keeps the start and the end (replies often end with a question)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    if not keep_end:
        return text[: n - 1].rstrip() + "…"
    head = n // 3
    return text[:head].rstrip() + " … " + text[-(n - head - 3):].lstrip()


def _projects(con):
    return {r["project_id"]: r["title"] for r in con.execute("select project_id, title from projection_projects")}


def _top_level_threads(con, since_iso=None, include_archived=False):
    sql = (f"select thread_id, project_id, title, created_at, updated_at, archived_at, payload_json from {T}threads "
           "where deleted_at is null and json_extract(payload_json, '$.lineage.parentThreadId') is null")
    if not include_archived:
        sql += " and archived_at is null"
    args = []
    if since_iso:
        sql += " and updated_at >= ?"
        args.append(since_iso)
    return con.execute(sql + " order by updated_at desc", args).fetchall()


def _latest_run(con, thread_id):
    return con.execute(f"select run_id, status, requested_at, completed_at from {T}runs where thread_id = ? "
                       "order by ordinal desc limit 1", (thread_id,)).fetchone()


def _pending_requests(con, thread_id):
    rows = con.execute(f"select kind from {T}runtime_requests where thread_id = ? and status = 'pending'",
                       (thread_id,)).fetchall()
    return [r["kind"] for r in rows]


def _message_text(row):
    try:
        return json.loads(row["payload_json"]).get("text") or ""
    except ValueError:
        return ""


AUTOMATED = re.compile(r"^\s*(Background command|<task-notification|<system-reminder|Context handoff|"
                       r"Provider context handoff)", re.I)


def _last_message(con, thread_id, role, run_id=None):
    """The latest message from `role`; for "user", the latest one the user actually wrote."""
    sql = f"select payload_json from {T}messages where thread_id = ? and role = ? and streaming = 0"
    args = [thread_id, role]
    if run_id:
        sql += " and run_id = ?"
        args.append(run_id)
    for row in con.execute(sql + " order by created_at desc limit 30", args):
        text = _message_text(row)
        if text and not (role == "user" and AUTOMATED.match(text)):
            return text
    return ""


def _subagents(con, thread_id):
    """Counts of this session's helper agents by status, e.g. {"working": 2, "finished": 9}."""
    rows = con.execute(f"select status, count(*) n from {T}subagents where thread_id = ? group by status",
                       (thread_id,)).fetchall()
    return {STATUS.get(r["status"], r["status"]): r["n"] for r in rows}


def _state(con, thread_id):
    run = _latest_run(con, thread_id)
    pending = _pending_requests(con, thread_id)
    status = STATUS.get(run["status"], run["status"]) if run else "not started"
    if pending:
        status = "needs you (" + ", ".join(sorted(set(pending))) + ")"
    return run, status


def work_overview(hours=24):
    hours = max(1, min(24 * 14, int(hours or 24)))
    since = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with _db() as con:
        names = _projects(con)
        threads = _top_level_threads(con, since)
        rows = []
        for t in threads[:25]:
            run, status = _state(con, t["thread_id"])
            outcome = _last_message(con, t["thread_id"], "assistant", run["run_id"] if run else None)
            if not outcome:  # still working on it: report its latest progress instead
                outcome = _last_message(con, t["thread_id"], "assistant")
            rows.append({"title": t["title"], "project": names.get(t["project_id"], "?"), "status": status,
                         "last_activity": _when(t["updated_at"]),
                         "last_ask": _clip(_last_message(con, t["thread_id"], "user"), 160),
                         "latest_result": _clip(outcome, 400, keep_end=True),
                         "helpers": _subagents(con, t["thread_id"]) or None})
    order = {"needs you": 0, "working": 1, "waiting": 2, "queued": 3, "failed": 4, "interrupted": 5}
    rows.sort(key=lambda r: next((v for k, v in order.items() if r["status"].startswith(k)), 9))
    counts = {}
    for r in rows:
        key = r["status"].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    return {"window": f"last {hours} hours", "now": datetime.now().strftime("%A %H:%M"),
            "counts": counts, "sessions": rows,
            "note": "Sessions are T3 Code threads. 'latest_result' is the assistant's last message in the latest run."}


def _find(con, query):
    q = (query or "").lower().strip()
    if not q:
        return None
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2 and w not in ("the", "session", "thread", "one")]
    best, best_score = None, 0
    for t in _top_level_threads(con, include_archived=True)[:400]:
        title = t["title"].lower()
        score = (10 if q in title else 0) + sum(2 for w in words if w in title)
        if score and t["archived_at"] is None:
            score += 1
        if score > best_score:
            best, best_score = t, score
    return best


def session_detail(session):
    with _db() as con:
        t = _find(con, session)
        if not t:
            return {"error": f"No session matching {session!r}. Try search_sessions."}
        names = _projects(con)
        run, status = _state(con, t["thread_id"])
        payload = json.loads(t["payload_json"])
        runs = con.execute(f"select run_id, status, requested_at, completed_at from {T}runs where thread_id = ? "
                           "order by ordinal desc limit 5", (t["thread_id"],)).fetchall()
        exchanges, last_done = [], None
        for r in reversed(runs):
            ask = con.execute(f"select payload_json from {T}messages where run_id = ? and role = 'user' "
                              "order by created_at limit 1", (r["run_id"],)).fetchone()
            asked = _message_text(ask) if ask else ""
            if asked.strip().startswith("/"):
                continue  # slash commands like /compact aren't real turns
            if AUTOMATED.match(asked):
                asked = "(automatic: a background task finished)"
            reply = _last_message(con, t["thread_id"], "assistant", r["run_id"])
            if reply and r["completed_at"]:
                last_done = r["run_id"]
            exchanges.append({"asked": _when(r["requested_at"]),
                              "answered": _when(r["completed_at"]) if r["completed_at"] else "not yet",
                              "status": STATUS.get(r["status"], r["status"]),
                              "you_asked": _clip(asked, 300),
                              "result": _clip(reply, 400, keep_end=True) if reply else "(no reply yet)"})
        # The newest finished reply, nearly whole: its ending is usually the question put to the user.
        last_reply = _last_message(con, t["thread_id"], "assistant", last_done) if last_done else ""
        total = con.execute(f"select count(*) from {T}runs where thread_id = ?", (t["thread_id"],)).fetchone()[0]
        return {"title": t["title"], "project": names.get(t["project_id"], "?"), "status": status,
                "started": _when(t["created_at"]), "last_activity": _when(t["updated_at"]),
                "archived": t["archived_at"] is not None, "turns": total,
                "branch": payload.get("branch"), "pull_requests": len(payload.get("pullRequests") or []),
                "subagents": _subagents(con, t["thread_id"]), "recent_turns": exchanges,
                "latest_reply_in_full": _clip(last_reply, 3000, keep_end=True),
                "note": "recent_turns are oldest first. latest_reply_in_full is the newest finished reply; "
                        "any question or options it ends with are what the session is asking the user."}


def search_sessions(query, days=14):
    days = max(1, min(90, int(days or 14)))
    since = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    like = f"%{(query or '').strip()}%"
    with _db() as con:
        names = _projects(con)
        hits = {}
        for t in _top_level_threads(con, since, include_archived=True):
            if like.strip("%").lower() in t["title"].lower():
                hits[t["thread_id"]] = (t, "title")
        rows = con.execute(f"select m.thread_id, m.payload_json from {T}messages m join {T}threads t "
                           "on t.thread_id = m.thread_id where m.created_at >= ? and t.deleted_at is null "
                           "and json_extract(t.payload_json, '$.lineage.parentThreadId') is null "
                           "and json_extract(m.payload_json, '$.text') like ? order by m.created_at desc limit 200",
                           (since, like)).fetchall()
        for r in rows:
            if r["thread_id"] not in hits:
                t = con.execute(f"select * from {T}threads where thread_id = ?", (r["thread_id"],)).fetchone()
                text = _message_text(r)
                i = text.lower().find(like.strip("%").lower())
                hits[r["thread_id"]] = (t, _clip(text[max(0, i - 80): i + 160], 240))
            if len(hits) >= 10:
                break
        return {"query": query, "matches": [
            {"title": t["title"], "project": names.get(t["project_id"], "?"), "last_activity": _when(t["updated_at"]),
             "matched": where} for t, where in list(hits.values())[:10]]}


T3_CLI = os.path.expanduser("~/.t3/bin/t3")
# Which T3 provider instance and model new sessions use (config.json "t3"); T3's CLI ignores
# project defaults, so set both or T3 may refuse to start the session.
NEW_SESSION_PROVIDER = settings.T3.get("provider", "")
NEW_SESSION_MODEL = settings.T3.get("model", "")


def _t3(*args):
    out = subprocess.run([T3_CLI, *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:400] or f"t3 exited {out.returncode}")
    return out.stdout.strip()


def _find_project(con, name):
    q = (name or "").lower().strip()
    rows = con.execute("select project_id, title, workspace_root from projection_projects "
                       "where deleted_at is null order by updated_at desc").fetchall()
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    for r in rows:
        if squash(r["title"]) == squash(q) or squash(os.path.basename(r["workspace_root"])) == squash(q):
            return r
    return next((r for r in rows if squash(q) and squash(q) in squash(r["title"])), None)


def list_projects():
    with _db() as con:
        rows = con.execute("select title, workspace_root from projection_projects where deleted_at is null "
                           "order by updated_at desc").fetchall()
    return {"projects": [{"name": r["title"], "folder": r["workspace_root"]} for r in rows]}


def start_session(project, message, title=None):
    with _db() as con:
        p = _find_project(con, project)
    if not p:
        return {"error": f"No project called {project!r}.", **list_projects()}
    # Named explicitly when configured: the t3 CLI ignores project defaults. It can't set effort,
    # so new sessions start at the model's default effort; existing sessions keep their own settings.
    args = ["thread", "start", p["project_id"], message.strip(), "--json"]
    if NEW_SESSION_PROVIDER and NEW_SESSION_MODEL:
        args += ["--provider", NEW_SESSION_PROVIDER, "--model", NEW_SESSION_MODEL]
    if title:
        args += ["--title", title]
    out = _t3(*args)
    try:
        info = json.loads(out)
    except ValueError:
        info = {"output": out[:300]}
    thread = info.get("thread") if isinstance(info.get("thread"), dict) else info
    thread_id = thread.get("threadId") or thread.get("id")
    followed = bool(thread_id) and _follow_quietly(thread_id, title or thread.get("title") or message.strip()[:60])
    return {"started": True, "project": p["title"], "thread": info, "following": followed}


def message_session(session, message):
    with _db() as con:
        t = _find(con, session)
    if not t:
        return {"error": f"No session matching {session!r}. Try search_sessions."}
    _t3("thread", "send", t["thread_id"], message.strip())
    followed = _follow_quietly(t["thread_id"], t["title"])
    return {"sent": True, "session": t["title"], "following": followed,
            "note": "A busy session gets it as its next turn; nothing was interrupted."}


def most_recent_session():
    """Title of the top-level session with the latest activity (likely the one open in T3)."""
    with _db() as con:
        rows = _top_level_threads(con)
    return rows[0]["title"] if rows else None


def _server(path, **params):
    url = f"http://127.0.0.1{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def follow_session(session):
    with _db() as con:
        t = _find(con, session)
        if not t:
            return {"error": f"No session matching {session!r}. Try search_sessions."}
        _, status = _state(con, t["thread_id"])
    _server("/api/follow", thread=t["thread_id"], title=t["title"])
    return {"following": t["title"], "status": status,
            "note": "JARVIS now says out loud when this session finishes, fails or needs you."}


def stop_following():
    was = _server("/api/follow", stop="1").get("was")
    return {"stopped": True, "was_following": was}


def _follow_quietly(thread_id, title):
    try:
        _server("/api/follow", thread=thread_id, title=title)
        return True
    except OSError:
        return False


def home_command(command):
    """Run one of JARVIS's instant TV/Xbox commands through the Lounge server (never back into the brain)."""
    url = "http://127.0.0.1/api/command?" + urllib.parse.urlencode({"text": command, "from": "mac", "rules": "1"})
    with urllib.request.urlopen(url, timeout=30) as r:
        out = json.load(r)
    if out.get("no_match"):
        return {"done": False, "note": "That isn't a TV or Xbox command JARVIS knows. Try a simpler wording, e.g. "
                                       "'open FIFA on the Xbox', 'switch to the Xbox', 'volume to 20'."}
    return {"done": bool(out.get("ok")), "result": out.get("reply", "")}


ACTION_TOOLS = [
    {"name": "list_projects",
     "description": "The T3 Code projects new sessions can be started in.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "start_session",
     "description": "Start a new T3 Code session (a coding agent) in a project, with the user's request as its first "
                    "message. Write the message the way the user would type it to the agent.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "Project name, e.g. etl-django"},
         "message": {"type": "string"}, "title": {"type": "string", "description": "Optional short title"}},
         "required": ["project", "message"]}},
    {"name": "message_session",
     "description": "Send a message to an existing T3 Code session, found by part of its title. If it is busy, "
                    "it gets the message as its next turn.",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}, "message": {"type": "string"}},
                     "required": ["session", "message"]}},
    {"name": "follow_session",
     "description": "Stay with one T3 Code session (found by part of its title): JARVIS then says out loud when it "
                    "finishes, fails or needs the user, with a short summary of its reply.",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}}, "required": ["session"]}},
    {"name": "stop_following",
     "description": "Stop following the T3 session JARVIS is following.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "home_command",
     "description": "Control the living-room TV or the Xbox with one plain command, the way the user would say it: "
                    "'open FIFA on the Xbox', 'switch to the Xbox', 'Xbox home', 'turn on the Xbox', 'volume to 20', "
                    "'mute', 'HDMI 2', 'what's the volume'. Returns what happened.",
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]
ACTION_HANDLERS = {"list_projects": list_projects, "start_session": start_session,
                   "message_session": message_session, "home_command": home_command,
                   "follow_session": follow_session, "stop_following": stop_following}

TOOLS = [
    {"name": "work_overview",
     "description": "What the user is working on: their T3 Code sessions active in the last N hours, with status "
                    "(needs you, working, waiting on a background job, finished, failed, stopped), project, what they last asked and the "
                    "latest result. Use for 'what are we working on', 'what's running', 'anything need me'. Only 'needs you' means a pending approval or question.",
     "inputSchema": {"type": "object", "properties": {"hours": {"type": "integer", "default": 24}}}},
    {"name": "session_detail",
     "description": "Details of one T3 Code session, found by part of its title (e.g. 'Japan League'): status, "
                    "its subagents, branch, and the last few things asked and the results.",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}}, "required": ["session"]}},
    {"name": "search_sessions",
     "description": "Find T3 Code sessions whose title or conversation mentions some words, within the last N days.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"},
                                                      "days": {"type": "integer", "default": 14}},
                     "required": ["query"]}},
]
HANDLERS = {"work_overview": work_overview, "session_detail": session_detail, "search_sessions": search_sessions}


def _reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def serve(tools, handlers, name):
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method, id_ = req.get("method"), req.get("id")
        if id_ is None:  # notification
            continue
        if method == "initialize":
            _reply(id_, {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": name, "version": "1.0"}})
        elif method == "tools/list":
            _reply(id_, {"tools": tools})
        elif method == "tools/call":
            p = req.get("params", {})
            fn = handlers.get(p.get("name"))
            if not fn:
                _reply(id_, error={"code": -32601, "message": f"unknown tool {p.get('name')}"})
                continue
            try:
                out = fn(**(p.get("arguments") or {}))
                _reply(id_, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]})
            except Exception as e:
                _reply(id_, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        elif method == "ping":
            _reply(id_, {})
        else:
            _reply(id_, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    if "--test" in sys.argv:
        print(json.dumps(work_overview(24), indent=1, ensure_ascii=False)[:4000])
        print(json.dumps(session_detail("Japan League"), indent=1, ensure_ascii=False)[:2500])
    elif "--actions" in sys.argv:
        serve(ACTION_TOOLS, ACTION_HANDLERS, "jarvis-actions")
    else:
        serve(TOOLS, HANDLERS, "jarvis-work")
