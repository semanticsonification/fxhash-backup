"""
Shared utilities for fxhash download scripts.
"""

import io
import json
import re
import tarfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

GRAPHQL_URL    = "https://api.fxhash.xyz/graphql"       # V1 — PRE_V3 Tezos
GRAPHQL_V2_URL = "https://api.v2.fxhash.xyz/v1/graphql"  # V2 — all chains
IPFS_GATEWAY   = "https://gateway.fxhash.xyz/ipfs"
IPFS_FALLBACK  = "https://ipfs.io/ipfs"   # used for tar bundle downloads
PAGE_SIZE = 50

ACCOUNT_FIELDS = """
    id username
    wallets { address network }
"""


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

def resolve_account(identifier: str) -> dict:
    """Resolve any fxhash identifier (username, Tezos address, EVM address)
    to an account dict with id, username, and all linked wallets.
    Exits with a clear error if the account is not found.
    """
    import sys
    data = gql(
        f"query($id: String!) {{ account(usernameOrAddress: $id) {{ {ACCOUNT_FIELDS} }} }}",
        {"id": identifier},
    )
    account = data.get("account")
    if not account:
        print(f"Account '{identifier}' not found.")
        sys.exit(1)
    wallet_strs = ", ".join(
        f"{w['address']} ({w['network']})" for w in (account.get("wallets") or [])
    )
    print(f"Account : {account['username'] or identifier}")
    print(f"Wallets : {wallet_strs or 'none'}")
    return account


def gql_v2(query: str, variables: dict = None) -> dict:
    """Query the V2 Hasura API (all chains: Tezos + Base/EVM)."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(5):
        try:
            r = requests.post(GRAPHQL_V2_URL, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except Exception as e:
            if attempt == 4:
                raise
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/5 in {wait}s] {e}")
            time.sleep(wait)


def gql(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(5):
        try:
            r = requests.post(GRAPHQL_URL, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except Exception as e:
            if attempt == 4:
                raise
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/5 in {wait}s] {e}")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# V2 → V1 normalisation
# ---------------------------------------------------------------------------

def _v2_token_to_v1(t: dict) -> dict:
    """Normalise a V2 generative_token record to the V1 shape used throughout."""
    meta = t.get("metadata") or {}
    author = t.get("author") or {}
    pf_list = t.get("pricing_fixeds") or []
    da_list = t.get("pricing_dutch_auctions") or []
    pf_raw  = pf_list[0] if pf_list else None
    da_raw  = da_list[0] if da_list else None
    pf = {"price": pf_raw.get("price"), "opensAt": pf_raw.get("opens_at")} if pf_raw else None
    da = {"levels": da_raw.get("levels", []), "opensAt": da_raw.get("opens_at")} if da_raw else None
    return {
        "id":            t["id"],
        "name":          t["name"],
        "slug":          t.get("slug") or "",
        "version":       t.get("version", ""),
        "chain":         t.get("chain", ""),
        "generativeUri": t.get("generative_uri"),
        "objktsCount":   int(t.get("iterations_count") or 0),
        "supply":        int(t.get("supply") or 0) or None,
        "createdAt":     t.get("created_at"),
        "metadata":      meta,
        "tags":          t.get("tags") or [],
        "captureMedia":  {"cid": t.get("capture_media_id")},
        "author":        {"id": author.get("id"), "name": author.get("name")},
        "pricingFixed":         pf,
        "pricingDutchAuction":  da,
        "features":      t.get("features") or [],
    }


def _v2_objkt_to_v1(o: dict) -> dict:
    """Normalise a V2 objkt record to the V1 shape used throughout."""
    minter = o.get("minter") or {}
    owner  = o.get("owner")  or {}
    return {
        "id":             o.get("id"),
        "iteration":      int(o.get("iteration") or 0),
        "generationHash": o.get("generation_hash"),
        "inputBytes":     o.get("input_bytes"),
        "features":       o.get("features") or [],
        "metadata":       o.get("metadata") or {},
        "rarity":         o.get("rarity"),
        "displayUri":     o.get("display_uri"),
        "captureMedia":   {"cid": o.get("capture_media_id")},
        "createdAt":      o.get("created_at"),
        "assignedAt":     o.get("assigned_at"),
        "mintedPrice":    o.get("minted_price"),
        "minter":         {"id": minter.get("id"), "name": minter.get("name")},
        "owner":          {"id": owner.get("id"),  "name": owner.get("name")},
    }


# ---------------------------------------------------------------------------
# Author name resolution (handles KT1 collab contracts)
# ---------------------------------------------------------------------------

_collab_cache: dict[str, str] = {}


def resolve_author_name(author: dict) -> str:
    """Return a human-readable author name.

    - For KT1 collab contracts: fetches collaborator names via the V1 API and
      returns e.g. "collab: Artist 1 - Artist 2".
    - For any other address with no name (tz1…, 0x…): resolves to a fxhash
      username via the V1 account endpoint.

    Results are cached so repeated tokens from the same address don't cause
    extra API calls.
    """
    name = (author or {}).get("name")
    if name:
        return name

    author_id = (author or {}).get("id") or ""
    if not author_id:
        return "unknown"

    if author_id in _collab_cache:
        return _collab_cache[author_id]

    # KT1 = Tezos originated/collab contract — list individual collaborators
    if author_id.upper().startswith("KT1"):
        try:
            data = gql(
                "query($id: String!) { user(id: $id) { collaborators { id name } } }",
                {"id": author_id},
            )
            collabs = (data.get("user") or {}).get("collaborators") or []
            names = [c.get("name") or c.get("id", "") for c in collabs]
            if names:
                result = "collab: " + " - ".join(names)
                _collab_cache[author_id] = result
                return result
        except Exception:
            pass
    else:
        # Any wallet address (tz1…, 0x…) — look up the fxhash username
        try:
            data = gql(
                "query($id: String!) { account(usernameOrAddress: $id) { username } }",
                {"id": author_id},
            )
            username = (data.get("account") or {}).get("username")
            if username:
                _collab_cache[author_id] = username
                return username
        except Exception:
            pass

    _collab_cache[author_id] = author_id
    return author_id


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

def ipfs_to_url(uri: str, gateway: str = IPFS_GATEWAY) -> str | None:
    if not uri:
        return None
    if uri.startswith("ipfs://"):
        return f"{gateway}/{uri[7:]}"
    return uri


def download_file(url: str, dest: Path, retries: int = 5) -> bool:
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=60, stream=True, allow_redirects=True)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            return True
        except Exception as e:
            if attempt == retries - 1:
                print(f"\n  [ERROR] Failed to download {url}: {e}")
                return False
            time.sleep(2 ** attempt)
    return False


# ---------------------------------------------------------------------------
# Images + metadata
# ---------------------------------------------------------------------------

def build_master_json(token: dict, objkts: list[dict], extra: dict = None) -> dict:
    """
    Build a comprehensive collection record for long-term archival.
    `token` is a GenerativeToken dict (from creations) or an issuer dict (from collected).
    `extra` is an optional dict of additional top-level fields to merge in.
    """
    token_meta = token.get("metadata") or {}
    description = (
        token_meta.get("description")
        or token_meta.get("childrenDescription")
        or ""
    )

    # Pricing
    pricing = {}
    if token.get("pricingFixed"):
        pf = token["pricingFixed"]
        pricing["type"] = "fixed"
        raw_price = int(pf["price"]) if pf.get("price") is not None else None
        pricing["priceMutez"] = raw_price
        pricing["priceXTZ"] = round(raw_price / 1_000_000, 6) if raw_price else None
        pricing["opensAt"] = pf.get("opensAt")
    elif token.get("pricingDutchAuction"):
        da = token["pricingDutchAuction"]
        pricing["type"] = "dutchAuction"
        pricing["levels"] = da.get("levels", [])
        raw_resting = int(da["restingPrice"]) if da.get("restingPrice") is not None else None
        pricing["restingPriceMutez"] = raw_resting
        pricing["restingPriceXTZ"] = (
            round(raw_resting / 1_000_000, 6) if raw_resting else None
        )
        pricing["decrementDuration"] = da.get("decrementDuration")
        pricing["opensAt"] = da.get("opensAt")

    # Collector distribution (over the objkts we have)
    owner_counts: Counter = Counter()
    owner_names: dict[str, str] = {}
    for objkt in objkts:
        owner = objkt.get("owner") or {}
        addr = owner.get("id") or "unknown"
        owner_counts[addr] += 1
        if owner.get("name"):
            owner_names[addr] = owner["name"]

    collectors = [
        {"address": addr, "name": owner_names.get(addr), "held": count}
        for addr, count in owner_counts.most_common()
    ]

    # Feature distribution
    feat_values: dict[str, Counter] = {}
    for objkt in objkts:
        for feat in (objkt.get("features") or []):
            fname = feat.get("name", "")
            fval  = feat.get("value", "")
            feat_values.setdefault(fname, Counter())[fval] += 1

    total = len(objkts)
    features_distribution = [
        {
            "name": fname,
            "values": [
                {"value": v, "count": c, "rarity": round(c / total, 6)}
                for v, c in counter.most_common()
            ],
        }
        for fname, counter in feat_values.items()
    ]

    # Per-iteration summary
    iterations = []
    for objkt in sorted(objkts, key=lambda o: o.get("iteration") or 0):
        minter = objkt.get("minter") or {}
        owner  = objkt.get("owner")  or {}
        iterations.append({
            "id":             objkt.get("id"),
            "iteration":      objkt.get("iteration"),
            "generationHash": objkt.get("generationHash"),
            "rarity":         objkt.get("rarity"),
            "mintedPrice":    objkt.get("mintedPrice"),
            "createdAt":      objkt.get("createdAt"),
            "assignedAt":     objkt.get("assignedAt"),
            "minter":  {"address": minter.get("id"), "name": minter.get("name")},
            "owner":   {"address": owner.get("id"),  "name": owner.get("name")},
            "features": objkt.get("features") or [],
        })

    doc = {
        "id":          token["id"],
        "name":        token["name"],
        "slug":        token["slug"],
        "description": description,
        "createdAt":   token.get("createdAt"),
        "supply":      token.get("supply"),
        "objktsCount": token.get("objktsCount"),
        "tags":        token.get("tags") or [],
        "generativeUri": token.get("generativeUri"),
        "captureMedia":  token.get("captureMedia"),
        "author":        token.get("author"),
        "pricing":       pricing,
        "collectors": {
            "uniqueCount":  len(collectors),
            "distribution": collectors,
        },
        "featuresDistribution": features_distribution,
        "iterations":  iterations,
        "exportedAt":  datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        doc.update(extra)
    return doc


def process_images(token: dict, objkts: list[dict], base_dir: Path,
                   workers: int, master_extra: dict = None) -> None:
    """Download images and write per-iteration + master JSON for a collection.

    Output layout (base_dir is fully resolved by the caller):
        base_dir/{slug}/thumbnails/{iteration:04d}.png
        base_dir/{slug}/metadata/{iteration:04d}.json
        base_dir/{slug}/metadata/_collection.json
    """
    token_dir  = base_dir / token["slug"]
    thumbnails = token_dir / "thumbnails"
    metadata   = token_dir / "metadata"
    thumbnails.mkdir(parents=True, exist_ok=True)
    metadata.mkdir(parents=True, exist_ok=True)

    master = build_master_json(token, objkts, extra=master_extra)
    with open(metadata / "_collection.json", "w") as f:
        json.dump(master, f, indent=2, ensure_ascii=False)
    print(f"  Collectors: {master['collectors']['uniqueCount']} unique  "
          f"(holding {len(objkts)} of {master['objktsCount']} total)")

    tasks = []
    for objkt in objkts:
        iteration = objkt.get("iteration") or 0
        prefix    = f"{iteration:04d}"
        img_path  = thumbnails / f"{prefix}.png"
        meta_path = metadata   / f"{prefix}.json"

        minter = objkt.get("minter") or {}
        owner  = objkt.get("owner")  or {}
        meta = {
            "id":             objkt["id"],
            "iteration":      iteration,
            "generationHash": objkt.get("generationHash"),
            "inputBytes":     objkt.get("inputBytes"),
            "rarity":         objkt.get("rarity"),
            "mintedPrice":    objkt.get("mintedPrice"),
            "createdAt":      objkt.get("createdAt"),
            "assignedAt":     objkt.get("assignedAt"),
            "minter":  {"address": minter.get("id"), "name": minter.get("name")},
            "owner":   {"address": owner.get("id"),  "name": owner.get("name")},
            "features": objkt.get("features") or [],
            "captureMedia": objkt.get("captureMedia"),
            "displayUri":   objkt.get("displayUri"),
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        cid = (objkt.get("captureMedia") or {}).get("cid")
        img_url = (
            f"{IPFS_GATEWAY}/{cid}" if cid
            else ipfs_to_url(objkt.get("displayUri"))
        )
        if img_url and not img_path.exists():
            tasks.append((img_url, img_path, iteration))

    if not tasks:
        print(f"  All {len(objkts)} images already downloaded.")
        return

    print(f"  Downloading {len(tasks)} image(s) with {workers} worker(s)...")
    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_file, url, path): itr for url, path, itr in tasks}
        for future in as_completed(futures):
            itr = futures[future]
            ok  = future.result()
            done += 1
            if not ok:
                failed += 1
            status = "  ERROR" if not ok else ""
            print(f"  [{done}/{len(tasks)}] iteration {itr}{status}        ", end="\r")
    print(f"\n  Done. {len(tasks)-failed} downloaded, {failed} failed.")


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def resolve_gen_uri(gen_uri: str) -> tuple[str, str]:
    """Return (scheme, base_url) for a generativeUri."""
    if gen_uri.startswith("ipfs://"):
        return "ipfs", f"{IPFS_FALLBACK}/{gen_uri[7:]}/"
    if gen_uri.startswith("onchfs://"):
        return "onchfs", f"https://onchfs.fxhash2.xyz/{gen_uri[9:]}/"
    return "unknown", ""


def build_url_params(token: dict, objkt: dict) -> str:
    """Build the full URL suffix (query string + optional hash fragment) passed to index.html.

    Returns a string starting with '?' like:
        ?fxhash=...&fxiteration=1&fxminter=tz1...
        ?fxhash=...&fxiteration=1&fxminter=tz1...&fxparams=3fdb...#0x3fdb...

    fxparams appears in both the query string (for SDK v2) and the hash fragment (for SDK v3+).
    """
    gen_uri     = token.get("generativeUri", "")
    hash_value  = objkt.get("generationHash") or ""
    iteration   = objkt.get("iteration") or 0
    input_bytes = objkt.get("inputBytes") or ""

    if gen_uri.startswith("onchfs://"):
        default_minter = "0x" + "0" * 40
        minter_id = (objkt.get("minter") or {}).get("id") or default_minter
        qs = (
            f"?cid={quote(gen_uri)}"
            f"&fxhash={hash_value}"
            f"&fxminter={minter_id}"
            f"&fxiteration={iteration}"
            f"&fxcontext=standalone"
        )
        if input_bytes:
            qs += f"&fxparams={input_bytes}"
            return qs + f"#0x{input_bytes}"
        return qs

    # Tezos / IPFS tokens
    default_minter = "tz1" + "1" * 33
    minter_id = (objkt.get("minter") or {}).get("id") or default_minter
    qs = (
        f"?fxhash={hash_value}"
        f"&fxiteration={iteration}"
        f"&fxminter={minter_id}"
    )
    if input_bytes:
        qs += f"&fxparams={input_bytes}"
        return qs + f"#0x{input_bytes}"
    return qs


def extract_ipfs_bundle(cid: str, folder: Path) -> bool:
    """Download the complete IPFS directory as tar and extract every file."""
    tar_url = f"{IPFS_FALLBACK}/{cid}?format=tar"
    print(f"  Downloading bundle as tar...")
    for attempt in range(5):
        try:
            r = requests.get(tar_url, timeout=120, stream=True)
            r.raise_for_status()
            buf = io.BytesIO()
            for chunk in r.iter_content(chunk_size=65536):
                buf.write(chunk)
            buf.seek(0)
            break
        except Exception as e:
            if attempt == 4:
                print(f"  [ERROR] Could not download bundle: {e}")
                return False
            time.sleep(2 ** attempt)

    try:
        with tarfile.open(fileobj=buf) as tf:
            members = tf.getmembers()
            written = 0
            for member in members:
                if not member.isfile():
                    continue
                parts = Path(member.name).parts
                rel   = Path(*parts[1:]) if len(parts) > 1 else Path(member.name)
                dest  = folder / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    with tf.extractfile(member) as src:
                        dest.write_bytes(src.read())
                    written += 1
            print(f"  Extracted {written} new file(s) ({len(members)} total in bundle).")
    except Exception as e:
        print(f"  [ERROR] Failed to extract bundle: {e}")
        return False
    return True


def prepare_html_collection(token: dict, package_dir: Path) -> bool:
    """Download the full generative bundle into package_dir/. Returns True on success."""
    gen_uri = token.get("generativeUri")
    if not gen_uri:
        print(f"  [skip HTML] No generativeUri for '{token['name']}'")
        return False

    scheme, base_url = resolve_gen_uri(gen_uri)
    if not base_url:
        print(f"  [skip HTML] Unsupported URI scheme: {gen_uri}")
        return False

    package_dir.mkdir(parents=True, exist_ok=True)

    if (package_dir / "index.html").exists():
        return True  # already extracted

    if scheme == "ipfs":
        return extract_ipfs_bundle(gen_uri[7:], package_dir)

    # Fallback (onchfs / unknown): fetch index.html + parse referenced assets
    print(f"  Fetching generative bundle ({scheme})...")
    try:
        r = requests.get(base_url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"  [ERROR] Could not fetch generative HTML: {e}")
        return False
    (package_dir / "index.html").write_text(html, encoding="utf-8")

    for asset in re.findall(r'(?:src|href)="([^"#?][^"]*)"', html):
        if asset.startswith(("http", "//")):
            continue
        asset_path = package_dir / asset
        if not asset_path.exists():
            print(f"  Downloading asset: {asset}")
            download_file(base_url + asset, asset_path)

    return True


def generate_html_iterations(token: dict, objkts: list[dict], html_dir: Path) -> None:
    """Write one tiny redirect HTML per iteration into html_dir/.

    Each file redirects to package/index.html with the correct URL parameters.
    These files are always (re)written so that URL parameter changes take effect
    without needing to manually delete them.
    """
    written = skipped = 0
    for objkt in objkts:
        iteration  = objkt.get("iteration") or 0
        hash_value = objkt.get("generationHash") or ""
        dest       = html_dir / f"{iteration:04d}.html"

        if not hash_value:
            print(f"  [WARN] No generationHash for iteration {iteration} — skipping")
            skipped += 1
            continue

        suffix   = build_url_params(token, objkt)
        redirect = f'<!doctype html><script>location.replace("package/index.html{suffix}")</script>\n'
        dest.write_text(redirect, encoding="utf-8")
        written += 1

    print(f"  HTML: {written} written, {skipped} skipped (no hash)  ({written+skipped} total)")


def process_html(token: dict, objkts: list[dict], base_dir: Path) -> None:
    """Download generative bundle and write per-iteration redirects.

    Output layout (base_dir is fully resolved by the caller):
        base_dir/{slug}/html/package/index.html   ← original bundle files
        base_dir/{slug}/html/{iteration:04d}.html  ← per-iteration redirects
    """
    html_dir    = base_dir / token["slug"] / "html"
    package_dir = html_dir / "package"
    html_dir.mkdir(parents=True, exist_ok=True)
    if prepare_html_collection(token, package_dir):
        generate_html_iterations(token, objkts, html_dir)
