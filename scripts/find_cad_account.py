#!/usr/bin/env python3
"""Discover the Nightshift CAD ad account id.

Run this once after putting META_ACCESS_TOKEN in .env. It lists every ad account
the token can see with its currency, then identifies the CAD one so you can pin
it as META_AD_ACCOUNT_ID.

    python scripts/find_cad_account.py

It only reads — it creates nothing and spends nothing.
"""
import asyncio
import os
import sys

# Allow running from the repo root or the scripts/ dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import meta_ads  # noqa: E402


async def main() -> int:
    if not meta_ads.configured():
        print("META_ACCESS_TOKEN is not set in the environment / .env.")
        print("Set a System User token with ads_management (or ads_read for read-only).")
        return 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            accounts = await meta_ads.find_ad_accounts(client)
        except meta_ads.MetaError as e:
            print(f"Failed to list ad accounts: {e}")
            return 1

    if not accounts:
        print("Token is valid but sees no ad accounts. Check the System User has")
        print("been assigned to the Nightshift ad account in Business Manager.")
        return 1

    print(f"Ad accounts visible to this token ({len(accounts)}):\n")
    for a in accounts:
        print(
            f"  {a.get('id'):<22} {a.get('currency','?'):<5} "
            f"status={a.get('account_status','?'):<3} {a.get('name','')}"
        )

    print()
    try:
        cad = meta_ads.pick_required_currency_account(accounts)
    except meta_ads.MetaError as e:
        print(f"Could not auto-pick a {meta_ads.REQUIRED_CURRENCY} account: {e}")
        print("Pick the right id from the list above and set it manually.")
        return 1

    print(f"==> {meta_ads.REQUIRED_CURRENCY} account: {cad.get('id')}  ({cad.get('name')})")
    print(f"\nPin it in .env:\n  META_AD_ACCOUNT_ID={cad.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
