"""Build Block Party 2026 - Adventure Club Meta video ad.

Campaign + ad set already exist (PAUSED) on Nightshift account.
This script:
1. Uploads the converted Meta-ready video
2. Updates ad set targeting (Edmonton/Red Deer/Calgary +25mi, ages 18-40)
3. Sets ad set end time (3 weeks)
4. Creates video creative + PAUSED ad
5. Stages Gabe's launch button
"""
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path("/home/gregnightshift/nightshift")
for ln in (ROOT / ".env").read_text().splitlines():
    s = ln.strip()
    if "=" in s and not s.startswith("#"):
        k, _, v = s.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"'))
sys.path.insert(0, str(ROOT))

import httpx
import meta_ads
import pending_campaign

GABE_UID = 7682958654

CAMPAIGN_ID = "120253267221260309"
ADSET_ID = "120253267222000309"
CAMPAIGN_NAME = "Block Party 2026 - Adventure Club"
TICKET_LINK = "https://showpass.com/blockparty2026"

VIDEO_PATH = "/data/greg/adventure_club_ads/adventure_club_meta.mp4"

CAPTION = (
    "\U0001f3b6 Adventure Club hits the Pawn Shop Live Outdoor Stage this summer! "
    "☀️ Day 2 — Sun Aug 23 · Don’t sleep on this one \U0001f525 "
    "Grab your tickets now \U0001f39f️"
)

DAILY_CENTS = 1905  # $19.05 CAD
END_TIME = "2026-07-07T23:59:59-06:00"  # 3 weeks from June 16

AGE_MIN = 18
AGE_MAX = 40

INTEREST_IDS = [
    "6003155409305",  # Electronic dance music (EDM)
    "6003479860669",  # House music
]

# Edmonton key=293225, Red Deer key=295768, Calgary key=292501
CITIES = [
    {"key": "293225", "radius": 25, "distance_unit": "mile"},  # Edmonton
    {"key": "295768", "radius": 25, "distance_unit": "mile"},  # Red Deer
    {"key": "292501", "radius": 25, "distance_unit": "mile"},  # Calgary
]


async def main():
    acct = meta_ads.get_profile("nightshift")
    base = meta_ads._base()
    tok = acct.token
    print(f"Account: {acct.label} (@{acct.key}) [{acct.ad_account_id}]")

    async with httpx.AsyncClient(timeout=300) as client:

        # --- 1. Update ad set targeting + age + end time ---
        print("\n1. Updating ad set targeting ...", flush=True)
        targeting = meta_ads.build_targeting(
            interest_ids=INTEREST_IDS,
            cities=CITIES,
            age_min=AGE_MIN,
            age_max=AGE_MAX,
        )
        upd = await client.post(f"{base}/{ADSET_ID}", data={
            "access_token": tok,
            "targeting": json.dumps(targeting),
            "end_time": END_TIME,
        })
        result = meta_ads._handle(upd)
        print(f"   Ad set update: {result}", flush=True)

        # --- 2. Upload converted video to Meta ---
        print(f"\n2. Uploading video {VIDEO_PATH} ...", flush=True)
        video_id = await meta_ads.upload_ad_video(client, VIDEO_PATH, acct=acct)
        print(f"   Video id: {video_id}", flush=True)

        # --- 3. Wait for Meta to process video ---
        print("\n3. Waiting for Meta to process video ...", flush=True)
        ready = await meta_ads.wait_for_video(client, video_id, acct=acct, timeout=180)
        print(f"   Video ready: {ready}", flush=True)

        # --- 4. Create video creative ---
        print("\n4. Creating video creative ...", flush=True)
        creative = await meta_ads.create_adcreative_video(
            client,
            name=f"{CAMPAIGN_NAME} — video creative",
            link=TICKET_LINK,
            caption=CAPTION,
            video_id=video_id,
            call_to_action_type="GET_EVENT_TICKETS",
            acct=acct,
        )
        creative_id = creative["id"]
        print(f"   Creative id: {creative_id}", flush=True)

        # --- 5. Create PAUSED ad ---
        print("\n5. Creating PAUSED ad ...", flush=True)
        ad = await meta_ads.create_ad(
            client,
            adset_id=ADSET_ID,
            name=f"{CAMPAIGN_NAME} — ad",
            creative_id=creative_id,
            acct=acct,
        )
        ad_id = ad["id"]
        print(f"   Ad id: {ad_id}", flush=True)

    # --- 6. Stage launch button for Gabe ---
    summary = "\n".join([
        "\U0001f7e1 Block Party 2026 — Adventure Club Meta Ad (PAUSED, ready to launch):",
        "",
        f"Account: {acct.label} (@{acct.key})",
        f"Campaign: {CAMPAIGN_NAME}",
        f"Campaign id: {CAMPAIGN_ID}",
        f"Ad set id: {ADSET_ID}",
        f"Ad id: {ad_id}",
        f"Creative id: {creative_id}",
        f"Video id: {video_id}",
        f"Destination: {TICKET_LINK}",
        f"Budget: ${DAILY_CENTS / 100:.2f} CAD/day (~$400 over 3 weeks)",
        f"Run: now → July 7 @ midnight MDT",
        f"Geo: Edmonton, Red Deer, Calgary (+25 miles each)",
        f"Ages: {AGE_MIN}–{AGE_MAX}",
        f"Interests: EDM, House Music",
        f"Creative: video ad (converted H.264 High L4.0)",
    ])

    daily_display = DAILY_CENTS / 100
    token = pending_campaign.stage(
        GABE_UID, CAMPAIGN_ID, CAMPAIGN_NAME, daily_display, summary, acct_key=acct.key
    )
    rec = pending_campaign.load(token)
    ok = pending_campaign.send_confirm_prompt(rec)
    print(f"\n{'✅' if ok else '⚠️'} Launch button {'sent to Gabe' if ok else 'FAILED to send'}")
    print(f"Token: {token}")
    print(f"\nFull summary:\n{summary}")


if __name__ == "__main__":
    asyncio.run(main())
