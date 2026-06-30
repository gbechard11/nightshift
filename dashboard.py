#!/usr/bin/env python3
"""Nightshift Marketing Team dashboard — staff web UI for Drops.

A password-protected web app where staff create drop pages, upload artwork,
watch signups roll in, prep blasts, and (optionally) spin up a paused Meta ad
for a drop. Served behind nginx at /marketingteam (Funnel :443).

  nginx location /marketingteam -> 127.0.0.1:8784 (this app, full path kept)

Auth is a single shared password (DROP_DASHBOARD_PW, default NS2026!!). The
staff dashboard is locked; the fan-facing drop pages it produces stay public.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drop_common as dc  # noqa: E402

from flask import (Blueprint, Flask, Response, abort, redirect, request,  # noqa: E402
                   render_template_string, send_file, session, url_for)

NIGHTSHIFT = dc.NIGHTSHIFT
PREFIX = "/marketingteam"


def _load_env():
    p = os.path.join(NIGHTSHIFT, ".env")
    if not os.path.exists(p):
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
PASSWORD = os.environ.get("DROP_DASHBOARD_PW", "NS2026!!")
BRANDS = ["Nightshift Entertainment", "Pawn Shop Live", "NS128", "Loud Sessions"]

bp = Blueprint("mt", __name__, url_prefix=PREFIX)

# --- layout ----------------------------------------------------------------

_BASE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{{title}} · Marketing Team</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;background:#0b0b0d;color:#ececec;font:15px/1.5 'Helvetica Neue',Arial,sans-serif}
 a{color:#fff}
 header{display:flex;align-items:center;justify-content:space-between;
  padding:16px 22px;border-bottom:1px solid #1c1c20;position:sticky;top:0;background:#0b0b0d;z-index:5}
 header .logo{font-weight:800;letter-spacing:.22em;text-transform:uppercase;font-size:13px}
 header nav a{margin-left:18px;color:#9a9aa2;text-decoration:none;font-size:13px;
  letter-spacing:.04em;text-transform:uppercase}
 header nav a:hover{color:#fff}
 .wrap{max-width:980px;margin:0 auto;padding:28px 22px 60px}
 h1{font-size:22px;letter-spacing:-.01em;margin:0 0 4px}
 .sub{color:#85858d;font-size:13px;margin:0 0 26px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
 .card{background:#131318;border:1px solid #20202a;border-radius:10px;overflow:hidden;
  display:flex;flex-direction:column}
 .card .art{aspect-ratio:16/9;background:#1a1a22 center/cover no-repeat;display:block}
 .card .body{padding:14px 15px;flex:1;display:flex;flex-direction:column;gap:6px}
 .card h3{margin:0;font-size:16px}
 .pill{display:inline-block;font-size:10px;letter-spacing:.12em;text-transform:uppercase;
  padding:3px 8px;border-radius:20px;font-weight:700}
 .pill.live{background:#1f5132;color:#7ff0a8}
 .pill.teaser{background:#3a3320;color:#f0d27f}
 .pill.closed{background:#3a2020;color:#f09a9a}
 .meta{color:#85858d;font-size:12.5px}
 .big{font-size:26px;font-weight:800;color:#fff}
 .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
 .btn{display:inline-block;padding:9px 14px;border-radius:7px;border:1px solid #2c2c38;
  background:#1b1b22;color:#fff;text-decoration:none;font-size:13px;cursor:pointer;font-weight:600}
 .btn:hover{border-color:#4a4a5a}
 .btn.primary{background:#fff;color:#0b0b0d;border-color:#fff}
 .btn.warn{background:#2a1d12;border-color:#5a3a1a;color:#f5b870}
 .btn.sm{padding:6px 10px;font-size:12px}
 form.stack{display:flex;flex-direction:column;gap:14px;max-width:560px}
 label{display:block;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
  color:#9a9aa2;margin-bottom:5px}
 input,select,textarea{width:100%;padding:11px 12px;background:#15151b;border:1px solid #2a2a34;
  border-radius:7px;color:#fff;font-size:14px;font-family:inherit}
 input:focus,select:focus,textarea:focus{outline:none;border-color:#6a6a80}
 textarea{min-height:80px;resize:vertical}
 .hint{color:#6f6f78;font-size:12px;margin-top:4px}
 .flash{padding:12px 15px;border-radius:8px;margin-bottom:18px;font-size:14px}
 .flash.ok{background:#11301d;color:#8ef0ad;border:1px solid #1f5132}
 .flash.err{background:#301414;color:#f0a0a0;border:1px solid #5a2020}
 .login{max-width:340px;margin:14vh auto 0;text-align:center}
 .login .logo{font-weight:800;letter-spacing:.26em;text-transform:uppercase;font-size:15px;margin-bottom:6px}
 .login p{color:#85858d;font-size:13px;margin:0 0 22px}
 code{background:#1a1a22;padding:2px 6px;border-radius:5px;font-size:13px}
 .splitcols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 @media(max-width:560px){.splitcols{grid-template-columns:1fr}}
</style></head><body>
{% if authed %}
<header>
 <span class=logo>Nightshift · Marketing</span>
 <nav>
   <a href="{{ url_for('mt.home') }}">Drops</a>
   <a href="{{ url_for('mt.new') }}">+ New Drop</a>
   <a href="{{ url_for('mt.logout') }}">Log out</a>
 </nav>
</header>
{% endif %}
<div class="{{ 'login' if not authed else 'wrap' }}">
{% if flash %}<div class="flash {{ flash_kind }}">{{ flash|safe }}</div>{% endif %}
{{ body|safe }}
</div></body></html>"""


def render(title, body, **kw):
    return render_template_string(
        _BASE, title=title, body=body, authed=bool(session.get("auth")),
        flash=session.pop("flash", None) if "flash" in session else kw.get("flash"),
        flash_kind=session.pop("flash_kind", "ok") if "flash_kind" in session else kw.get("flash_kind", "ok"),
        **kw,
    )


def flash(msg, kind="ok"):
    session["flash"] = msg
    session["flash_kind"] = kind


@bp.before_request
def _guard():
    if request.endpoint in ("mt.login", "mt.health"):
        return
    if not session.get("auth"):
        return redirect(url_for("mt.login"))


@bp.route("/health")
def health():
    return "ok"


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("auth"):
        return redirect(url_for("mt.home"))
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["auth"] = True
            session.permanent = True
            return redirect(url_for("mt.home"))
        body = _LOGIN.replace("{{err}}", '<p style="color:#f0a0a0">Wrong password.</p>')
        return render("Log in", body)
    return render("Log in", _LOGIN.replace("{{err}}", ""))


_LOGIN = """<div class=logo>Nightshift</div>
<p>Marketing Team dashboard</p>{{err}}
<form method=post class=stack>
 <input type=password name=password placeholder="Password" autofocus required>
 <button class="btn primary" type=submit>Enter</button>
</form>"""


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("mt.login"))


@bp.route("/")
def home():
    drops = dc.list_drops()
    cards = []
    for d in drops:
        art = dc.art_public_url(d["id"]) if dc.art_file(d["id"]) else (d.get("art_url") or "")
        style = f'style="background-image:url({art})"' if art else ""
        st = d.get("status", "teaser")
        cards.append(f"""
        <div class=card>
          <a class=art href="{url_for('mt.edit', drop_id=d['id'])}" {style}></a>
          <div class=body>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <h3>{_esc(d.get('title',''))}</h3>
              <span class="pill {st}">{st}</span>
            </div>
            <div class=meta>{_esc(d.get('subtitle',''))} · {_esc(d.get('venue_line',''))}</div>
            <div class=big>{dc.signup_count(d['id'])} <span class=meta>signups</span></div>
            <div class=row>
              <a class="btn sm" href="{url_for('mt.edit', drop_id=d['id'])}">Edit</a>
              <a class="btn sm" href="{dc.drop_url(d['id'])}" target=_blank>View</a>
              <a class="btn sm" href="{url_for('mt.signups', drop_id=d['id'])}">Signups</a>
              <a class="btn sm warn" href="{url_for('mt.boost_form', drop_id=d['id'])}">Boost</a>
            </div>
          </div>
        </div>""")
    if not cards:
        grid = ('<p class=meta>No drops yet. '
                f'<a href="{url_for("mt.new")}">Create your first drop →</a></p>')
    else:
        grid = '<div class=grid>' + "".join(cards) + '</div>'
    body = (f'<h1>Drops</h1><p class=sub>{len(drops)} total · live capture into your '
            f'email + SMS list</p>{grid}')
    return render("Drops", body)


def _esc(s):
    import html
    return html.escape(s or "")


_FORM_FIELDS = """
<div class=splitcols>
  <div><label>Title</label><input name=title value="{title}" required></div>
  <div><label>Subtitle (city / line 2)</label><input name=subtitle value="{subtitle}"></div>
</div>
<div class=splitcols>
  <div><label>Venue / date line</label><input name=venue_line value="{venue_line}" placeholder="Park Theatre — Sun Jul 12"></div>
  <div><label>City (for segmentation)</label><input name=city value="{city}"></div>
</div>
<div>
  <label>Brand</label>
  <select name=brand>{brand_opts}</select>
</div>
<div><label>Blurb</label><textarea name=blurb placeholder="Short hype paragraph">{blurb}</textarea></div>
<div class=splitcols>
  <div>
    <label>Status</label>
    <select name=status>{status_opts}</select>
  </div>
  <div><label>Button label (teaser mode)</label><input name=cta value="{cta}" placeholder="Notify Me"></div>
</div>
<div><label>Ticket URL (set + status=live → shows Get Tickets)</label>
  <input name=buy_url value="{buy_url}" placeholder="https://www.showpass.com/..."></div>
<div>
  <label>Artwork (flyer / photo)</label>
  <input type=file name=art accept="image/*">
  <div class=hint>{art_hint}</div>
</div>
"""


def _opts(values, current):
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == current else ""}>{_esc(v)}</option>'
        for v in values
    )


def _form_html(drop, action_url, submit_label):
    art_hint = "JPG/PNG/WEBP. Shows as the hero image on the drop page."
    if dc.art_file(drop["id"]) if drop.get("id") else None:
        art_hint = "Artwork uploaded ✓ — choose a new file to replace it."
    fields = _FORM_FIELDS.format(
        title=_esc(drop.get("title", "")), subtitle=_esc(drop.get("subtitle", "")),
        venue_line=_esc(drop.get("venue_line", "")), city=_esc(drop.get("city", "")),
        blurb=_esc(drop.get("blurb", "")), cta=_esc(drop.get("cta", "")),
        buy_url=_esc(drop.get("buy_url", "")),
        brand_opts=_opts(BRANDS, drop.get("brand", BRANDS[0])),
        status_opts=_opts(["teaser", "live", "closed"], drop.get("status", "teaser")),
        art_hint=art_hint,
    )
    return (f'<form class=stack method=post enctype="multipart/form-data" action="{action_url}">'
            f'{fields}<button class="btn primary" type=submit>{submit_label}</button></form>')


@bp.route("/new", methods=["GET", "POST"])
def new():
    if request.method == "POST":
        return _save(None)
    blank = {"id": "", "status": "teaser", "brand": BRANDS[0]}
    body = '<h1>New drop</h1><p class=sub>Builds a public 1-tap signup page.</p>' + \
           _form_html(blank, url_for("mt.new"), "Create drop")
    return render("New drop", body)


@bp.route("/edit/<drop_id>", methods=["GET", "POST"])
def edit(drop_id):
    drop = dc.load_drop(drop_id)
    if not drop:
        abort(404)
    if request.method == "POST":
        return _save(drop_id)
    link = dc.drop_url(drop_id)
    head = (f'<h1>Edit · {_esc(drop.get("title",""))}</h1>'
            f'<p class=sub>Public link: <a href="{link}" target=_blank>{link}</a> · '
            f'{dc.signup_count(drop_id)} signups</p>')
    body = head + _form_html(drop, url_for("mt.edit", drop_id=drop_id), "Save changes")
    return render("Edit drop", body)


def _save(drop_id):
    f = request.form
    title = (f.get("title") or "").strip()
    if not title:
        flash("Title is required.", "err")
        return redirect(request.url)
    if drop_id:
        drop = dc.load_drop(drop_id) or {}
    else:
        drop = {"id": dc.slugify(title), "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if dc.load_drop(drop["id"]):
            drop["id"] = f'{drop["id"]}-{int(time.time()) % 10000}'
    for k in ("title", "subtitle", "venue_line", "city", "brand", "blurb",
              "cta", "buy_url", "status"):
        drop[k] = (f.get(k) or "").strip()
    drop["title"] = title

    up = request.files.get("art")
    if up and up.filename:
        data = up.read()
        if data:
            drop["art_url"] = dc.save_art(drop["id"], data,
                                          content_type=up.mimetype or "",
                                          filename=up.filename)
    elif dc.art_file(drop["id"]):
        drop["art_url"] = dc.art_public_url(drop["id"])

    dc.save_drop(drop)
    flash(f'Saved. Public link: <a href="{dc.drop_url(drop["id"])}" target=_blank>'
          f'{dc.drop_url(drop["id"])}</a>')
    return redirect(url_for("mt.edit", drop_id=drop["id"]))


@bp.route("/signups/<drop_id>")
def signups(drop_id):
    drop = dc.load_drop(drop_id)
    if not drop:
        abort(404)
    if request.args.get("download"):
        path = dc._signups_path(drop_id)
        if not os.path.exists(path):
            abort(404)
        return send_file(path, as_attachment=True,
                         download_name=f"{drop_id}-signups.csv")
    import csv as _csv
    path = dc._signups_path(drop_id)
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
    trs = "".join(
        f"<tr><td>{_esc(r.get('Email',''))}</td><td>{_esc(r.get('Phone',''))}</td>"
        f"<td>{_esc(r.get('City',''))}</td><td class=meta>{_esc(r.get('ts',''))}</td></tr>"
        for r in reversed(rows[-200:])
    ) or '<tr><td colspan=4 class=meta>No signups yet.</td></tr>'
    table = (f'<table style="width:100%;border-collapse:collapse" cellpadding=8>'
             f'<tr style="text-align:left;color:#85858d;font-size:12px">'
             f'<th>Email</th><th>Phone</th><th>City</th><th>When</th></tr>{trs}</table>')
    body = (f'<h1>{_esc(drop.get("title",""))} · signups</h1>'
            f'<p class=sub>{len(rows)} total. These flow into your blast list automatically.</p>'
            f'<div class=row style="margin-bottom:16px">'
            f'<a class=btn href="{url_for("mt.signups", drop_id=drop_id)}?download=1">Download CSV</a>'
            f'<a class=btn href="{url_for("mt.edit", drop_id=drop_id)}">Back to drop</a></div>'
            f'{table}')
    return render("Signups", body)


_BOOST_FORM = """<h1>Boost · {title}</h1>
<p class=sub>Builds a <b>PAUSED</b>, spend-capped Meta campaign pointing at this
drop, using the uploaded artwork. Nothing spends until it's approved in Telegram
or Ads Manager.</p>
<form class=stack method=post action="{action}">
 <div class=splitcols>
   <div><label>Daily budget (CAD)</label><input name=daily type=number min=1 step=1 value="20" required></div>
   <div><label>Ad end date (auto-stops)</label><input name=end type=date value="{end}" required></div>
 </div>
 <div class=splitcols>
   <div><label>Ad account</label><select name=acct>{acct_opts}</select></div>
   <div><label>Objective</label><select name=objective>
     <option value=OUTCOME_TRAFFIC selected>Traffic (to drop page)</option>
     <option value=OUTCOME_ENGAGEMENT>Engagement</option>
   </select></div>
 </div>
 <div><label>Interest IDs (optional, comma-sep from /research)</label>
   <input name=interests placeholder="leave blank for broad"></div>
 <div><label>Caption</label><textarea name=caption>{caption}</textarea></div>
 <div class=hint>{art_note}</div>
 <button class="btn warn" type=submit>Create paused campaign</button>
</form>"""


@bp.route("/boost/<drop_id>", methods=["GET"])
def boost_form(drop_id):
    drop = dc.load_drop(drop_id)
    if not drop:
        abort(404)
    art_note = ("Artwork uploaded ✓ — it'll be the ad creative." if dc.art_file(drop_id)
                else "⚠ No artwork uploaded. Add one on the drop first for a real creative "
                     "(otherwise the ad uses the link preview).")
    accts = ["nightshift", "pawnshop"]
    body = _BOOST_FORM.format(
        title=_esc(drop.get("title", "")), action=url_for("mt.boost", drop_id=drop_id),
        end="", caption=_esc(drop.get("blurb", "") or drop.get("title", "")),
        acct_opts=_opts(accts, "nightshift"), art_note=art_note,
    )
    return render("Boost", body)


@bp.route("/boost/<drop_id>", methods=["POST"])
def boost(drop_id):
    drop = dc.load_drop(drop_id)
    if not drop:
        abort(404)
    f = request.form
    cmd = [os.path.join(NIGHTSHIFT, ".venv", "bin", "python"),
           os.path.join(NIGHTSHIFT, "drop_boost.py"),
           "--id", drop_id, "--daily", f.get("daily", "20"),
           "--end", f.get("end", ""), "--acct", f.get("acct", "nightshift"),
           "--objective", f.get("objective", "OUTCOME_TRAFFIC"),
           "--caption", f.get("caption", "") or drop.get("title", "")]
    if f.get("interests"):
        cmd += ["--interests", f["interests"]]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        res = (out.stdout or out.stderr).strip()
        ok = out.returncode == 0
    except Exception as e:
        res, ok = str(e), False
    if ok:
        flash(f"Paused campaign created. Approve it in Telegram (Pedro) or Ads "
              f"Manager before it spends.<br><code>{_esc(res[:600])}</code>")
    else:
        flash(f"Boost failed (nothing launched):<br><code>{_esc(res[:600])}</code>", "err")
    return redirect(url_for("mt.edit", drop_id=drop_id))


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("DROP_DASHBOARD_SECRET") or os.environ.get("UNSUB_SECRET", "ns-marketing-dev")
    app.permanent_session_lifetime = 60 * 60 * 12
    app.register_blueprint(bp)

    @app.route("/")
    def _root():
        return redirect(PREFIX + "/")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "8784"))
    app.run(host="127.0.0.1", port=port, threaded=True)
