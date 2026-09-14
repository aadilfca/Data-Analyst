"""Command-line entry point for the read-only storage preview."""

import json

from services.storage_cleanup import get_storage_cleanup_preview


if __name__ == "__main__":
    print(json.dumps(get_storage_cleanup_preview(), default=str, indent=2))
