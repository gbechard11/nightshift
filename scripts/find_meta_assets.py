#!/usr/bin/env python3
"""Discover the Meta ad accounts, Pages and pixels a profile's token can see.

Usage:
    python scripts/find_meta_assets.py            # default profile (Nightshift)
    python scripts/find_meta_assets.py @pawnshop  # a specific profile

Use this after a System User token is set for a new account (e.g. Pawn Shop Live)
to find the ids to pin in .env (META_<KEY>_AD_ACCOUNT_ID / _PAGE_ID / _PIXEL_ID).
Read-only — it never creates or changes anything.
"""
import asyncio
import os
import pathlib
import sys

# Load .env into the environment, then import meta_ads (which reads env at import).
ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
if ENV.exists():
    for ln in ENV.read_text().splitlines():
        s = ln.strip()
        if "=" in s and not s.startswith("#"):
            k, _, v = s.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import meta_ads  # noqa: E402


async def main() -> None:
    key = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        acct = meta_ads.get_profile(key)
    except meta_ads.MetaError as e:
        print(e)
        print("Known profiles:", ", ".join("@" + p.key for p in meta_ads.list_profiles()))
        return

    print(f"Profile @{acct.key} — {acct.label}  [{acct.currency}]")
    tok = f"set ({acct.token[:6]}…)" if acct.token else "MISSING"
    print(f"  token: {tok}")
    print(f"  pinned ad account: {acct.ad_account_id or '(none)'}")
    print(f"  pinned page: {acct.page_id or '(none)'}    pinned pixel: {acct.pixel_id or '(none)'}")
    if not acct.token:
        KU = acct.key.upper()
        print(f"\nNo token for this profile. Set META_{KU}_ACCESS_TOKEN in .env, then re-run.")
        return

    async with httpx.AsyncClient(timeout=45.0) as client:
        print("\n== Ad accounts this token can see ==")
        try:
            accts = await meta_ads.find_ad_accounts(client, acct=acct)
            if not accts:
                print("  (none — token has no ad accounts assigned)")
            for a in accts:
                print(f"  {a.get('id')}  {a.get('name')!r}  {a.get('currency')}  status={a.get('account_status')}")
        except meta_ads.MetaError as e:
            print(f"  ERROR: {e}")

        print("\n== Pages this token can see ==")
        try:
            pages = await meta_ads.find_pages(client, acct=acct)
            if not pages:
                print("  (none — token has no Pages assigned)")
            for p in pages:
                print(f"  {p.get('id')}  {p.get('name')!r}  ({p.get('category')})")
        except meta_ads.MetaError as e:
            print(f"  ERROR: {e}")

        if acct.ad_account_id:
            print(f"\n== Pixels on {acct.ad_account_id} ==")
            try:
                data = await meta_ads._get(
                    client, f"{acct.ad_account_id}/adspixels", {"fields": "id,name"}, token=acct.token
                )
                px = data.get("data", [])
                if not px:
                    print("  (none)")
                for p in px:
                    print(f"  {p.get('id')}  {p.get('name')!r}")
            except meta_ads.MetaError as e:
                print(f"  ERROR: {e}")

    KU = acct.key.upper()
    print(
        f"\nNext: pin the right ids in .env then restart nightshift.service:\n"
        f"  META_{KU}_AD_ACCOUNT_ID=act_...\n  META_{KU}_PAGE_ID=...\n  META_{KU}_PIXEL_ID=..."
    )


if __name__ == "__main__":
    asyncio.run(main())
