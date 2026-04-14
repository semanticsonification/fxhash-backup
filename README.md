# fxhash backup

Scripts to archive [fxhash](https://www.fxhash.xyz/) generative art collections locally — high-res renders, metadata, and self-contained HTML previews — across all chains (Tezos and Base/Ethereum).

## Scripts

### `download_creations.py`
Downloads all collections **created** by a user.

### `download_collected.py`
Downloads all artworks **collected** (owned) by a user, excluding their own creations.

## Requirements

```bash
pip install requests
```

## Usage

Both scripts accept a fxhash username, Tezos address (`tz1…`), or EVM address (`0x…`):

```bash
python3 download_creations.py --user monokai
python3 download_collected.py --user monokai

python3 download_creations.py --user tz1…
python3 download_collected.py --user 0x…
```

All linked wallets (Tezos + EVM) are discovered automatically from the account.

### Options

| Flag | Description |
|------|-------------|
| `--user USER` | fxhash username, Tezos address, or EVM address (required) |
| `--output DIR` | Root output directory (default: `./downloads`) |
| `--workers N` | Parallel image download workers (default: 4) |
| `--no-images` | Skip image and metadata download |
| `--no-html` | Skip HTML preview download |
| `--token-id ID` | Creations only: process a single token by ID or slug |

## Output layout

```
downloads/
  {username}/
    creations/
      {slug}/
        thumbnails/        ← render PNG per iteration
        metadata/
          _collection.json ← full collection record with feature distribution
          {iteration}.json ← per-iteration metadata
        html/
          package/         ← self-contained generative bundle (original files)
          {iteration}.html ← opens the artwork for that specific iteration
    collection/
      {author}/
        {slug}/
          thumbnails/
          metadata/
          html/
```

## HTML previews

Open any `{iteration}.html` file directly in a browser — no server needed for most artworks. It redirects to `package/index.html` with the correct URL parameters (`fxhash`, `fxiteration`, `fxminter`, and `fxparams` for parametric works).

## APIs used

- **fxhash V1** (`api.fxhash.xyz/graphql`) — account/wallet resolution, collab contract lookup
- **fxhash V2** (`api.v2.fxhash.xyz/v1/graphql`) — all tokens across all chains (Tezos, Base, Ethereum)
- **IPFS** (`ipfs.io`) — bundle downloads
- **fxhash IPFS gateway** (`gateway.fxhash.xyz`) — render image downloads
- **onchfs** (`onchfs.fxhash2.xyz`) — Base/Ethereum chain generative bundles
