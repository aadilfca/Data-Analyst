import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import storage_cleanup
from routers.auth import require_admin


class StorageCleanupPreviewTests(unittest.TestCase):
    def test_preview_route_returns_read_only_payload(self):
        expected = {"mode": "dry_run", "database": {"total_bytes": 100, "formatted": "100 B"}}
        with patch.object(storage_cleanup, "get_storage_cleanup_preview", return_value=expected):
            result = storage_cleanup.storage_cleanup_preview({"role": "admin"})
        self.assertEqual(result, expected)

    def test_preview_route_only_calls_preview_service(self):
        with patch.object(storage_cleanup, "get_storage_cleanup_preview", return_value={"mode": "dry_run"}) as preview:
            storage_cleanup.storage_cleanup_preview({"role": "admin"})
        preview.assert_called_once_with()

    def test_admin_guard_rejects_non_admin(self):
        with self.assertRaises(HTTPException) as error:
            require_admin({"role": "analyst"})
        self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
