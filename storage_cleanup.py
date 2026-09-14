"""Command-line entry point for previewing or explicitly applying cleanup."""

import argparse
import json

from services.storage_cleanup import (
    CONFIRMATION_TEXT,
    apply_storage_cleanup,
    get_storage_cleanup_preview,
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview or apply approved storage cleanup")
    parser.add_argument("--apply", action="store_true", help="apply the supplied fresh preview")
    parser.add_argument("--preview-hash", help="hash returned by the fresh preview")
    parser.add_argument("--confirm", help=f"must equal: {CONFIRMATION_TEXT}")
    parser.add_argument("--operator", default="CLI administrator")
    args = parser.parse_args()

    if not args.apply:
        print(json.dumps(get_storage_cleanup_preview(), default=str, indent=2))
    else:
        if not args.preview_hash or args.confirm != CONFIRMATION_TEXT:
            parser.error(f"--apply requires --preview-hash and --confirm '{CONFIRMATION_TEXT}'")
        print(json.dumps(
            apply_storage_cleanup(args.preview_hash, args.operator, args.confirm),
            default=str,
            indent=2,
        ))
