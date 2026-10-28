"""JARVIS's brain: questions the rule-based commands can't answer go to Claude.

Runs Claude Code headless (`claude -p`) on your own Claude login, so it uses the
claude.ai connectors (Google Calendar, Gmail, Drive, Slack, Jira) with no API key.

  ask("what's on my calendar tomorrow?", source="mac") -> {"ok", "reply", "seconds"}

Reading is free. Actions (booking, sending, replying, starting T3 sessions...) need a spoken yes:
when Claude calls an action tool, Claude Code asks us first (stream-json "can_use_tool"), and
ask() returns the question instead of an answer, with "confirm": True. The next ask() is the
answer: "yes" runs exactly that tool call, anything else cancels it (and Claude hears what was
said, so "make it 11 instead" re-asks with the change). Unanswered after 90 s, it's cancelled.

One Claude process is kept warm (connectors already connected), so answers take ~2-6 s.
Follow-ups ("and the day after?") work for 5 minutes, then it starts a fresh conversation.
If your connectors live on a non-default Claude login, set claude_config_dir in config.json.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

import settings

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
MODEL = "claude-sonnet-5-5"
EFFORT = "low"            # measured: ~4.8 s a question vs ~6.8 s at default effort, same quality (Haiku: ~5.2 s, sloppier)
WORKDIR = os.path.join(ROOT, "brain")
STATE = os.path.expanduser("~/.config/lounge/brain.json")
FOLLOW_UP_SECONDS = 300   # questions within 5 minutes of each other share a conversation
CONFIRM_SECONDS = 90      # how long a "shall I?" waits for an answer
TIMEOUT = 90
USER_NAME = settings.USER_NAME
PLACE, TIMEZONE = settings.PLACE, settings.TIMEZONE

READ_TOOLS = {
    "Google_Calendar": ["list_calendars", "list_events", "get_event", "search_events", "suggest_time"],
    "Gmail": ["search_threads", "get_thread", "get_message", "list_labels", "list_drafts", "get_draft"],
    "Google_Drive": ["search_files", "read_file_content", "get_file_metadata", "list_recent_files",
                     "download_file_content"],
    "Slack": ["slack_search_public", "slack_search_public_and_private", "slack_read_channel",
              "slack_read_thread", "slack_search_channels", "slack_search_users", "slack_read_user_profile",
              "slack_list_user_channels", "slack_read_canvas"],
    "Atlassian_MCP": ["searchJiraIssuesUsingJql", "getJiraIssue", "atlassianUserInfo",
                      "getAccessibleAtlassianResources", "searchConfluence", "getConfluenceContent", "search"],
}
# Actions: each one runs only after the user says yes (see _confirm).
ACTION_TOOLS = {
    "Google_Calendar": ["create_event", "update_event", "delete_event", "respond_to_event"],
    "Gmail": ["send_message", "reply", "forward", "create_draft", "trash_thread", "trash_message"],
    "Slack": ["slack_send_message", "slack_schedule_message", "slack_send_message_draft", "slack_add_reaction"],
    "Atlassian_MCP": ["addOrEditJiraIssueComment", "transitionJiraIssue", "createJiraIssue", "editJiraIssue"],
}
# Every other write tool on those connectors: denied outright (and kept out of the prompt).
WRITE_TOOLS = {
    "Google_Calendar": ["create_event", "delete_event", "respond_to_event", "update_event"],
    "Gmail": ["apply_sensitive_message_label", "apply_sensitive_thread_label", "create_draft", "create_label",
              "delete_draft", "delete_label", "forward", "label_message", "label_thread", "mark_message_spam",
              "mark_thread_spam", "reply", "send_message", "trash_message", "trash_thread", "unlabel_message",
              "unlabel_thread", "unmark_message_spam", "unmark_thread_spam", "untrash_message",
              "untrash_thread", "update_draft", "update_label", "update_message_labels"],
    "Google_Drive": ["copy_file", "create_file", "share_file", "trash_file", "update_file", "get_file_permissions"],
    "Slack": ["slack_add_list_record", "slack_add_reaction", "slack_complete_file_upload", "slack_create_canvas",
              "slack_create_conversation", "slack_create_list", "slack_get_file_upload_url",
              "slack_schedule_message", "slack_send_message", "slack_send_message_draft", "slack_update_canvas",
              "slack_update_list", "slack_update_list_record", "slack_read_list", "slack_read_file",
              "slack_get_reactions", "slack_list_channel_members", "slack_search_emojis"],
    "Atlassian_MCP": ["addGraphContext", "addOrEditJiraIssueComment", "createConfluenceContent", "createJiraIssue",
                      "editJiraIssue", "executeDestructive", "executeWrite", "executeRead", "discover",
                      "transitionJiraIssue", "updateConfluenceContent", "getGraphContext", "getGraphObject",
                      "getLoomObjectsContent"],
}
# Whole servers JARVIS has no use for: denied, which also keeps their tools out of the prompt.
UNUSED_SERVERS = ["claude_ai_Amplitude", "claude_ai_Canva", "claude_ai_Claude_Docs", "claude_ai_Figma",
                  "claude_ai_Notion", "claude_ai_OneSignal", "claude_ai_Supabase", "obs", "ravens", "sofasport",
                  "plugin_pdf-viewer_pdf", "plugin_small-business_shopify", "plugin_small-business_gmail",
                  "plugin_small-business_google-calendar", "plugin_data_amplitude"]

# Local MCP servers. jarvis-work (read-only, registered with `claude mcp add --scope user`) and
# jarvis-actions (start/message T3 sessions), which only the brain loads, via --mcp-config.
LOCAL_TOOLS = ["mcp__jarvis-work__work_overview", "mcp__jarvis-work__session_detail",
               "mcp__jarvis-work__search_sessions", "mcp__jarvis-actions__list_projects"]
LOCAL_ACTIONS = ["mcp__jarvis-actions__start_session", "mcp__jarvis-actions__message_session"]
MCP_CONFIG = json.dumps({"mcpServers": {"jarvis-actions": {
    "command": PYTHON, "args": ["-I", os.path.join(HERE, "workmcp.py"), "--actions"]}}})

# Fields an action can't run without; a call missing them goes back to Claude, not to the user.
REQUIRED = {"create_event": ["summary", "startTime", "endTime"], "update_event": ["eventId"],
            "delete_event": ["eventId"], "respond_to_event": ["eventId", "responseStatus"],
            "reply": ["messageId"], "forward": ["messageId"], "trash_thread": ["threadId"],
            "trash_message": ["messageId"], "slack_send_message": ["channel_id", "message"],
            "slack_schedule_message": ["channel_id", "message", "post_at"],
            "slack_send_message_draft": ["channel_id", "message"], "slack_add_reaction": ["channel_id", "emoji"],
            "addOrEditJiraIssueComment": ["issueIdOrKey"], "transitionJiraIssue": ["issueIdOrKey"],
            "createJiraIssue": ["projectKey", "summary", "issueType"], "editJiraIssue": ["issueIdOrKey"],
            "start_session": ["project", "message"], "message_session": ["session", "message"]}
ACTIONS = {f"mcp__claude_ai_{server}__{tool}" for server, tools in ACTION_TOOLS.items() for tool in tools}
ACTIONS |= set(LOCAL_ACTIONS)

# The user's settings stay on (the claude.ai connectors need them), minus hooks and plugins.
SETTINGS = json.dumps({"disableAllHooks": True})   # your own hooks and plugins stay out of JARVIS's way

PERSONA = f"""You are JARVIS, the home and work assistant of {USER_NAME}, who lives in {PLACE}.
Your reply is spoken aloud by a text-to-speech voice, so:
- Answer in one to three short sentences. Plain speech only: no markdown, lists, headings, links, emoji or IDs.
- Say times the way a person would ("half past seven tonight", "10 am tomorrow"), in {PLACE} time ({TIMEZONE}).
  Don't name the time zone unless asked.
- Lead with the answer. Summarise; don't read long content verbatim. Say "nothing" plainly when there is nothing.
- Be a calm, dry, courteous British butler. Use {USER_NAME}'s name now and then, not every time.
You can look things up in Google Calendar, Gmail, Google Drive, Slack, Jira and T3 Code sessions.
Never guess facts you didn't look up; if a lookup fails, say so briefly.

You can also act for {USER_NAME}: book, move, cancel or answer calendar invites; send, reply to, forward, draft or
trash email; send or schedule Slack messages and add reactions; comment on, create, edit or move Jira issues; start a
T3 Code session in a project, or send a message to an existing session. Every action is confirmed: when you call an
action tool, {USER_NAME} is asked aloud and it only runs if they say yes. So:
- Load an action tool with ToolSearch before its first use, so you call it with its real fields.
- Look up what you need first (email addresses, event IDs, Slack channel or user IDs, the message to reply to).
  Don't ask {USER_NAME} for things you can look up. If who or when is genuinely unclear, ask one short question.
- Right before calling the action tool, write ONE short sentence saying exactly what you will do, with the key
  details in plain words, e.g. "I'll book Standup with Sam for 10 am tomorrow, half an hour, with a Meet link."
  That sentence is read out as the question, so don't ask "shall I?" in a separate turn and don't wait for permission.
- If the action is refused because {USER_NAME} said no, acknowledge in a few words. If they changed the details,
  call the tool again with the changes. After it runs, confirm in a few words ("Done, it's booked.").
- Emails and messages: short, plain text, in {USER_NAME}'s voice, no signature unless asked. Calendar times: local
  {PLACE} time with no UTC offset. Meetings with other people get a Google Meet link unless told otherwise.
- You can't stop or interrupt T3 sessions or answer their approval prompts; {USER_NAME} must do that in T3.

T3 Code sessions (jarvis-work tools): what's running, finished, failed or needs {USER_NAME}, and what each did.
"What are we working on" means these sessions. When summarising sessions, name each by its topic in a few words, say
its state, and mention anything that needs {USER_NAME} first. "Needs you" means an approval or question is
pending; "waiting on a background job" doesn't need them. You can't control the house from here yet.
Calendar: use the primary calendar unless told otherwise. Jira: "my issues" means assigned to the current user."""

LOOKUP_PERSONA = ("You fetch data for a home assistant's background jobs. Use the tools you need, then reply with "
                  "only the JSON asked for: no prose, no markdown fences.")

PRIVATE_NOTE = ("PRIVACY MODE IS ON and this answer is shown on the living-room TV: do not read out or quote "
                "the contents of emails or messages. Give only counts, senders and a vague topic at most.")

_lock = threading.Lock()       # one question at a time
_state_lock = threading.Lock()
_pending = {}                  # the action waiting for a yes (see _wait)
_announced = []                # (time, text) of recent announcements, so "what does it want?" makes sense
after_action = []              # callbacks(tool_name) run after an approved action (announce.py refreshes the calendar)
context = []                   # callables returning a line of background for each question (announce.py: the calendar)


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def setting(name, default=None):
    return _load().get(name, default)


def set_setting(name, value):
    with _state_lock:
        state = _load()
        state[name] = value
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE)


def privacy():
    return bool(setting("privacy"))


def set_privacy(on):
    set_setting("privacy", bool(on))


def note_announcement(text):
    _announced.append((time.time(), text))
    del _announced[:-10]


def pending():
    """True while JARVIS is waiting for a yes or no to an action."""
    return bool(_pending)


def _command(persona=PERSONA, stream=True):
    allowed = [f"mcp__claude_ai_{server}__{tool}" for server, tools in READ_TOOLS.items() for tool in tools]
    allowed += LOCAL_TOOLS
    denied = [f"mcp__claude_ai_{server}__{tool}" for server, tools in WRITE_TOOLS.items() for tool in tools
              if tool not in ACTION_TOOLS.get(server, [])]
    denied += [f"mcp__{server}" for server in UNUSED_SERVERS]
    cmd = [CLAUDE, "-p", "--model", MODEL, "--effort", EFFORT, "--verbose",
           "--tools", "ToolSearch",             # no shell, files or web; connector tools load on demand
           "--permission-mode", "default",      # allowed tools run; actions ask us; denied never run
           "--allowedTools", ",".join(allowed),
           "--disallowedTools", ",".join(denied),
           "--mcp-config", MCP_CONFIG,
           "--disable-slash-commands", "--settings", SETTINGS,
           "--append-system-prompt", persona]
    if stream:
        cmd += ["--input-format", "stream-json", "--output-format", "stream-json", "--permission-prompt-tool", "stdio"]
    else:
        cmd += ["--output-format", "json", "--permission-prompts", "none"]   # background lookups never act
    return cmd


def _env():
    return dict(os.environ, MCP_CONNECTION_NONBLOCKING="false", ENABLE_TOOL_SEARCH="true",
                CLAUDE_CONFIG_DIR=os.environ.get("CLAUDE_CONFIG_DIR") or settings.CLAUDE_CONFIG_DIR or os.path.expanduser("~/.claude"))


class _Worker:
    """One Claude process kept running, so the connectors are already up when you ask.

    Questions share one conversation (follow-ups work). After FOLLOW_UP_SECONDS of quiet,
    or MAX_TURNS questions, it's swapped for a fresh, already-warm process.
    """
    MAX_TURNS = 30

    def __init__(self):
        os.makedirs(WORKDIR, exist_ok=True)
        self.proc = subprocess.Popen(_command(), cwd=WORKDIR, env=_env(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.events = queue.Queue()   # ("result", msg) | ("permission", msg) | ("ended", None)
        self.said = ""                # Claude's latest text in this turn: the "I'll book..." before an action
        self.turns = 0
        self.last_used = time.time()
        threading.Thread(target=self._read, daemon=True).start()
        self._send({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}})

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "assistant":
                for block in (msg.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        self.said = block["text"]
            elif kind == "result":
                self.events.put(("result", msg))
            elif kind == "control_request" and (msg.get("request") or {}).get("subtype") == "can_use_tool":
                self.events.put(("permission", msg))
        self.events.put(("ended", None))  # process ended

    def alive(self):
        return self.proc.poll() is None

    def ask(self, question):
        self.turns += 1
        self.said = ""
        self.last_used = time.time()
        self._send({"type": "user", "message": {"role": "user", "content": question}})

    def wait(self, timeout):
        event = self.events.get(timeout=timeout)
        self.last_used = time.time()
        return event

    def answer(self, request_id, allow, tool_input=None, message=""):
        decision = ({"behavior": "allow", "updatedInput": tool_input} if allow
                    else {"behavior": "deny", "message": message})
        self._send({"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                              "response": decision}})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


_worker = {"w": None}


def _fresh_worker():
    _pending.clear()
    old, _worker["w"] = _worker["w"], _Worker()
    if old:
        threading.Thread(target=old.close, daemon=True).start()
    return _worker["w"]


def warm_up():
    """Start the Claude process in the background and keep it fresh. Call once at server start."""
    def loop():
        with _lock:
            _fresh_worker()
        while True:
            time.sleep(5)
            with _lock:
                if _pending and time.time() - _pending["at"] > CONFIRM_SECONDS:
                    _expire()
                    continue
                if _pending:
                    continue
                w = _worker["w"]
                stale = w.turns and time.time() - w.last_used > FOLLOW_UP_SECONDS
                if not w.alive() or stale or w.turns >= _Worker.MAX_TURNS:
                    _fresh_worker()
    threading.Thread(target=loop, daemon=True).start()


# ---- spoken confirmations ----------------------------------------------------------------------

# Only a deliberate phrase runs an action. A bare "yes" isn't enough: Whisper hears "Yes." in short or
# near-silent clips (it turned a spoken "No." into "Yes." in testing), so that only asks again.
YES = re.compile(r"^((yes|yeah|yep|ok|okay|sure)( please)?,? )?(go ahead|go for it|do it|send it|book it|post it|"
                 r"start it|confirm|confirmed|proceed|make it so)\b|^yes,? please\b|^please do\b")
WEAK_YES = re.compile(r"^(yes|yeah|yep|yup|ya|sure|ok|okay|alright|all right|correct|right|fine|affirmative|"
                      r"absolutely|definitely|that's right|sounds good|perfect)( (it is|that's right|sir))?$")
NO = re.compile(r"^(no|nope|nah|cancel|don't|do not|stop|never ?mind|forget it|scratch that|hold on|wait|not now|"
                r"leave it)\b")
HEDGE = re.compile(r"\b(but|change|instead|except|actually|wait|not|don't|rather|make it)\b")


def _decision(text):
    t = re.sub(r"[^\w\s']", " ", text.lower())
    t = re.sub(r"^\s*((hey )?jarvis|um+|uh+|erm|oh)\s+", "", re.sub(r"\s+", " ", t)).strip()
    if NO.match(t):
        return "no"
    if YES.match(t) and len(t.split()) <= 6 and not HEDGE.search(t):
        return "yes"
    if WEAK_YES.match(t):
        return "weak"
    return "other"


def _when(iso):
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    return d.strftime("%a %-d %b, %H:%M")


def _list(v):
    return ", ".join(v) if isinstance(v, list) else str(v or "")


def describe(tool, a):
    """The exact action, for the screen: who it goes to, what it says, when."""
    name = tool.split("__")[-1]
    a = a or {}
    attendees = _list([x.get("email", "") for x in a.get("attendees") or []] + (a.get("attendeeEmails") or []))
    lines = {
        "create_event": f"New event \u201c{a.get('summary')}\u201d, {_when(a.get('startTime'))} to "
                        f"{_when(a.get('endTime'))[-5:]}" + (f", with {attendees}" if attendees else "")
                        + (", Meet link" if a.get("addGoogleMeetUrl") else ""),
        "update_event": "Change event: " + ", ".join(
            f"{k} {_when(v) if k.endswith('Time') else v}" for k, v in a.items()
            if k not in ("eventId", "calendarId") and v not in (None, "", [])),
        "delete_event": "Delete a calendar event",
        "respond_to_event": f"Answer invite: {a.get('responseStatus')}",
        "send_message": f"Email to {_list(a.get('to'))}" + (f", cc {_list(a.get('cc'))}" if a.get("cc") else "")
                        + f" \u2014 \u201c{a.get('subject', '')}\u201d\n{a.get('body', '')}",
        "reply": f"Email reply{' to all' if a.get('replyAll') else ''}:\n{a.get('body', '')}",
        "forward": f"Forward email to {_list(a.get('to'))}\n{a.get('forwardText', '')}",
        "create_draft": f"Draft email to {_list(a.get('to'))} \u2014 \u201c{a.get('subject', '')}\u201d\n{a.get('body', '')}",
        "trash_thread": "Move an email thread to the Trash",
        "trash_message": "Move an email to the Trash",
        "slack_send_message": f"Slack message:\n{a.get('message', '')}",
        "slack_send_message_draft": f"Slack draft:\n{a.get('message', '')}",
        "slack_schedule_message": f"Slack message at {_when(datetime.fromtimestamp(a['post_at']).isoformat()) if a.get('post_at') else '?'}:\n{a.get('message', '')}",
        "slack_add_reaction": f"React :{a.get('emoji')}: on Slack",
        "addOrEditJiraIssueComment": f"Comment on {a.get('issueIdOrKey')}:\n{a.get('commentBody', '')}",
        "transitionJiraIssue": f"Move {a.get('issueIdOrKey')} \u2192 {a.get('transitionName') or a.get('transitionId') or 'sprint'}",
        "createJiraIssue": f"New {a.get('issueType')} in {a.get('projectKey')}: {a.get('summary')}",
        "editJiraIssue": f"Edit {a.get('issueIdOrKey')}",
        "start_session": f"New T3 session in {a.get('project')}:\n{a.get('message', '')}",
        "message_session": f"Message to T3 session \u201c{a.get('session')}\u201d:\n{a.get('message', '')}",
    }
    text = lines.get(name) or f"{name}: {json.dumps(a)[:300]}"
    return text[:900]


def _intent(said, details):
    """What JARVIS says it's about to do, as one spoken question."""
    t = re.sub(r"[*_`#>]+", "", said or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", t)
    plans = [s for s in sentences if re.match(r"(I'll|I will|I'm going to|I am going to|I'd like to)\b", s)]
    t = (plans[-1] if plans else " ".join(sentences[-2:]))[-320:].strip() if t else ""
    if not t:
        t = details.split("\n")[0] + "."
    return t if t.endswith("?") else f"{t} Shall I go ahead?"


def _wait(w, source):
    """Wait for this turn to end, or stop at the first action that needs a yes."""
    deadline = time.time() + TIMEOUT
    while True:
        try:
            kind, msg = w.wait(max(1, deadline - time.time()))
        except queue.Empty:
            return {"error": "timed out"}
        if kind == "ended":
            return {"error": "Claude stopped"}
        if kind == "result":
            return {"result": msg}
        req = msg.get("request") or {}
        tool, tool_input = req.get("tool_name", ""), req.get("input") or {}
        if tool not in ACTIONS:
            w.answer(msg["request_id"], False, message="That tool isn't available to JARVIS.")
            continue
        missing = [k for k in REQUIRED.get(tool.split("__")[-1], []) if not tool_input.get(k)]
        if not tool_input or missing:  # usually a deferred tool called before its schema was loaded
            w.answer(msg["request_id"], False, message=(
                f"That call was missing {', '.join(missing) or 'its arguments'}. Load the tool with ToolSearch "
                "if you haven't, then call it again with every required field filled in."))
            continue
        if source != "mac" and privacy():
            w.answer(msg["request_id"], False, message="Privacy mode is on, so actions only work when asked from "
                                                       "the Mac. Tell {USER_NAME} that, briefly.")
            continue
        details = describe(tool, tool_input)
        _pending.update(worker=w, request_id=msg["request_id"], tool=tool, input=tool_input, at=time.time(),
                        source=source, details=details)
        print(f"BRAIN confirm? {tool} {json.dumps(tool_input)[:300]}", flush=True)
        return {"confirm": _intent(w.said, details), "details": details}


def _confirm(text):
    """The user's answer to the pending "shall I?"."""
    p, decision = dict(_pending), _decision(text)
    print(f"BRAIN confirm {decision}: {text[:80]!r} for {p['tool']}", flush=True)
    if decision == "weak":   # still waiting; ask for the deliberate phrase
        _pending["at"] = time.time()
        return {"confirm": "To be safe, say go ahead, or cancel.", "details": p["details"]}
    _pending.clear()
    w = p["worker"]
    if decision == "yes":
        w.answer(p["request_id"], True, p["input"])
    elif decision == "no":
        w.answer(p["request_id"], False, message=f"{USER_NAME} said no, so it was not done. Acknowledge in a few words.")
    else:
        w.answer(p["request_id"], False, message=f"{USER_NAME} didn't say yes, so it was not done. Instead they said: "
                                                 f"{text!r}. If that changes the details, call the tool again with the "
                                                 "changes. If it's something else, deal with that.")
    result = _wait(w, p["source"])
    if decision == "yes" and "result" in result:
        for callback in after_action:
            threading.Thread(target=callback, args=(p["tool"],), daemon=True).start()
    return result


def _expire():
    p = dict(_pending)
    _pending.clear()
    print(f"BRAIN confirm expired for {p['tool']}", flush=True)
    p["worker"].answer(p["request_id"], False, message="Nobody answered, so it was not done. Reply with just: Cancelled.")
    _wait(p["worker"], p["source"])   # let the turn finish; nobody is listening for it


def ask(text, source="mac"):
    """Answer a question. `source` is where it was asked: "mac", "remote" or "tv"."""
    text = (text or "").strip()[:500]
    started = time.time()
    with _lock:
        if _pending:
            result = _confirm(text)
        else:
            w = _worker["w"]
            if w is None or not w.alive():
                w = _fresh_worker()
            now = time.strftime("%A %d %B %Y, %H:%M", time.localtime())
            note = f" {PRIVATE_NOTE}" if source != "mac" and privacy() else ""
            recent = [t for at, t in _announced if time.time() - at < 900]
            if recent:
                note += " You recently announced: " + " / ".join(recent[-3:])
            for provide in context:
                try:
                    note += " " + (provide() or "")
                except Exception as e:
                    print("BRAIN context error:", repr(e)[:120], flush=True)
            try:
                w.ask(f"[It is {now} in {PLACE}. Asked from: {source}.{note}]\n{text}")
                result = _wait(w, source)
            except (OSError, ValueError) as e:
                result = {"error": repr(e)}
        seconds = round(time.time() - started, 1)
        if "confirm" in result:
            return {"ok": True, "reply": result["confirm"], "confirm": True, "details": result["details"],
                    "seconds": seconds}
        msg = result.get("result") or {}
        reply = (msg.get("result") or "").strip()
        if not reply or msg.get("is_error"):
            print("BRAIN error:", result.get("error") or msg.get("subtype"), flush=True)
            _fresh_worker()
            return {"ok": False, "seconds": seconds,
                    "reply": f"I'm sorry, {USER_NAME}, I couldn't get an answer just now. Try asking again."}
        print(f"BRAIN {source} {seconds}s turns={msg.get('num_turns')}: {text[:80]!r} -> {reply[:120]!r}", flush=True)
        return {"ok": True, "reply": reply, "seconds": seconds}


def lookup_json(prompt, timeout=150):
    """A one-off background lookup (its own Claude process, never acts). Returns parsed JSON or None."""
    try:
        out = subprocess.run(_command(LOOKUP_PERSONA, stream=False) + [prompt], cwd=WORKDIR, env=_env(),
                             capture_output=True, text=True, timeout=timeout).stdout
        data = json.loads(out[min(i for i in (out.find("["), out.find("{")) if i >= 0):])
        if isinstance(data, list):  # --verbose: every message; the last "result" is the answer
            data = next(m for m in reversed(data) if m.get("type") == "result")
        text = data.get("result") or ""
        text = re.sub(r"^```(json)?|```$", "", text.strip()).strip()
        return json.loads(text[min(i for i in (text.find("["), text.find("{")) if i >= 0):])
    except Exception as e:
        print("BRAIN lookup failed:", repr(e)[:200], flush=True)
        return None


if __name__ == "__main__":
    import sys
    for q in sys.argv[1:] or ["What's on my calendar tomorrow?"]:
        print(ask(q, source="cli"))
