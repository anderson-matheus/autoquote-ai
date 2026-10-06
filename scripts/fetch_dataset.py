"""Downloads the challenge dataset into dataset/ (git-ignored: it is PII-shaped)."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import httpx

URL = (
    "https://raw.githubusercontent.com/namastexlabs/namastex-fde-challenge/main/"
    "dataset/conversations.parquet"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("dataset/conversations.parquet"))
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    response = httpx.get(URL, follow_redirects=True, timeout=60)
    response.raise_for_status()
    args.out.write_bytes(response.content)
    digest = hashlib.sha256(response.content).hexdigest()[:16]
    print(f"saved {len(response.content):,} bytes to {args.out} (sha256 {digest}...)")


if __name__ == "__main__":
    main()
