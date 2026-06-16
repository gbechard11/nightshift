"""One-off: build the Block Party 2026 Meta ad campaign for Gabe.

Campaign, ad set, and ad are already ACTIVE on the Nightshift account.
This script stages a fresh Launch button for Gabe pointing at the live campaign.
"""
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

import meta_ads
import pending_campaign

GABE_UID = 7682958654

CAMPAIGN_NAME = "Block Party 2026 | YEG RDR CGY"
TICKET_LINK = "https://showpass.com/blockparty2026"

LIFETIME_CAD = 250.0
END_TIME = "2026-06-20T21:00:00-06:00"  # 9PM MDT June 20

# Nightshift account — campaign, ad set, and ad are all ACTIVE and spending.
EXISTING_CAMPAIGN_ID = "120253266391680309"
EXISTING_ADSET_ID = "120253266468030309"
EXISTING_AD_ID = "120253266473190309"

AGE_MIN = 18
AGE_MAX = 40


def main():
    acct = meta_ads.get_profile("nightshift")
    print(f"Account: {acct.label} (@{acct.key})")

    campaign_id = EXISTING_CAMPAIGN_ID
    adset_id = EXISTING_ADSET_ID
    ad_id = EXISTING_AD_ID
    print(f"Using existing ACTIVE campaign {campaign_id} / ad set {adset_id} / ad {ad_id}")

    summary = "\n".join([
        "\U0001F7E2 Block Party 2026 Meta Ad — ACTIVE, spending now:",
        "",
        f"Account: {acct.label} (@{acct.key})",
        f"Campaign: {CAMPAIGN_NAME}",
        f"Campaign id: {campaign_id}",
        f"Creative: existing IG post instagram.com/p/DZoDE8Ysrhj/",
        f"Destination: {TICKET_LINK}",
        f"Budget: ${LIFETIME_CAD:.2f} CAD lifetime",
        f"Run: now → June 20 @ 9PM MDT",
        f"Geo: Edmonton, Red Deer, Calgary (+40km each)",
        f"Ages: {AGE_MIN}–{AGE_MAX}",
        f"Interests: EDM, House, Dubstep, Techno, Electronic Festivals",
        f"Ad set id: {adset_id}",
        f"Ad id: {ad_id}",
    ])

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
    main()
