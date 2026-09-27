"""Publish showcase/ as a free static Hugging Face Space.

    pip install huggingface_hub
    python scripts/deploy_showcase.py --space citedesk     # -> <your-username>/citedesk

Authentication: HF_TOKEN with write access (or `hf auth login`).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

SHOWCASE = Path(__file__).resolve().parent.parent / "showcase"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--space", required=True, help="Space id, e.g. yourname/citedesk (or just citedesk)")
    args = parser.parse_args()
    if not (SHOWCASE / "index.html").is_file():
        raise SystemExit("showcase/index.html not found. Run scripts/build_showcase.py first.")

    from huggingface_hub import HfApi

    api = HfApi(token=(os.getenv("HF_TOKEN") or "").strip() or None)
    username = api.whoami()["name"]
    space = args.space if "/" in args.space else f"{username}/{args.space}"
    print(f"Logged in to Hugging Face as {username}")
    try:
        api.create_repo(space, repo_type="space", space_sdk="static", exist_ok=True)
    except Exception as exc:
        raise SystemExit(f"Could not create the Space: {exc}") from None
    api.upload_folder(folder_path=str(SHOWCASE), repo_id=space, repo_type="space", commit_message="Update showcase")
    print(f"Deployed: https://huggingface.co/spaces/{space}")


if __name__ == "__main__":
    main()
