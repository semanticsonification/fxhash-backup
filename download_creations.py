#!/usr/bin/env python3
"""
Download high-res renders, metadata, and local HTML previews for all fxhash
collections created by a given user — across all chains (Tezos and Base).

The --user flag accepts a fxhash username, a Tezos address (tz1…/KT1…), or
an EVM address (0x…). All linked wallets are discovered automatically and
tokens authored by any of those wallets are included.

Output layout:
  output/
    {username}/
      creations/
        {slug}/
          thumbnails/   ← PNG renders
          metadata/     ← per-iteration JSON + _collection.json
          html/         ← generative bundle + per-iteration redirect HTML

Usage:
  python3 download_creations.py --user artist
  python3 download_creations.py --user tz1xxx
  python3 download_creations.py --user 0xxxx
  python3 download_creations.py --user artist --no-images
  python3 download_creations.py --user artist --no-html
  python3 download_creations.py --user artist --token-id 10345
  python3 download_creations.py --user artist --output ./downloads
"""

import argparse
import sys
from pathlib import Path

from fxhash_lib import (
    PAGE_SIZE, gql, gql_v2, resolve_account,
    _v2_token_to_v1, _v2_objkt_to_v1,
    process_images, process_html,
)

# ---------------------------------------------------------------------------
# Token fetching (V2 API — all chains)
# ---------------------------------------------------------------------------

_TOKEN_FIELDS = """
    id name slug version chain generative_uri
    supply iterations_count created_at
    metadata tags
    features
    capture_media_id
    author { id name }
    pricing_fixeds         { price opens_at }
    pricing_dutch_auctions { levels opens_at }
"""


def fetch_user_creations(identifier: str) -> tuple[dict, list[dict]]:
    """Resolve account and return all generative tokens via the V2 API."""
    account = resolve_account(identifier)
    wallet_addresses = [w["address"] for w in (account.get("wallets") or [])]

    data = gql_v2("""
        query($addrs: [String!]!) {
            onchain {
                generative_token(where: { author: { id: { _in: $addrs } } }) {
    """ + _TOKEN_FIELDS + """
                }
            }
        }
    """, {"addrs": wallet_addresses})

    raw_tokens = data["onchain"]["generative_token"]
    tokens = [_v2_token_to_v1(t) for t in raw_tokens]

    print(f"Found {len(tokens)} collection(s):")
    for t in sorted(tokens, key=lambda t: t.get("createdAt") or ""):
        print(f"  [{t['id']}] {t['name']} ({t['objktsCount']} items)"
              f" — {t['chain']}  [{t['version']}]")
    return account, tokens


# ---------------------------------------------------------------------------
# Objkt (iteration) fetching (V2 API)
# ---------------------------------------------------------------------------

_OBJKT_FIELDS = """
    id iteration generation_hash input_bytes features metadata rarity
    display_uri capture_media_id
    created_at assigned_at minted_price
    minter { id name }
    owner  { id name }
"""


def fetch_objkts_page(token_id: str, skip: int, take: int) -> list[dict]:
    data = gql_v2("""
        query($id: String!, $skip: Int!, $take: Int!) {
            onchain {
                objkt(
                    where: { issuer_id: { _eq: $id } }
                    offset: $skip
                    limit:  $take
                    order_by: { iteration: asc }
                ) {
    """ + _OBJKT_FIELDS + """
                }
            }
        }
    """, {"id": token_id, "skip": skip, "take": take})
    return [_v2_objkt_to_v1(o) for o in data["onchain"]["objkt"]]


def fetch_all_objkts(token: dict) -> list[dict]:
    total = token["objktsCount"]
    all_objkts, skip = [], 0
    while skip < total:
        batch = fetch_objkts_page(token["id"], skip, PAGE_SIZE)
        if not batch:
            break
        all_objkts.extend(batch)
        skip += len(batch)
        print(f"  Fetched {skip}/{total} iterations...", end="\r")
    print(f"  Fetched {len(all_objkts)} iterations.        ")
    return all_objkts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download fxhash creations: renders, metadata, and HTML previews."
    )
    parser.add_argument(
        "--user", required=True,
        metavar="USER",
        help="fxhash username, Tezos address (tz1…/KT1…), or EVM address (0x…)",
    )
    parser.add_argument("--output",    default="./downloads",
                        help="Root output directory")
    parser.add_argument("--workers",   type=int, default=4)
    parser.add_argument("--token-id",  default=None,
                        help="Process only this token ID or slug")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--no-html",   action="store_true")
    args = parser.parse_args()

    account, tokens = fetch_user_creations(args.user)
    username = account["username"] or args.user

    if args.token_id is not None:
        tokens = [t for t in tokens
                  if str(t["id"]) == args.token_id or t["slug"] == args.token_id]
        if not tokens:
            print(f"Token '{args.token_id}' not found for '{args.user}'.")
            sys.exit(1)

    base_dir = Path(args.output) / username / "creations"
    base_dir.mkdir(parents=True, exist_ok=True)

    for token in sorted(tokens, key=lambda t: t.get("createdAt") or ""):
        print(f"\n{'='*60}")
        print(f"Collection : {token['name']}  [{token['chain']}  {token['version']}]")
        print(f"ID         : {token['id']}")
        print(f"Iterations : {token['objktsCount']}")
        print(f"{'='*60}")

        objkts = fetch_all_objkts(token)

        if not args.no_images:
            process_images(token, objkts, base_dir, args.workers)
        if not args.no_html:
            process_html(token, objkts, base_dir)

    print("\nAll done!")


if __name__ == "__main__":
    main()
