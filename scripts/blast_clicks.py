#!/usr/bin/env python3
"""Click-through stats for a blast campaign.

Usage: blast_clicks.py <campaign>
Reads blast-clicks/<campaign>.jsonl (logged by click_server.py) and the
blast-ledger/<campaign>.jsonl delivered count to report total clicks, unique
clickers, click-through rate, and a breakdown by link.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # ~/nightshift
CLICK_DIR = os.path.join(ROOT, "blast-clicks")
LEDGER_DIR = os.path.join(ROOT, "blast-ledger")


def _load(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: blast_clicks.py <campaign>")
    camp = sys.argv[1]

    clicks = _load(os.path.join(CLICK_DIR, f"{camp}.jsonl"))
    uniq = {c.get("email", "").lower() for c in clicks if c.get("email")}

    delivered = {r["recipient"].lower() for r in _load(os.path.join(LEDGER_DIR, f"{camp}.jsonl"))
                 if r.get("ok") and r.get("channel") == "email" and r.get("recipient")}
    d = len(delivered)

    by_url = {}
    for c in clicks:
        by_url[c.get("url", "?")] = by_url.get(c.get("url", "?"), 0) + 1

    print(f"Campaign:        {camp}")
    print(f"Delivered:       {d}")
    print(f"Total clicks:    {len(clicks)}")
    ctr = f"  ({100 * len(uniq) / d:.1f}% click-through)" if d else ""
    print(f"Unique clickers: {len(uniq)}{ctr}")
    if by_url:
        print("By link:")
        for url, n in sorted(by_url.items(), key=lambda x: -x[1]):
            print(f"  {n:6d}  {url}")


if __name__ == "__main__":
    main()
