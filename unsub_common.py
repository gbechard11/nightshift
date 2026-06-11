"""Shared unsubscribe helpers for Nightshift blasts.

Single source of truth for the signed-token format and the opt-out file, used
by BOTH the public unsubscribe endpoint (unsubscribe_server.py) and the sender
(scripts/blast.py). No database: the HMAC signature in the link IS the proof
that the clicker owns that address, so any email — even a forwarded/archived
one — unsubscribes correctly with zero per-send state.
"""
import base64
import hashlib
import hmac
import os
import threading

NIGHTSHIFT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(NIGHTSHIFT, ".env")
OPTOUT_EMAIL = os.path.join(NIGHTSHIFT, "blast-optout-email.txt")
# Public base (Tailscale Funnel :443 -> nginx -> /u). Overridable via env.
BASE_URL = os.environ.get("UNSUB_BASE_URL", "https://nightshift-vps.tail6f5de5.ts.net")

_lock = threading.Lock()


def _load_env() -> None:
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _secret() -> bytes:
    _load_env()
    s = os.environ.get("UNSUB_SECRET")
    if not s:
        raise RuntimeError("UNSUB_SECRET not set (add it to ~/nightshift/.env)")
    return s.encode()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(email: str) -> str:
    """Opaque, tamper-proof token encoding the (lowercased) email + HMAC sig."""
    email = email.strip().lower()
    sig = hmac.new(_secret(), email.encode(), hashlib.sha256).digest()[:9]
    return _b64u(email.encode()) + "." + _b64u(sig)


def verify_token(token: str):
    """Return the email if the token is valid, else None."""
    try:
        e_b64, s_b64 = token.split(".", 1)
        email = _unb64u(e_b64).decode().strip().lower()
        sig = _unb64u(s_b64)
    except Exception:
        return None
    expect = hmac.new(_secret(), email.encode(), hashlib.sha256).digest()[:9]
    return email if hmac.compare_digest(sig, expect) else None


def unsub_url(email: str) -> str:
    return f"{BASE_URL}/u?t={make_token(email)}"


# --- Click tracking ---------------------------------------------------------
# Same HMAC scheme as unsubscribe, but the token signs (email, campaign, dest
# url) together. Because the destination is part of the signed payload, the /c
# endpoint can ONLY redirect to a URL we minted — no open-redirect abuse.
_CLICK_SEP = "\x1f"  # unit separator; never appears in an email or campaign id


def make_click_token(email: str, campaign: str, url: str) -> str:
    email = email.strip().lower()
    payload = _CLICK_SEP.join([email, campaign, url]).encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()[:9]
    return _b64u(payload) + "." + _b64u(sig)


def verify_click_token(token: str):
    """Return (email, campaign, url) if the token is valid, else None."""
    try:
        p_b64, s_b64 = token.split(".", 1)
        payload = _unb64u(p_b64)
        sig = _unb64u(s_b64)
    except Exception:
        return None
    expect = hmac.new(_secret(), payload, hashlib.sha256).digest()[:9]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        email, campaign, url = payload.decode().split(_CLICK_SEP, 2)
    except Exception:
        return None
    return email, campaign, url


def click_url(email: str, campaign: str, dest_url: str) -> str:
    return f"{BASE_URL}/c?t={make_click_token(email, campaign, dest_url)}"


def is_optout(email: str) -> bool:
    email = email.strip().lower()
    if not os.path.exists(OPTOUT_EMAIL):
        return False
    with open(OPTOUT_EMAIL) as f:
        return any(line.strip().lower() == email for line in f)


def add_optout(email: str) -> bool:
    """Append email to the opt-out file (deduped, thread-safe). True if newly added."""
    email = email.strip().lower()
    if not email:
        return False
    with _lock:
        if is_optout(email):
            return False
        with open(OPTOUT_EMAIL, "a") as f:
            f.write(email + "\n")
    return True


def load_optout_set() -> set:
    """All opted-out addresses as a lowercased set."""
    s = set()
    if os.path.exists(OPTOUT_EMAIL):
        with open(OPTOUT_EMAIL) as f:
            for line in f:
                v = line.strip().lower()
                if v and not v.startswith("#"):
                    s.add(v)
    return s


def scrub_csv(path: str) -> int:
    """Remove rows containing any opted-out address from a customer CSV.

    Field-exact (a whole cell must equal an opt-out address) so a suffix like
    'im@x.com' never accidentally removes 'tim@x.com'. Only rewrites the file
    when something is actually removed. Returns the row count removed.
    """
    import csv as _csv
    opt = load_optout_set()
    if not opt or not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(_csv.reader(f))
    if not rows:
        return 0
    header, body = rows[0], rows[1:]
    kept = [r for r in body
            if not any((c or "").strip().lower() in opt for c in r)]
    removed = len(body) - len(kept)
    if removed:
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(header)
            w.writerows(kept)
        os.replace(tmp, path)
    return removed
