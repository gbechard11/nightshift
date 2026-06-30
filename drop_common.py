"""Shared helpers for Nightshift Drops — the Laylo-style "drop page" system.

A *drop* is a hosted landing page for a show / release. A fan lands on it and
in one tap leaves their email + phone ("notify me when it drops" or "get
tickets"). Those opt-ins flow straight into the same contact lists that
scripts/blast.py broadcasts from — so a drop becomes a self-growing, owned
fan list instead of renting one from a third party.

This module is the single source of truth for the drop record format, the
signup files, and the public page HTML. It is imported by BOTH the CLI
(drop.py) and the public server (drop_server.py), exactly like unsub_common.py
backs both the unsubscribe sender and endpoint.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import threading
import time

NIGHTSHIFT = os.path.dirname(os.path.abspath(__file__))
DROPS_DIR = os.path.join(NIGHTSHIFT, "drops")
CONTACTS_DIR = os.path.join(os.path.dirname(NIGHTSHIFT), "..", "data", "greg", "contacts")
# Resolve the real contacts dir (VPS layout: /data/greg/contacts).
_CANON_CONTACTS = "/data/greg/contacts"
MASTER_SIGNUPS = os.path.join(
    _CANON_CONTACTS if os.path.isdir(_CANON_CONTACTS) else DROPS_DIR,
    "drop_signups.csv",
)
MASTER_HEADER = ["Email", "Name", "First", "Phone", "City", "Drop", "ts"]

# Public base — same Tailscale Funnel host the unsubscribe/click endpoints use.
# Drops are mounted under /d (Funnel strips the prefix; the backend sees "/").
BASE_URL = os.environ.get("DROP_BASE_URL") or os.environ.get(
    "UNSUB_BASE_URL", "https://nightshift-vps.tail6f5de5.ts.net"
)

_lock = threading.Lock()

_ID_RE = re.compile(r"[^a-z0-9-]+")


def slugify(text: str) -> str:
    s = _ID_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or "drop"


def drop_path(drop_id: str) -> str:
    return os.path.join(DROPS_DIR, f"{slugify(drop_id)}.json")


def drop_url(drop_id: str) -> str:
    return f"{BASE_URL}/d?id={slugify(drop_id)}"


def load_drop(drop_id: str) -> dict | None:
    p = drop_path(drop_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_drop(drop: dict) -> str:
    os.makedirs(DROPS_DIR, exist_ok=True)
    drop["id"] = slugify(drop["id"])
    p = drop_path(drop["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(drop, f, indent=2)
    os.replace(tmp, p)
    return p


def list_drops() -> list[dict]:
    if not os.path.isdir(DROPS_DIR):
        return []
    out = []
    for fn in os.listdir(DROPS_DIR):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(DROPS_DIR, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
            except Exception:
                pass
    out.sort(key=lambda d: d.get("created", ""), reverse=True)
    return out


# --- signups ---------------------------------------------------------------

def _signups_path(drop_id: str) -> str:
    return os.path.join(DROPS_DIR, f"{slugify(drop_id)}.signups.csv")


def _norm_phone(num: str) -> str:
    n = re.sub(r"[^\d+]", "", num or "")
    if not n:
        return ""
    # North-American convenience: 10 digits -> +1; 11 starting with 1 -> +.
    if not n.startswith("+"):
        if len(n) == 10:
            n = "+1" + n
        elif len(n) == 11 and n.startswith("1"):
            n = "+" + n
        else:
            n = "+" + n
    return n


def _valid_email(addr: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr or ""))


def already_signed(drop_id: str, email: str) -> bool:
    p = _signups_path(drop_id)
    if not os.path.exists(p):
        return False
    email = (email or "").strip().lower()
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("Email", "") or "").strip().lower() == email:
                return True
    return False


def add_signup(drop_id: str, email: str, phone: str = "", name: str = "",
               city: str = "") -> tuple[bool, str]:
    """Record a fan opt-in for a drop. Returns (ok, message).

    Writes to two places: the per-drop signups CSV (audit) and the master
    drop_signups.csv that blast.py can target. Deduped per drop by email.
    Opted-out addresses are silently honored (recorded as opt-out, not added
    to the sendable master)."""
    drop_id = slugify(drop_id)
    email = (email or "").strip().lower()
    phone = _norm_phone(phone)
    name = (name or "").strip()
    city = (city or "").strip()
    if not _valid_email(email) and not phone:
        return False, "Need a valid email or phone."

    # Respect existing opt-outs (don't re-add someone who unsubscribed).
    opted_out = False
    try:
        import unsub_common as uc  # type: ignore
        opted_out = bool(email) and uc.is_optout(email)
    except Exception:
        pass

    with _lock:
        if email and already_signed(drop_id, email):
            return True, "already"
        os.makedirs(DROPS_DIR, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        per = _signups_path(drop_id)
        new = not os.path.exists(per)
        with open(per, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["Email", "Name", "Phone", "City", "OptOut", "ts"])
            w.writerow([email, name, phone, city, "1" if opted_out else "", ts])

        # Master sendable list — skip opted-out so blasts never re-touch them.
        if not opted_out:
            os.makedirs(os.path.dirname(MASTER_SIGNUPS), exist_ok=True)
            mnew = not os.path.exists(MASTER_SIGNUPS)
            first = name.split(" ")[0] if name else ""
            with open(MASTER_SIGNUPS, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if mnew:
                    w.writerow(MASTER_HEADER)
                w.writerow([email, name, first, phone, city, drop_id, ts])
    return True, "added"


def signup_count(drop_id: str) -> int:
    p = _signups_path(drop_id)
    if not os.path.exists(p):
        return 0
    with open(p, newline="", encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)


# --- HTML page -------------------------------------------------------------

_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title} — {brand}</title>
<meta property="og:title" content="{title}">
<meta property="og:description" content="{ogdesc}">
{ogimg}
<meta name="theme-color" content="#0a0a0a">
<style>
 *{{box-sizing:border-box}}
 body{{margin:0;background:#0a0a0a;color:#fafafa;
  font-family:'Helvetica Neue',Arial,sans-serif;-webkit-font-smoothing:antialiased;
  min-height:100vh;display:flex;flex-direction:column}}
 a{{color:inherit}}
 .hero{{position:relative;width:100%;aspect-ratio:1/1;max-height:62vh;overflow:hidden;
  background:#161616}}
 .hero img{{width:100%;height:100%;object-fit:cover;display:block;filter:saturate(1.02)}}
 .wrap{{flex:1;width:100%;max-width:620px;margin:0 auto;padding:34px 24px 56px}}
 .kicker{{font-size:12px;letter-spacing:.32em;text-transform:uppercase;color:#7a7a7a;
  margin:0 0 14px}}
 h1{{font-size:clamp(34px,9vw,58px);line-height:.96;letter-spacing:-.01em;
  text-transform:uppercase;font-weight:800;margin:0 0 10px}}
 .sub{{font-size:15px;letter-spacing:.06em;text-transform:uppercase;color:#bdbdbd;
  margin:0 0 4px}}
 .venue{{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:#7a7a7a;
  margin:0 0 26px}}
 .blurb{{color:#cfcfcf;font-size:15px;line-height:1.6;margin:0 0 28px;max-width:48ch}}
 form{{display:flex;flex-direction:column;gap:11px;margin:0}}
 input{{width:100%;padding:16px 16px;background:#141414;border:1px solid #2a2a2a;
  border-radius:0;color:#fff;font-size:16px;outline:none}}
 input:focus{{border-color:#fff}}
 input::placeholder{{color:#6c6c6c}}
 button,.btn{{width:100%;padding:17px 16px;background:#fff;color:#0a0a0a;border:0;
  font-size:14px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;
  cursor:pointer;text-align:center;text-decoration:none;display:block}}
 button:active{{transform:translateY(1px)}}
 .btn-ghost{{background:transparent;color:#fff;border:1px solid #2f2f2f;margin-top:11px}}
 .hp{{position:absolute;left:-9999px}}
 .ok{{padding:22px 0}}
 .ok h2{{font-size:22px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.02em}}
 .ok p{{color:#9a9a9a;font-size:15px;margin:0;line-height:1.5}}
 .legal{{margin-top:22px;font-size:11px;line-height:1.5;color:#5e5e5e}}
 .brandbar{{padding:20px 24px;border-top:1px solid #1c1c1c;text-align:center;
  font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:#5e5e5e}}
</style></head><body>
{herohtml}
<div class=wrap>
 <p class=kicker>{kicker}</p>
 <h1>{title}</h1>
 {subhtml}
 {venuehtml}
 {blurbhtml}
 <div id=form>{actionhtml}</div>
 <p class=legal>{legal}</p>
</div>
<div class=brandbar>{brand}</div>
<script>
 var f=document.getElementById('signupform');
 if(f){{f.addEventListener('submit',function(e){{
   e.preventDefault();
   var b=f.querySelector('button'); b.disabled=true; b.textContent='…';
   fetch(location.pathname+location.search,{{method:'POST',
     headers:{{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}},
     body:new URLSearchParams(new FormData(f))}})
   .then(function(r){{return r.json()}})
   .then(function(j){{
     document.getElementById('form').innerHTML=
       '<div class=ok><h2>'+(j.title||"You're on the list")+'</h2><p>'+
       (j.msg||"We'll text and email you the moment it drops.")+'</p></div>';
   }})
   .catch(function(){{b.disabled=false;b.textContent='Try again';}});
 }});}}
</script>
</body></html>"""

_FORM = """<form id=signupform>
 <input class=hp tabindex=-1 autocomplete=off name=website placeholder="">
 <input type=email name=email placeholder="Email" required autocomplete=email>
 <input type=tel name=phone placeholder="Mobile (for text alerts)" autocomplete=tel>
 <button type=submit>{cta}</button>
</form>"""


def render_page(drop: dict) -> str:
    brand = html.escape(drop.get("brand") or "Nightshift Entertainment")
    title = html.escape(drop.get("title") or "Drop")
    sub = html.escape(drop.get("subtitle") or "")
    venue = html.escape(drop.get("venue_line") or "")
    blurb = html.escape(drop.get("blurb") or "")
    art = drop.get("art_url") or ""
    buy = drop.get("buy_url") or ""
    status = drop.get("status") or "teaser"
    kicker = html.escape(drop.get("kicker") or ("On sale now" if buy and status == "live" else "Coming soon"))

    herohtml = f'<div class=hero><img src="{html.escape(art)}" alt=""></div>' if art else ""
    subhtml = f'<p class=sub>{sub}</p>' if sub else ""
    venuehtml = f'<p class=venue>{venue}</p>' if venue else ""
    blurbhtml = f'<p class=blurb>{blurb}</p>' if blurb else ""
    ogimg = f'<meta property="og:image" content="{html.escape(art)}">' if art else ""
    ogdesc = html.escape(" · ".join(x for x in [sub, venue] if x) or brand)

    if buy and status == "live":
        # Tickets are live: lead with the buy button, still capture alerts.
        action = (
            f'<a class=btn href="{html.escape(buy)}" target=_blank rel=noopener>Get Tickets</a>'
            + _FORM.format(cta="Get Drop Alerts").replace("<button", '<button class=btn-ghost')
            .replace("<form id=signupform>", '<form id=signupform style="margin-top:11px">')
        )
        legal = ("By signing up you agree to receive email and text updates from "
                 f"{brand}. Msg &amp; data rates may apply. Reply STOP to opt out; "
                 "unsubscribe anytime.")
    else:
        action = _FORM.format(cta=html.escape(drop.get("cta") or "Notify Me"))
        legal = ("Drop your email and mobile to be first in line. We'll email and "
                 f"text you the second it drops. {brand} only — no spam, opt out "
                 "anytime. Msg &amp; data rates may apply.")

    return _PAGE.format(
        title=title, brand=brand, kicker=kicker, ogdesc=ogdesc, ogimg=ogimg,
        herohtml=herohtml, subhtml=subhtml, venuehtml=venuehtml,
        blurbhtml=blurbhtml, actionhtml=action, legal=legal,
    )
