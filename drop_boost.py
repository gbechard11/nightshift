#!/usr/bin/env python3
"""Build a PAUSED Meta campaign for a drop — the dashboard "Boost" button.

Reuses meta_ads.py (same paused-by-default guarantees as /draft) to stand up a
campaign + ad set + creative + ad that drives traffic to a drop's public page,
using the drop's uploaded artwork as the creative. Nothing ever goes live here:
approval still happens via Telegram (Pedro) or Ads Manager.

Per the standing rule, the ad set's end_time is REQUIRED and set to the show /
drop end date so spend auto-stops; ad_autostop.py is the backstop.

Usage (called by dashboard.py, also runnable by hand):
  drop_boost.py --id loud-sessions-wpg --daily 20 --end 2026-07-12 \
      --acct nightshift --objective OUTCOME_TRAFFIC --caption "..." [--interests 600..,600..]
Prints a JSON result.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drop_common as dc  # noqa: E402


def _load_env():
    p = os.path.join(dc.NIGHTSHIFT, ".env")
    if not os.path.exists(p):
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


async def _run(args) -> dict:
    import httpx
    import meta_ads

    drop = dc.load_drop(args.id)
    if not drop:
        return {"ok": False, "error": f"no drop '{args.id}'"}
    acct = meta_ads.get_profile(args.acct)
    if not (acct.token and acct.ad_account_id):
        return {"ok": False, "error": f"meta account '{args.acct}' not configured"}

    link = dc.drop_url(args.id)
    name = f"{drop.get('title', args.id)} — Drop"
    daily_cents = int(round(float(args.daily) * 100))
    if daily_cents <= 0:
        return {"ok": False, "error": "daily budget must be > 0"}
    # end_time is mandatory; Winnipeg/Calgary are -0500 in summer (CDT).
    end_time = f"{args.end}T23:59:00-0500" if args.end else None
    if not end_time:
        return {"ok": False, "error": "end date required (auto-stop rule)"}

    cta = ("GET_EVENT_TICKETS" if (drop.get("buy_url") and drop.get("status") == "live")
           else "LEARN_MORE")

    result = {"ok": True, "drop": args.id, "account": acct.key, "link": link,
              "status": "PAUSED", "end_time": end_time}
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Geo: target a specific city (radius km) when --city is given, so an
        # Edmonton brand never pays to reach all of Canada.
        cities = None
        if args.city:
            locs = await meta_ads.search_locations(
                client, args.city, country_codes=["CA"], location_types=["city"], acct=acct)
            if locs:
                key = locs[0].get("key")
                cities = [{"key": key, "radius": int(args.radius), "distance_unit": "kilometer"}]
                result["geo"] = f'{locs[0].get("name")}, {locs[0].get("region","")} +{args.radius}km'
            else:
                result["geo_warning"] = f"no city match for '{args.city}', fell back to CA-wide"

        # Interests: explicit --interests ids, else auto-resolve a music/nightlife set.
        interest_ids = [x.strip() for x in (args.interests or "").split(",") if x.strip()]
        if not interest_ids:
            terms = ["House music", "Electronic dance music", "Nightclub", "Music festival"]
            for t in terms:
                try:
                    hits = await meta_ads.search_interests(client, t, limit=1, acct=acct)
                    if hits:
                        interest_ids.append(str(hits[0]["id"]))
                except Exception:
                    pass
            result["interests"] = interest_ids

        targeting = (meta_ads.build_targeting(interest_ids, cities=cities) if cities
                     else meta_ads.build_targeting(interest_ids, countries=acct.default_countries))

        camp = await meta_ads.create_campaign(client, name, objective=args.objective, acct=acct)
        cid = camp.get("id")
        if not cid:
            return {"ok": False, "error": f"no campaign id: {camp}"}
        result["campaign_id"] = cid
        adset = await meta_ads.create_adset(
            client, cid, f"{name} — ad set", daily_cents, targeting,
            end_time=end_time, acct=acct,
        )
        asid = adset.get("id")
        result["adset_id"] = asid

        # Creative: explicit --image override, else the drop's uploaded artwork,
        # else the link's OG preview (the drop page already carries og:image).
        art = args.image if args.image else dc.art_file(args.id)
        image_hash = None
        try:
            if art:
                image_hash = await meta_ads.upload_ad_image(client, art, acct=acct)
            creative = await meta_ads.create_adcreative(
                client, f"{name} — creative", link, args.caption or drop.get("title", ""),
                image_hash=image_hash, call_to_action_type=cta, acct=acct,
            )
            crid = creative.get("id")
            result["creative_id"] = crid
            result["artwork"] = bool(image_hash)
            if crid and asid:
                ad = await meta_ads.create_ad(client, asid, f"{name} — ad", crid, acct=acct)
                result["ad_id"] = ad.get("id")
        except meta_ads.MetaError as ce:
            result["creative_warning"] = str(ce)

        try:
            est = await meta_ads.reach_estimate(client, targeting, acct=acct)
            result["reach_estimate"] = est
        except Exception:
            pass
    return result


def main():
    p = argparse.ArgumentParser(description="Build a paused Meta campaign for a drop")
    p.add_argument("--id", required=True)
    p.add_argument("--daily", required=True)
    p.add_argument("--end", required=True, help="YYYY-MM-DD auto-stop date")
    p.add_argument("--acct", default="nightshift")
    p.add_argument("--objective", default="OUTCOME_TRAFFIC")
    p.add_argument("--caption", default="")
    p.add_argument("--interests", default="")
    p.add_argument("--city", default="", help="target a city (e.g. Edmonton); CA-wide if omitted")
    p.add_argument("--radius", default="25", help="city radius in km (default 25)")
    p.add_argument("--image", default="", help="ad creative image path (overrides drop artwork)")
    args = p.parse_args()
    _load_env()
    try:
        res = asyncio.run(_run(args))
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res, indent=2))
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
