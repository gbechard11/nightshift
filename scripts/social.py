#!/usr/bin/env python3
"""
Nightshift Social Media Manager

Generates, schedules, and posts content to Facebook + Instagram for
Nightshift Entertainment and Pawn Shop Live.

Commands:
  auto-schedule <slug>      Auto-generate full post series for a Showpass event
  generate <slug>           Generate post copy for an event (preview only)
  schedule                  Add a single post to the calendar
  calendar [--days N]       Show upcoming scheduled posts
  post-now <id>             Post a scheduled item immediately
  briefing                  Today's social media brief (for Telegram cron)
  auth-check                Check Meta API token permissions
  list                      List all pending/recent posts
  skip <id>                 Skip (cancel) a scheduled post

Usage:
  python scripts/social.py briefing
  python scripts/social.py auto-schedule blockparty2026 --account nightshift
  python scripts/social.py calendar --days 14
  python scripts/social.py post-now abc123
"""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import httpx

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
SOCIAL_DIR = pathlib.Path("/data/greg/social")
CALENDAR_FILE = SOCIAL_DIR / "calendar.json"
LEDGER_FILE = SOCIAL_DIR / "ledger.json"
CONFIG_FILE = SOCIAL_DIR / "config.json"

# ── Load .env ──────────────────────────────────────────────────────────────
if ENV_FILE.exists():
    for ln in ENV_FILE.read_text().splitlines():
        s = ln.strip()
        if "=" in s and not s.startswith("#"):
            k, _, v = s.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT))

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


# ── Config ─────────────────────────────────────────────────────────────────
def load_config():
    return json.loads(CONFIG_FILE.read_text())


def get_account(cfg, key):
    acct = cfg["accounts"].get(key)
    if not acct:
        sys.exit(f"Unknown account '{key}'. Known: {list(cfg['accounts'])}")
    token = os.environ.get(acct["token_env"], "")
    return acct, token


# ── Calendar / Ledger ──────────────────────────────────────────────────────
def load_calendar():
    if not CALENDAR_FILE.exists():
        return []
    return json.loads(CALENDAR_FILE.read_text())


def save_calendar(posts):
    CALENDAR_FILE.write_text(json.dumps(posts, indent=2, default=str))


def load_ledger():
    if not LEDGER_FILE.exists():
        return []
    return json.loads(LEDGER_FILE.read_text())


def save_ledger(ledger):
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2, default=str))


def make_id(event_slug, post_type, account):
    raw = f"{event_slug}:{post_type}:{account}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


# ── Content Templates ──────────────────────────────────────────────────────
TEMPLATES = {
    "announcement": [
        "🔊 JUST ANNOUNCED\n\n{artist} is coming to {venue} on {date_long}!\n\n{tagline}\n\n{cta}\n\n{hashtags}",
        "Big news 🎉\n\n{artist} — {venue}\n{date_short} | Doors {doors}\n\n{cta}\n\n{hashtags}",
        "Mark your calendars 📅\n\n{artist} live at {venue}\n{date_long}\n\n{tagline}\n\n{cta}\n\n{hashtags}",
    ],
    "countdown_7": [
        "7 DAYS AWAY 🔥\n\n{artist} hits {venue} one week from today.\n\nDon't sleep — {urgency}\n\n{cta}\n\n{hashtags}",
        "One week until {artist} 🎵\n\n{venue} | {date_short}\n\n{urgency} — link in bio.\n\n{hashtags}",
        "T-minus 7 days ⏳\n\n{artist} @ {venue}\n{date_short}\n\n{cta}\n\n{hashtags}",
    ],
    "countdown_3": [
        "3 DAYS 🚨\n\n{artist} at {venue} is almost here.\n\n{urgency}\n\n{cta}\n\n{hashtags}",
        "This weekend 👀\n\n{artist} — {venue}\n{date_short}\n\nTickets going fast. {cta}\n\n{hashtags}",
        "72 hours ⏰\n\n{artist} live at {venue}\n\n{urgency}\n{cta}\n\n{hashtags}",
    ],
    "day_before": [
        "TOMORROW NIGHT 🔥\n\n{artist} at {venue}\nDoors {doors}\n\nLast chance to grab tickets — {cta}\n\n{hashtags}",
        "See you tomorrow 👊\n\n{artist} — {venue}\nDoors {doors} | {date_short}\n\n{cta}\n\n{hashtags}",
        "Tomorrow. {venue}. {artist}. 🎶\n\nDoors {doors}. {urgency}\n\n{cta}\n\n{hashtags}",
    ],
    "day_of": [
        "TONIGHT ✨\n\n{artist} takes the stage at {venue}.\nDoors {doors}.\n\nSee you there! {cta}\n\n{hashtags}",
        "It's go time 🎤\n\n{artist} TONIGHT at {venue}\nDoors {doors}\n\nLimited tickets at the door — {cta}\n\n{hashtags}",
        "Tonight's the night 🔊\n\n{artist} live at {venue}\nDoors open {doors}\n\n{hashtags}",
    ],
    "post_show": [
        "What a night 🙌\n\nThanks to everyone who came out for {artist} at {venue}!\n\nFollow {artist_handle} for upcoming tour dates and new music. 🎵\n\n{hashtags}",
        "📸 Last night was unreal.\n\n{artist} brought everything at {venue}. Thanks to the whole crowd for making it special.\n\n{hashtags}",
        "That's a wrap on {artist} 🎶\n\nAmazing energy from the {venue} crowd last night. See you at the next one!\n\n{hashtags}",
    ],
}

URGENCY_PHRASES = [
    "tickets selling fast",
    "limited tickets remaining",
    "don't miss out",
    "almost sold out",
    "grab yours before they're gone",
]


def build_hashtags(acct_cfg, city=None, genre_tags=None):
    tags = list(acct_cfg["hashtags_base"])
    if city and city.lower() in ("edmonton", "yeg"):
        tags += acct_cfg.get("hashtags_edmonton", [])
    elif city and city.lower() in ("winnipeg", "ywg"):
        tags += acct_cfg.get("hashtags_winnipeg", [])
    if genre_tags:
        tags += genre_tags
    seen = set()
    deduped = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return " ".join(deduped)


def render_template(post_type, vars_dict, variant=0):
    templates = TEMPLATES.get(post_type, TEMPLATES["announcement"])
    tmpl = templates[variant % len(templates)]
    try:
        return tmpl.format(**vars_dict)
    except KeyError as e:
        return tmpl.replace("{" + str(e).strip("'") + "}", "")


def build_post_vars(event, acct_cfg, post_type):
    """Build template variables from event data."""
    # Parse date
    try:
        dt = datetime.fromisoformat(event.get("start", ""))
    except Exception:
        dt = None

    date_long = dt.strftime("%A, %B %-d, %Y") if dt else event.get("start", "")
    date_short = dt.strftime("%b %-d") if dt else event.get("start", "")
    doors = dt.strftime("%-I:%M %p") if dt else "TBA"

    cta = acct_cfg.get("ticket_link_cta", "Link in bio")
    ticket_url = event.get("ticket_url") or event.get("url", "")
    if ticket_url:
        cta = f"Tickets → {ticket_url}"

    city = event.get("city", "")
    hashtags = build_hashtags(
        acct_cfg,
        city=city,
        genre_tags=event.get("genre_tags", []),
    )

    artist = event.get("artist", event.get("name", ""))
    venue = event.get("venue", acct_cfg.get("label", ""))
    artist_handle = event.get("artist_handle", artist)
    tagline = event.get("tagline", f"Live at {venue}")

    import random
    urgency = random.choice(URGENCY_PHRASES)

    return {
        "artist": artist,
        "venue": venue,
        "date_long": date_long,
        "date_short": date_short,
        "doors": doors,
        "cta": cta,
        "hashtags": hashtags,
        "tagline": tagline,
        "urgency": urgency,
        "artist_handle": artist_handle,
    }


# ── Showpass integration ───────────────────────────────────────────────────
def fetch_showpass_event(slug):
    """Pull event data from Showpass public API."""
    venv_py = ROOT / ".venv/bin/python"
    showpass = ROOT / "showpass.py"
    try:
        result = subprocess.run(
            [str(venv_py), str(showpass), "event", slug, "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def showpass_to_event(sp_data, slug):
    """Normalize Showpass event data to our internal format."""
    if not sp_data:
        return None
    # Try local time first (has tz offset), then UTC
    start = (
        sp_data.get("local_starts_on")
        or sp_data.get("starts_on")
        or sp_data.get("start_date")
        or sp_data.get("starts_at")
        or sp_data.get("start", "")
    )
    venue_obj = sp_data.get("venue") or {}
    venue = venue_obj.get("name") or sp_data.get("venue_name", "")
    city = venue_obj.get("city") or sp_data.get("city", "")
    name = sp_data.get("name") or sp_data.get("title") or slug
    url = (
        sp_data.get("frontend_details_url")
        or sp_data.get("url")
        or f"https://showpass.com/{slug}/"
    )
    # Extract artist from "Event Name w/ ARTIST" or "ARTIST at Venue"
    artist = name
    if " w/ " in name:
        artist = name.split(" w/ ", 1)[1].split("(")[0].strip()
    elif " w/" in name:
        artist = name.split(" w/", 1)[1].strip()
    # Venue name override: Showpass venue is often the org, not the physical venue
    venue_display = venue if venue and venue != "Nightshift Entertainment" else "Pawn Shop Live"
    return {
        "slug": slug,
        "name": name,
        "artist": artist,
        "venue": venue_display,
        "city": city or "Edmonton",
        "start": start,
        "ticket_url": url,
        "genre_tags": [],
        "artist_handle": "",
        "tagline": "",
    }


# ── Meta Graph API posting ─────────────────────────────────────────────────
def check_permissions(token):
    """Return list of permissions the token has."""
    try:
        r = httpx.get(
            f"{GRAPH_BASE}/me/permissions",
            params={"access_token": token},
            timeout=15,
        )
        data = r.json()
        granted = [
            p["permission"] for p in data.get("data", [])
            if p.get("status") == "granted"
        ]
        return granted
    except Exception as e:
        return []


def can_post(token):
    perms = check_permissions(token)
    return "pages_manage_posts" in perms or "instagram_content_publish" in perms


def post_to_facebook(page_id, token, text, image_path=None):
    """Post text (+ optional image) to a Facebook page. Returns post ID or raises."""
    if image_path and pathlib.Path(image_path).exists():
        with open(image_path, "rb") as f:
            r = httpx.post(
                f"{GRAPH_BASE}/{page_id}/photos",
                params={"access_token": token},
                data={"caption": text, "published": "true"},
                files={"source": f},
                timeout=60,
            )
    else:
        r = httpx.post(
            f"{GRAPH_BASE}/{page_id}/feed",
            params={"access_token": token},
            data={"message": text},
            timeout=30,
        )
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", str(data["error"])))
    return data.get("id") or data.get("post_id")


def post_to_instagram(ig_account_id, token, text, image_url=None):
    """Post to Instagram Business account. Requires a public image URL.
    Returns IG media ID or raises."""
    if not ig_account_id:
        raise RuntimeError("No Instagram account ID configured for this account.")
    if not image_url:
        raise RuntimeError("Instagram posts require a public image URL.")

    # Step 1: Create media container
    r = httpx.post(
        f"{GRAPH_BASE}/{ig_account_id}/media",
        params={"access_token": token},
        data={"image_url": image_url, "caption": text},
        timeout=30,
    )
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", str(data["error"])))
    container_id = data.get("id")

    # Step 2: Publish
    r2 = httpx.post(
        f"{GRAPH_BASE}/{ig_account_id}/media_publish",
        params={"access_token": token},
        data={"creation_id": container_id},
        timeout=30,
    )
    data2 = r2.json()
    if "error" in data2:
        raise RuntimeError(data2["error"].get("message", str(data2["error"])))
    return data2.get("id")


# ── Commands ───────────────────────────────────────────────────────────────
def cmd_auth_check(args):
    cfg = load_config()
    print("=== Meta Token Permission Check ===\n")
    for key, acct_cfg in cfg["accounts"].items():
        token = os.environ.get(acct_cfg["token_env"], "")
        print(f"Account: {acct_cfg['label']} (@{key})")
        if not token:
            print("  ✗ No token found in env.\n")
            continue
        perms = check_permissions(token)
        if perms:
            print(f"  Permissions: {', '.join(perms)}")
        else:
            print("  Could not read permissions (may be System User token).")
        has_fb = "pages_manage_posts" in perms
        has_ig = "instagram_content_publish" in perms
        print(f"  FB posting: {'✓ READY' if has_fb else '✗ needs pages_manage_posts'}")
        print(f"  IG posting: {'✓ READY' if has_ig else '✗ needs instagram_content_publish'}")
        if not has_fb or not has_ig:
            print("  → To enable: Business Manager → System Users → [user] →")
            print("    Add Assets → Pages → assign 'Content creator' role, then")
            print("    regenerate token with pages_manage_posts + instagram_content_publish scopes.")
        print()


def cmd_generate(args):
    cfg = load_config()
    acct_cfg, token = get_account(cfg, args.account)

    slug = args.slug
    print(f"Fetching event: {slug} ...")
    sp = fetch_showpass_event(slug)
    event = showpass_to_event(sp, slug)

    if not event:
        # Manual fallback
        event = {
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "artist": slug.replace("-", " ").title(),
            "venue": acct_cfg.get("label", ""),
            "city": "Edmonton",
            "start": "",
            "ticket_url": f"https://showpass.com/{slug}/",
            "genre_tags": [],
            "artist_handle": "",
            "tagline": "",
        }
        print("(Showpass data not available — using slug as artist name)\n")

    print(f"\nEvent: {event['name']}")
    print(f"Venue: {event['venue']}  |  Date: {event.get('start','TBA')}")
    print(f"Tickets: {event.get('ticket_url','')}")
    print("\n" + "="*60 + "\n")

    for post_type in ["announcement", "countdown_7", "countdown_3", "day_before", "day_of", "post_show"]:
        vars_dict = build_post_vars(event, acct_cfg, post_type)
        text = render_template(post_type, vars_dict)
        print(f"── {post_type.upper().replace('_',' ')} ──")
        print(text)
        print()


def cmd_auto_schedule(args):
    cfg = load_config()
    acct_cfg, token = get_account(cfg, args.account)
    slug = args.slug

    print(f"Fetching event: {slug} ...")
    sp = fetch_showpass_event(slug)
    event = showpass_to_event(sp, slug)

    if not event:
        print(f"Could not fetch event '{slug}' from Showpass. Use --name, --date, --venue to fill in manually.")
        sys.exit(1)

    # Parse event date
    try:
        event_dt = datetime.fromisoformat(event["start"])
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
    except Exception:
        print(f"Could not parse event date: {event['start']!r}")
        sys.exit(1)

    now = datetime.now(tz=timezone.utc)
    calendar = load_calendar()

    # Map offset-days → post types
    series = {
        -21: "announcement",
        -7: "countdown_7",
        -3: "countdown_3",
        -1: "day_before",
        0: "day_of",
        1: "post_show",
    }

    post_times = cfg["posting_times"]
    created = []
    skipped_past = []

    platforms = args.platform.split(",") if args.platform else ["fb", "ig"]

    for offset_days, post_type in series.items():
        post_date = event_dt + timedelta(days=offset_days)

        if post_type == "day_of":
            h, m = 11, 0  # morning of show day
        elif post_type == "post_show":
            h, m = 10, 0  # morning after
        else:
            h, m = 19, 0  # evening

        post_dt = post_date.replace(hour=h, minute=m, second=0, microsecond=0)

        if post_dt < now and not args.include_past:
            skipped_past.append(post_type)
            continue

        for platform in platforms:
            post_id = make_id(slug, f"{post_type}_{platform}", args.account)
            # Skip if already in calendar
            if any(p["id"] == post_id for p in calendar):
                print(f"  Already scheduled: {post_type} ({platform})")
                continue

            vars_dict = build_post_vars(event, acct_cfg, post_type)
            text = render_template(post_type, vars_dict, variant=0)

            post = {
                "id": post_id,
                "event_slug": slug,
                "event_name": event["name"],
                "account": args.account,
                "platform": platform,
                "post_type": post_type,
                "scheduled_for": post_dt.isoformat(),
                "text": text,
                "image_path": None,
                "image_url": None,
                "ticket_url": event.get("ticket_url"),
                "status": "pending",
                "fb_post_id": None,
                "ig_post_id": None,
                "created_at": now.isoformat(),
                "posted_at": None,
            }
            calendar.append(post)
            created.append(post)

    save_calendar(calendar)

    if skipped_past:
        print(f"  Skipped (already past): {', '.join(skipped_past)}")
    print(f"\n✓ Scheduled {len(created)} posts for '{event['name']}':\n")
    for p in created:
        dt_str = p["scheduled_for"][:16].replace("T", " ")
        print(f"  [{p['id']}]  {dt_str}  {p['post_type']:<15}  {p['platform'].upper()}")

    print(f"\nPreview with: social.py calendar --days 90")
    print(f"Post manually: social.py post-now <id>")


def cmd_schedule(args):
    """Add a single post manually."""
    cfg = load_config()
    acct_cfg, _ = get_account(cfg, args.account)
    calendar = load_calendar()
    now = datetime.now(tz=timezone.utc)

    try:
        scheduled_for = datetime.fromisoformat(args.when)
    except Exception:
        sys.exit(f"Invalid --when: {args.when!r}. Use ISO format: 2026-06-20T19:00:00")

    post_id = make_id(args.event or "manual", args.post_type or "manual", args.account)

    post = {
        "id": post_id,
        "event_slug": args.event or "manual",
        "event_name": args.event or "Manual post",
        "account": args.account,
        "platform": args.platform,
        "post_type": args.post_type or "manual",
        "scheduled_for": scheduled_for.isoformat(),
        "text": args.text,
        "image_path": args.image,
        "image_url": args.image_url,
        "ticket_url": None,
        "status": "pending",
        "fb_post_id": None,
        "ig_post_id": None,
        "created_at": now.isoformat(),
        "posted_at": None,
    }
    calendar.append(post)
    save_calendar(calendar)
    print(f"✓ Scheduled [{post_id}] for {scheduled_for.strftime('%b %-d at %-I:%M %p')} ({args.platform.upper()})")


def cmd_calendar(args):
    calendar = load_calendar()
    days = args.days if args.days else 30
    now = datetime.now(tz=timezone.utc)
    cutoff = now + timedelta(days=days)

    upcoming = [
        p for p in calendar
        if p["status"] in ("pending", "failed")
        and datetime.fromisoformat(p["scheduled_for"]).replace(tzinfo=timezone.utc) <= cutoff
    ]
    upcoming.sort(key=lambda p: p["scheduled_for"])

    if not upcoming:
        print(f"No pending posts in the next {days} days.")
        return

    print(f"{'ID':<12} {'When':<18} {'Account':<12} {'Platform':<6} {'Type':<15} {'Event'}")
    print("-" * 85)
    for p in upcoming:
        dt = datetime.fromisoformat(p["scheduled_for"]).replace(tzinfo=timezone.utc)
        dt_str = dt.strftime("%b %-d  %-I:%M %p")
        past = " ⚠️" if dt < now else ""
        print(
            f"[{p['id']}]  {dt_str:<16}  {p['account']:<12} {p['platform'].upper():<6} "
            f"{p['post_type']:<15}  {p['event_name'][:30]}{past}"
        )


def cmd_list(args):
    calendar = load_calendar()
    status_filter = args.status if args.status else None

    posts = [p for p in calendar if not status_filter or p["status"] == status_filter]
    posts.sort(key=lambda p: p["scheduled_for"], reverse=True)

    for p in posts[:50]:
        dt = datetime.fromisoformat(p["scheduled_for"]).replace(tzinfo=timezone.utc)
        print(
            f"[{p['id']}]  {dt.strftime('%b %-d')}  {p['post_type']:<15}  "
            f"{p['platform'].upper():<4}  {p['status']:<8}  {p['event_name'][:35]}"
        )


def cmd_skip(args):
    calendar = load_calendar()
    for p in calendar:
        if p["id"] == args.id:
            p["status"] = "skipped"
            save_calendar(calendar)
            print(f"✓ Skipped [{args.id}]")
            return
    print(f"Post [{args.id}] not found.")


def cmd_post_now(args):
    cfg = load_config()
    calendar = load_calendar()
    ledger = load_ledger()
    now = datetime.now(tz=timezone.utc)

    post = next((p for p in calendar if p["id"] == args.id), None)
    if not post:
        sys.exit(f"Post [{args.id}] not found.")

    acct_cfg, token = get_account(cfg, post["account"])

    if not token:
        sys.exit("No access token configured for this account.")

    if not can_post(token):
        print("⚠️  Token lacks posting permissions.")
        print("\nPost copy for manual posting:\n")
        print("─" * 50)
        print(post["text"])
        print("─" * 50)
        if post.get("ticket_url"):
            print(f"\nTicket URL: {post['ticket_url']}")
        print("\nTo enable auto-posting: see 'social.py auth-check' for instructions.")
        return

    errors = []
    fb_id = None
    ig_id = None

    if post["platform"] in ("fb", "both"):
        try:
            fb_id = post_to_facebook(
                acct_cfg["fb_page_id"], token,
                post["text"], post.get("image_path"),
            )
            print(f"✓ Posted to Facebook: {fb_id}")
        except Exception as e:
            errors.append(f"FB: {e}")
            print(f"✗ Facebook error: {e}")

    if post["platform"] in ("ig", "both"):
        try:
            ig_id = post_to_instagram(
                acct_cfg.get("ig_account_id"), token,
                post["text"], post.get("image_url"),
            )
            print(f"✓ Posted to Instagram: {ig_id}")
        except Exception as e:
            errors.append(f"IG: {e}")
            print(f"✗ Instagram error: {e}")

    if fb_id or ig_id:
        post["status"] = "posted"
        post["posted_at"] = now.isoformat()
        post["fb_post_id"] = fb_id
        post["ig_post_id"] = ig_id
        ledger.append(post)
        save_ledger(ledger)
        # Remove from calendar
        calendar = [p for p in calendar if p["id"] != args.id]
        save_calendar(calendar)
    elif errors:
        post["status"] = "failed"
        save_calendar(calendar)


def cmd_briefing(args):
    """
    Generate today's social media brief — sent via Telegram cron each morning.
    Outputs text suitable for posting to Telegram.
    """
    calendar = load_calendar()
    now = datetime.now(tz=timezone.utc)

    # Posts due today (within next 24h and overdue by up to 48h)
    window_start = now - timedelta(hours=48)
    window_end = now + timedelta(hours=24)

    due = [
        p for p in calendar
        if p["status"] == "pending"
        and window_start <= datetime.fromisoformat(p["scheduled_for"]).replace(tzinfo=timezone.utc) <= window_end
    ]
    due.sort(key=lambda p: p["scheduled_for"])

    # Upcoming in next 7 days
    week_end = now + timedelta(days=7)
    upcoming = [
        p for p in calendar
        if p["status"] == "pending"
        and window_end < datetime.fromisoformat(p["scheduled_for"]).replace(tzinfo=timezone.utc) <= week_end
    ]

    lines = ["📱 *Social Media Brief*\n"]

    if not due and not upcoming:
        lines.append("Nothing scheduled for today or this week.")
        print("\n".join(lines))
        return

    if due:
        lines.append(f"*POST TODAY ({len(due)} posts):*")
        for p in due:
            dt = datetime.fromisoformat(p["scheduled_for"]).replace(tzinfo=timezone.utc)
            past_marker = " ⚠️ overdue" if dt < now else ""
            lines.append(
                f"\n[{p['id']}] {p['event_name']} — {p['post_type'].replace('_',' ').title()}"
                f" ({p['platform'].upper()}){past_marker}"
            )
            lines.append(f"Scheduled: {dt.strftime('%-I:%M %p UTC')}")
            lines.append("```")
            lines.append(p["text"][:400] + ("…" if len(p["text"]) > 400 else ""))
            lines.append("```")
            if p.get("ticket_url"):
                lines.append(f"🎟 {p['ticket_url']}")

    if upcoming:
        lines.append(f"\n*COMING UP THIS WEEK ({len(upcoming)}):*")
        for p in upcoming:
            dt = datetime.fromisoformat(p["scheduled_for"]).replace(tzinfo=timezone.utc)
            lines.append(
                f"  • {dt.strftime('%a %-d')} — {p['event_name']} / {p['post_type'].replace('_',' ')} ({p['platform'].upper()})"
            )

    lines.append(
        "\n_To post now: `social.py post-now <id>`_"
        "\n_To skip: `social.py skip <id>`_"
    )

    print("\n".join(lines))


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Nightshift Social Media Manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # auth-check
    sub.add_parser("auth-check", help="Check Meta token permissions")

    # generate
    p_gen = sub.add_parser("generate", help="Generate post copy for an event")
    p_gen.add_argument("slug", help="Showpass event slug")
    p_gen.add_argument("--account", default="nightshift", help="nightshift or pawnshop")

    # auto-schedule
    p_auto = sub.add_parser("auto-schedule", help="Auto-schedule full post series for an event")
    p_auto.add_argument("slug", help="Showpass event slug")
    p_auto.add_argument("--account", default="nightshift")
    p_auto.add_argument("--platform", default="fb,ig", help="fb,ig or just fb or ig")
    p_auto.add_argument("--include-past", action="store_true", help="Include already-past post dates")

    # schedule
    p_sched = sub.add_parser("schedule", help="Schedule a single post")
    p_sched.add_argument("--event", help="Event slug or name")
    p_sched.add_argument("--account", default="nightshift")
    p_sched.add_argument("--platform", default="both", choices=["fb", "ig", "both"])
    p_sched.add_argument("--post-type", default="manual")
    p_sched.add_argument("--text", required=True, help="Post body text")
    p_sched.add_argument("--when", required=True, help="ISO datetime: 2026-06-20T19:00:00")
    p_sched.add_argument("--image", help="Local image path")
    p_sched.add_argument("--image-url", help="Public image URL (for IG)")

    # calendar
    p_cal = sub.add_parser("calendar", help="Show upcoming scheduled posts")
    p_cal.add_argument("--days", type=int, default=30)

    # list
    p_list = sub.add_parser("list", help="List all posts")
    p_list.add_argument("--status", choices=["pending", "posted", "failed", "skipped"])

    # post-now
    p_post = sub.add_parser("post-now", help="Post a scheduled item immediately")
    p_post.add_argument("id", help="Post ID from calendar")

    # briefing
    sub.add_parser("briefing", help="Today's social media brief (for cron)")

    # skip
    p_skip = sub.add_parser("skip", help="Skip/cancel a scheduled post")
    p_skip.add_argument("id")

    args = parser.parse_args()

    dispatch = {
        "auth-check": cmd_auth_check,
        "generate": cmd_generate,
        "auto-schedule": cmd_auto_schedule,
        "schedule": cmd_schedule,
        "calendar": cmd_calendar,
        "list": cmd_list,
        "post-now": cmd_post_now,
        "briefing": cmd_briefing,
        "skip": cmd_skip,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
