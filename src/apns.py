"""Direct APNs client for Live Activity updates.

Expo's push service delivers ordinary notifications but cannot address Live
Activities, so this module talks to Apple directly over HTTP/2 using the
"MXT Live Activity" APNs auth key. Credentials come from the environment:

    APNS_KEY_B64   base64 of the AuthKey .p8 file contents
    APNS_KEY_ID    10-char key id from the developer portal
    APNS_TEAM_ID   Apple developer team id

Without them every send becomes a quiet no-op, so the API runs fine in
environments where Live Activities aren't configured.
"""

import base64
import json
import logging
import os
import time

log = logging.getLogger("moto.apns")

_BUNDLE_ID = "com.mitchfisch.mototracker"
_LA_TOPIC = _BUNDLE_ID + ".push-type.liveactivity"
_APNS_HOST = "https://api.push.apple.com"

_jwt_cache = {"token": None, "minted": 0.0}


def _credentials():
    b64 = os.environ.get("APNS_KEY_B64")
    key_id = os.environ.get("APNS_KEY_ID")
    team_id = os.environ.get("APNS_TEAM_ID")
    if not (b64 and key_id and team_id):
        return None
    try:
        return base64.b64decode(b64).decode(), key_id, team_id
    except Exception:
        log.warning("APNS_KEY_B64 is not valid base64")
        return None


def apns_ready() -> bool:
    return _credentials() is not None


def _auth_token():
    """Mint (or reuse) the ES256 provider JWT — Apple allows one per ~20-60 min."""
    creds = _credentials()
    if not creds:
        return None
    if _jwt_cache["token"] and time.time() - _jwt_cache["minted"] < 45 * 60:
        return _jwt_cache["token"]
    import jwt  # PyJWT
    key, key_id, team_id = creds
    token = jwt.encode({"iss": team_id, "iat": int(time.time())}, key,
                       algorithm="ES256", headers={"kid": key_id})
    _jwt_cache.update(token=token, minted=time.time())
    return token


def send_live_activity(token: str, event: str, content_state: dict,
                       alert: dict | None = None, client=None):
    """Send one Live Activity push. event: 'update' | 'end' | 'start'.

    Returns (ok, reason). reason 'BadDeviceToken'/'Unregistered' means the
    token is stale and should be pruned.
    """
    auth = _auth_token()
    if not auth:
        return False, "not configured"
    import httpx
    payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": event,
            "content-state": content_state,
        }
    }
    if event == "end":
        # Remove from the lock screen immediately instead of Apple's default
        # of leaving the final state visible for up to 4 hours.
        payload["aps"]["dismissal-date"] = int(time.time())
    if event == "start":
        payload["aps"]["attributes-type"] = "MXTRaceAttributes"
        payload["aps"]["attributes"] = {}
        payload["aps"]["alert"] = alert or {
            "title": "MXT Live Timing",
            "body": "The race is on — running order on your lock screen.",
        }
    headers = {
        "authorization": f"bearer {auth}",
        "apns-topic": _LA_TOPIC,
        "apns-push-type": "liveactivity",
        "apns-priority": "10",
    }
    own_client = client is None
    if own_client:
        client = httpx.Client(http2=True, timeout=15)
    try:
        r = client.post(f"{_APNS_HOST}/3/device/{token}",
                        headers=headers, json=payload)
        if r.status_code == 200:
            return True, "ok"
        try:
            reason = r.json().get("reason", str(r.status_code))
        except json.JSONDecodeError:
            reason = str(r.status_code)
        return False, reason
    except Exception as e:  # network hiccup — try again next tick
        return False, str(e)
    finally:
        if own_client:
            client.close()
