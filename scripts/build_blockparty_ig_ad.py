"""One-off: build the Block Party 2026 Meta ad campaign for Gabe.

Uses the existing Nightshift IG post as creative, lifetime budget $250 CAD,
Edmonton/Red Deer/Calgary +25mi, June 16–20 at 9PM MDT.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Load .env before importing meta_ads so profile env vars are available.
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH) as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

import httpx
import meta_ads
import pending_campaign

GABE_UID = 7682958654

CAMPAIGN_NAME = "Block Party 2026 | YEG RDR CGY"
IG_POST_URL = "https://www.instagram.com/p/DZoDE8Ysrhj/?igsh=MWowaHNnazBscmU4cw=="
TICKET_LINK = "https://showpass.com/blockparty2026"

LIFETIME_CAD = 250.0
END_TIME = "2026-06-20T21:00:00-06:00"  # 9PM MDT June 20

# Use the Pawn Shop campaign (act_2437053100136886, ACTIVE) — Nightshift account is
# unsettled (billing). We still load the nightshift profile for its token + IG actor ID,
# then override the ad_account_id so the creative lands in the active account.
PAWNSHOP_AD_ACCOUNT_ID = "act_2437053100136886"
EXISTING_CAMPAIGN_ID = "120248795351400293"
EXISTING_ADSET_ID = "120248795352020293"

INTEREST_IDS = [
    "6003155409305",  # Electronic Dance Music (given)
    "6003479860669",  # House Music (given)
    "6003022971356",  # Dubstep
    "6002911345572",  # Techno
    "6808891387078",  # Electronic music festivals
]

CITIES_RAW = ["Edmonton", "Red Deer", "Calgary"]
RADIUS_KM = 40  # 25 miles ≈ 40 km

AGE_MIN = 18
AGE_MAX = 40


async def main():
    acct = meta_ads.get_profile("nightshift")
    # Billing on Nightshift is unsettled; redirect billing to the active Pawn Shop account.
    acct.ad_account_id = PAWNSHOP_AD_ACCOUNT_ID
    print(f"Account: {acct.label} (billing via {acct.ad_account_id})")
    print(f"IG actor: {acct.ig_actor_id}")

    media_id = meta_ads.ig_post_url_to_media_id(IG_POST_URL)
    print(f"IG media id: {media_id}")

    async with httpx.AsyncClient(timeout=300.0) as client:
        # 1. Resolve city geo keys
        print("\nResolving city geo keys...")
        city_specs, city_labels = [], []
        for city in CITIES_RAW:
            results = await meta_ads.search_locations(client, city, country_codes=["CA"], location_types=["city"], acct=acct)
            if results:
                hit = results[0]
                city_specs.append({"key": str(hit["key"]), "radius": RADIUS_KM, "distance_unit": "kilometer"})
                city_labels.append(f"{hit.get('name')}, {hit.get('region', '')} (+{RADIUS_KM}km)")
                print(f"  {city} → {hit.get('name')}, {hit.get('region')} (key {hit['key']})")
            else:
                print(f"  {city} → NOT FOUND")

        # 2. Build targeting
        targeting = meta_ads.build_targeting(
            interest_ids=INTEREST_IDS,
            cities=city_specs,
            age_min=AGE_MIN,
            age_max=AGE_MAX,
        )

        # 3. Reach estimate
        try:
            est = await meta_ads.reach_estimate(client, targeting, acct=acct)
            users = est.get("users") or est.get("estimate_mau")
            if isinstance(users, int):
                print(f"  Est. reach: ~{users:,}")
        except Exception as e:
            print(f"  (reach estimate skipped: {e})")
            est = None

        # 4 & 5. Use the already-created campaign + adset from prior run
        campaign_id = EXISTING_CAMPAIGN_ID
        adset_id = EXISTING_ADSET_ID
        print(f"\nUsing existing campaign {campaign_id} / ad set {adset_id}")

        # 6. Create creative from existing IG post
        creative_id = ad_id = creative_error = None
        print(f"\nCreating creative from IG post {media_id}...")
        try:
            creative = await meta_ads.create_adcreative_from_ig_post(
                client, f"{CAMPAIGN_NAME} — creative",
                ig_media_id=media_id,
                link=TICKET_LINK,
                call_to_action_type="GET_EVENT_TICKETS",
                acct=acct,
            )
            creative_id = creative.get("id")
            print(f"  Creative id: {creative_id}")
        except Exception as ce:
            creative_error = str(ce)
            print(f"  Creative FAILED: {creative_error}")

        if creative_id:
            print(f"\nCreating ad...")
            ad = await meta_ads.create_ad(client, adset_id, f"{CAMPAIGN_NAME} — ad", creative_id, acct=acct)
            ad_id = ad.get("id")
            print(f"  Ad id: {ad_id}")

    # 7. Stage + send Launch button to Gabe
    geo_label = ", ".join(city_labels) if city_labels else "CA"
    lines = [
        "\U0001F4CB Block Party 2026 Meta Ad — PAUSED, not spending:",
        "",
        f"Account: {acct.label} (@{acct.key})",
        f"Campaign: {CAMPAIGN_NAME}",
        f"Campaign id: {campaign_id}",
        f"Creative: existing IG post instagram.com/p/DZoDE8Ysrhj/ (media {media_id})",
        f"Destination: {TICKET_LINK}",
        f"Budget: ${LIFETIME_CAD:.2f} CAD lifetime",
        f"Run: now → {END_TIME} (June 20 @ 9PM MDT)",
        f"Geo: {geo_label}",
        f"Ages: {AGE_MIN}–{AGE_MAX}",
        f"Interests: EDM, House, Dubstep, Techno, Electronic Festivals",
        f"Ad set id: {adset_id}",
    ]
    if ad_id:
        lines.append(f"Ad id: {ad_id}")
    elif creative_error:
        lines.append(f"⚠️ Creative FAILED — {creative_error}")
        lines.append("Campaign + ad set are created. Set up the creative manually in Meta Ads Manager.")
    summary = "\n".join(lines)

    # Daily equivalent for display (4 days)
    daily_equiv = LIFETIME_CAD / 4

    token = pending_campaign.stage(
        GABE_UID, campaign_id, CAMPAIGN_NAME, daily_equiv, summary, acct_key=acct.key
    )
    rec = pending_campaign.load(token)
    ok = pending_campaign.send_confirm_prompt(rec)
    print(f"\n{'✅' if ok else '⚠️'} Launch button {'sent to Gabe' if ok else 'FAILED to send'}")
    print(f"Token: {token}")
    print(f"\nFull summary:\n{summary}")


if __name__ == "__main__":
    asyncio.run(main())
