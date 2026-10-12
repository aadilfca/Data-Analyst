"""Command-line entry point for previewing or explicitly applying cleanup."""

import argparse
import json

from services.storage_cleanup import (
    CONFIRMATION_TEXT,
    INDEX_MAINTENANCE_CONFIRMATION_TEXT,
    apply_storage_cleanup,
    apply_index_maintenance,
    get_storage_cleanup_preview,
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview or apply approved storage cleanup")
    parser.add_argument("--apply", action="store_true", help="apply the supplied fresh preview")
    parser.add_argument("--preview-hash", help="hash returned by the fresh preview")
    parser.add_argument("--confirm", help=f"must equal: {CONFIRMATION_TEXT}")
    parser.add_argument("--remove-index", help="evidence-approved index name to remove")
    parser.add_argument("--index-preview-hash", help="fresh storage preview hash for index maintenance")
    parser.add_argument(
        "--index-confirm",
        help=f"must equal: {INDEX_MAINTENANCE_CONFIRMATION_TEXT}",
    )
    parser.add_argument("--operator", default="CLI administrator")
    args = parser.parse_args()

    if args.remove_index:
        if not args.index_preview_hash or args.index_confirm != INDEX_MAINTENANCE_CONFIRMATION_TEXT:
            parser.error(
                "--remove-index requires --index-preview-hash and "
                f"--index-confirm '{INDEX_MAINTENANCE_CONFIRMATION_TEXT}'"
            )
        print(json.dumps(
            apply_index_maintenance(
                preview_hash=args.index_preview_hash,
                index_name=args.remove_index,
                operator=args.operator,
                confirmation=args.index_confirm,
            ),
            default=str,
            indent=2,
        ))
    elif not args.apply:
        print(json.dumps(get_storage_cleanup_preview(), default=str, indent=2))
    else:
        if not args.preview_hash or args.confirm != CONFIRMATION_TEXT:
            parser.error(f"--apply requires --preview-hash and --confirm '{CONFIRMATION_TEXT}'")
        print(json.dumps(
            apply_storage_cleanup(args.preview_hash, args.operator, args.confirm),
            default=str,
            indent=2,
        ))
