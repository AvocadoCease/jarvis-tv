"""Control the Xbox through Microsoft's cloud (xbox-webapi), after a one-time sign-in.

Local wake-up (xbox.py) is ignored by this console, so power and buttons go
through Microsoft: Mac -> Xbox Live -> console. Needs the console's
"Remote features" setting on, and internet.

Secrets live in ~/.config/lounge (mode 700), never in the project:
  xbox-app.json     Azure app registration: client ID + secret (entered on /setup/xbox)
  xbox-tokens.json  OAuth refresh token from the user's sign-in
"""
import asyncio
import json
import os
import re
import secrets
import time

from xbox.webapi.api.client import XboxLiveClient
from xbox.webapi.api.provider.smartglass.models import InputKeyType, PowerState
from xbox.webapi.authentication.manager import AuthenticationManager
from xbox.webapi.authentication.models import OAuth2TokenResponse
from xbox.webapi.common.signed_session import SignedSession

CONFIG_DIR = os.path.expanduser("~/.config/lounge")
APP_FILE = os.path.join(CONFIG_DIR, "xbox-app.json")
TOKENS_FILE = os.path.join(CONFIG_DIR, "xbox-tokens.json")
REDIRECT_URI = "http://localhost/auth/callback"

BUTTONS = {
    "a": InputKeyType.A, "b": InputKeyType.B, "x": InputKeyType.X, "y": InputKeyType.Y,
    "up": InputKeyType.Up, "down": InputKeyType.Down, "left": InputKeyType.Left, "right": InputKeyType.Right,
    "menu": InputKeyType.Menu, "view": InputKeyType.View, "guide": InputKeyType.Guide,
}

# What people say -> words in the store name
ALIASES = {"fifa": "ea sports fc", "fc": "ea sports fc", "ea fc": "ea sports fc", "2k": "nba 2k", "nba": "nba 2k",
           "apple tv": "apple tv", "disney": "disney", "disney plus": "disney", "dstv": "dstv", "spotify": "spotify",
           "music": "apple music", "movies": "movies  tv"}

_pending_state = {"value": None, "at": 0.0}
_console = {"id": None, "name": None}
_apps = {"list": None, "at": 0.0}


# ------------------------------------------------------------------ secret storage

def _write_private(path, data):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)


def configured():
    return os.path.exists(APP_FILE)


def signed_in():
    return configured() and os.path.exists(TOKENS_FILE)


def save_app(client_id, client_secret):
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("Both the client ID and the secret are needed")
    _write_private(APP_FILE, json.dumps({"client_id": client_id, "client_secret": client_secret}))


def _app():
    with open(APP_FILE) as f:
        return json.load(f)


# ------------------------------------------------------------------ sign-in

def _auth_manager(session):
    app = _app()
    return AuthenticationManager(session, app["client_id"], app["client_secret"], REDIRECT_URI)


def auth_url():
    """Microsoft sign-in URL; the state guards the callback against forgery."""
    _pending_state.update(value=secrets.token_urlsafe(24), at=time.time())

    async def go():
        async with SignedSession() as session:
            return _auth_manager(session).generate_authorization_url(state=_pending_state["value"])
    return asyncio.run(go())


def finish_sign_in(code, state):
    if not _pending_state["value"] or state != _pending_state["value"] or time.time() - _pending_state["at"] > 900:
        raise ValueError("This sign-in link has expired. Start again from /setup/xbox")
    _pending_state["value"] = None

    async def go():
        async with SignedSession() as session:
            mgr = _auth_manager(session)
            await mgr.request_tokens(code)
            _write_private(TOKENS_FILE, mgr.oauth.model_dump_json())
            consoles = await XboxLiveClient(mgr).smartglass.get_console_list()
            return [{"name": c.name, "type": c.console_type.value, "power": c.power_state.value,
                     "remote": c.remote_management_enabled} for c in consoles.result]
    return asyncio.run(go())


# ------------------------------------------------------------------ commands

async def _with_client(fn):
    if not signed_in():
        raise RuntimeError("the Xbox isn't linked yet. Open http://localhost/setup/xbox on the Mac")
    async with SignedSession() as session:
        mgr = _auth_manager(session)
        with open(TOKENS_FILE) as f:
            mgr.oauth = OAuth2TokenResponse.model_validate_json(f.read())
        await mgr.refresh_tokens()
        _write_private(TOKENS_FILE, mgr.oauth.model_dump_json())  # refresh tokens rotate
        client = XboxLiveClient(mgr)
        if not _console["id"]:
            consoles = (await client.smartglass.get_console_list()).result
            if not consoles:
                raise RuntimeError("no Xbox is linked to this Microsoft account")
            _console.update(id=consoles[0].id, name=consoles[0].name)
        return await fn(client.smartglass, _console["id"])


def _run(fn):
    return asyncio.run(_with_client(fn))


async def _command(sg, cid, kind, command, params=None):
    """Send a console command and read the reply as plain JSON.

    The library's CommandResponse model requires fields Microsoft no longer
    sends, so its own methods raise after the command has already worked.
    """
    body = {
        "destination": "Xbox", "type": kind, "command": command,
        "sessionId": sg._smartglass_session_id, "sourceId": "com.microsoft.smartglass",
        "parameters": params or [{}], "linkedXboxId": cid,
    }
    resp = await sg.client.session.post(f"{sg.SG_URL}/commands", json=body, headers=sg.HEADERS_SG)
    resp.raise_for_status()
    status = (resp.json().get("status") or {}).get("errorCode", "OK")
    if status != "OK":
        raise RuntimeError(f"the Xbox said {status}")
    return True


def _cmd(kind, command, params=None):
    return _run(lambda sg, cid: _command(sg, cid, kind, command, params))


def power_state():
    async def go(sg, cid):
        for c in (await sg.get_console_list()).result:
            if c.id == cid:
                return c.power_state.value
        return PowerState.Unknown.value
    return _run(go)


def wake():
    return _cmd("Power", "WakeUp")


def turn_off():
    return _cmd("Power", "TurnOff")


def button(name):
    return _cmd("Shell", "InjectKey", [{"keyType": BUTTONS[name.lower()].value}])


def home():
    return _cmd("Shell", "GoHome")


def back():
    return _cmd("Shell", "GoBack")


def media(action):
    """play, pause, next, previous, mute, unmute"""
    kind = "Audio" if action in ("mute", "unmute") else "Media"
    return _cmd(kind, action.capitalize())


def volume(up=True, amount=1):
    return _cmd("Audio", "Volume", [{"direction": "Up" if up else "Down", "amount": str(amount)}])


def installed_apps():
    if _apps["list"] is not None and time.time() - _apps["at"] < 600:
        return _apps["list"]

    # Read the raw list: Microsoft dropped fields (isGame, instanceId) that the
    # library's model still requires, so its parser rejects the response.
    async def go(sg, cid):
        resp = await sg._fetch_list("installedApps", {"deviceId": cid})
        return [{"name": a.get("name"), "id": a.get("oneStoreProductId"), "game": a.get("isGame")}
                for a in resp.json().get("result", []) if a.get("name") and a.get("oneStoreProductId")]
    _apps.update(list=_run(go), at=time.time())
    return _apps["list"]


def find_app(query):
    """The installed app best matching a spoken name, or None."""
    q = re.sub(r"^(the|my)\s+", "", query.lower().strip())
    q = ALIASES.get(q, q)
    apps = installed_apps()

    def norm(name):
        return re.sub(r"[^a-z0-9+ ]", "", name.lower())
    return (next((a for a in apps if norm(a["name"]) == q), None) or
            next((a for a in apps if norm(a["name"]).startswith(q)), None) or
            next((a for a in apps if q in norm(a["name"])), None))


def launch(query):
    """Open an installed game/app by (part of) its name. Returns the name to say."""
    match = find_app(query)
    if not match:
        raise RuntimeError(f"I couldn't find “{query}” among the Xbox's installed apps")
    _cmd("Shell", "ActivateApplicationWithOneStoreProductId", [{"oneStoreProductId": match["id"]}])
    return spoken_name(match["name"])


def spoken_name(name):
    """"EA SPORTS FC™ 24 Xbox Series X|S" -> "EA Sports FC 24": what you'd say out loud."""
    name = re.sub(r"[™®]", "", name)
    name = re.sub(r"\s*(for )?Xbox Series X\|S$|\s*\|\s*Xbox$|\s*Xbox - .*$|\s+Xbox$", "", name)
    return name.replace("EA SPORTS", "EA Sports").strip()


if __name__ == "__main__":
    print("configured:", configured(), "signed in:", signed_in())
    if signed_in():
        print("power:", power_state())
        print("apps:", [a["name"] for a in installed_apps()][:15])
