#!/usr/bin/env python3
"""
Download high-res renders, metadata, and local HTML previews for all fxhash
artworks collected (owned) by a user — excluding their own creations.

The --user flag accepts a fxhash username, a Tezos address (tz1…/KT1…), or
an EVM address (0x…). All linked wallets are discovered automatically, and
artworks authored by any of those wallets are excluded from the collected set.

Artworks are grouped by collection (issuer). Each collection gets its own
folder containing only the iterations the user holds.

Output layout:
  output/
    {username}/
      collection/
        {author}/
          {slug}/
            thumbnails/   ← PNG renders
            metadata/     ← per-iteration JSON + _collection.json
            html/         ← generative bundle + per-iteration redirect HTML

Usage:
  python3 download_collected.py --user artist
  python3 download_collected.py --user tz1xxx
  python3 download_collected.py --user 0xxxx
  python3 download_collected.py --user artist --no-images
  python3 download_collected.py --user artist --no-html
  python3 download_collected.py --user artist --output ./collected
"""

import argparse
import re
from pathlib import Path

from fxhash_lib import (
    PAGE_SIZE, gql_v2, resolve_account,
    _v2_token_to_v1, _v2_objkt_to_v1,
    resolve_author_name,
    process_images, process_html,
)


def _safe_name(name: str) -> str:
    """Convert an arbitrary display name to a safe folder name."""
    name = (name or "unknown").strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    return name or "unknown"


# ---------------------------------------------------------------------------
# Fetch collected objkts via V2 API (all chains: Tezos, Base, Ethereum, …)
# ---------------------------------------------------------------------------

_OBJKT_FIELDS = """
    id iteration generation_hash input_bytes features metadata rarity
    display_uri capture_media_id
    created_at assigned_at minted_price
    minter { id name }
    owner  { id name }
    generative_token {
        id name slug chain version
        supply iterations_count created_at
        generative_uri metadata tags capture_media_id
        pricing_fixeds         { price opens_at }
        pricing_dutch_auctions { levels opens_at }
        author { id name }
    }
"""


def fetch_objkts_page(wallet_addresses: list[str], skip: int) -> list[dict]:
    data = gql_v2("""
        query($addrs: [String!]!, $skip: Int!, $take: Int!) {
            onchain {
                objkt(
                    where: { owner_id: { _in: $addrs } }
                    offset: $skip
                    limit:  $take
                    order_by: { created_at: desc }
                ) {
    """ + _OBJKT_FIELDS + """
                }
            }
        }
    """, {"addrs": wallet_addresses, "skip": skip, "take": PAGE_SIZE})
    return data["onchain"]["objkt"]


def fetch_all_collected(wallet_addresses: list[str],
                        own_wallet_set: set[str]) -> dict[str, dict]:
    """
    Fetch all objkts owned by the account across all chains, filter out own
    creations, and return a dict keyed by issuer id:
        { issuer_id: {"token": <v1-normalised token>, "objkts": [<v1-normalised objkt>, ...]} }
    """
    groups: dict[str, dict] = {}
    skip = 0
    total_fetched = 0

    print("Fetching collected objkts (paginating)...")
    while True:
        batch = fetch_objkts_page(wallet_addresses, skip)
        if not batch:
            break
        for raw_objkt in batch:
            raw_token = raw_objkt.get("generative_token")
            if not raw_token:
                continue
            # Skip own creations
            author_id = (raw_token.get("author") or {}).get("id", "")
            if author_id.lower() in own_wallet_set:
                continue

            token  = _v2_token_to_v1(raw_token)
            objkt  = _v2_objkt_to_v1(raw_objkt)
            iid    = token["id"]
            if iid not in groups:
                groups[iid] = {"token": token, "objkts": []}
            groups[iid]["objkts"].append(objkt)

        total_fetched += len(batch)
        print(f"  {total_fetched} objkts fetched, {len(groups)} collections so far...", end="\r")
        if len(batch) < PAGE_SIZE:
            break
        skip += PAGE_SIZE

    print(f"  {total_fetched} objkts fetched total.                          ")
    return groups


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download fxhash collected artworks: renders, metadata, and HTML previews."
    )
    parser.add_argument(
        "--user", required=True,
        metavar="USER",
        help="fxhash username, Tezos address (tz1…/KT1…), or EVM address (0x…)",
    )
    parser.add_argument("--output",    default="./downloads", help="Root output directory")
    parser.add_argument("--workers",   type=int, default=4)
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--no-html",   action="store_true")
    args = parser.parse_args()

    account = resolve_account(args.user)
    username        = account["username"] or args.user
    wallets         = account.get("wallets") or []
    wallet_addresses = [w["address"] for w in wallets]
    own_wallet_set   = {w["address"].lower() for w in wallets}

    groups = fetch_all_collected(wallet_addresses, own_wallet_set)

    owned_total = sum(len(g["objkts"]) for g in groups.values())
    print(f"\nCollected {owned_total} iteration(s) across {len(groups)} collection(s):")
    for g in sorted(groups.values(), key=lambda g: g["token"]["name"]):
        t = g["token"]
        print(f"  [{t['id']}] {t['name']} by {resolve_author_name(t.get('author'))} "
              f"[{t.get('chain', '?')}] — {len(g['objkts'])} held / {t['objktsCount']} total")

    collection_dir = Path(args.output) / username / "collection"
    collection_dir.mkdir(parents=True, exist_ok=True)

    for g in sorted(groups.values(), key=lambda g: g["token"]["name"]):
        token  = g["token"]
        objkts = g["objkts"]

        author_name = resolve_author_name(token.get("author"))
        base_dir    = collection_dir / _safe_name(author_name)

        print(f"\n{'='*60}")
        print(f"Collection : {token['name']}  [{token.get('chain', '?')}  {token.get('version', '?')}]")
        print(f"By         : {author_name}")
        print(f"Held       : {len(objkts)} / {token['objktsCount']} total  (id={token['id']})")
        print(f"{'='*60}")

        master_extra = {
            "collectedCount":       len(objkts),
            "collectionTotalCount": token["objktsCount"],
        }

        if not args.no_images:
            process_images(token, objkts, base_dir, args.workers, master_extra=master_extra)
        if not args.no_html:
            process_html(token, objkts, base_dir)

    print("\nAll done!")


if __name__ == "__main__":
    main()
