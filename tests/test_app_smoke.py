import os
import tempfile
import unittest
from unittest.mock import patch

import database
from database import init_db


class AppSmokeTests(unittest.TestCase):
    """Confirms the FastAPI app boots and its routers actually respond, end-to-end over real HTTP
    request/response handling (not just calling a function directly, like the other tests here) —
    against a throwaway temp database (never the real test.db), same isolation pattern as
    test_verification_indexes.py / test_upload_validation_counts.py."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()
        # Imported here (not at module scope) so database.DB_NAME is already patched before
        # main.py's app is built and its startup event (which also calls init_db()) runs.
        from fastapi.testclient import TestClient
        import main
        self.client = TestClient(main.app)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_dashboard_page_serves(self):
        response = self.client.get("/static/index.html")
        self.assertEqual(response.status_code, 200)

    def test_clients_api_responds_on_fresh_db(self):
        # The clients router is Postgres-backed in production. Keep this smoke test
        # hermetic: the HTTP/router wiring is real, while the external PG read is stubbed.
        with patch("routers.clients.execute_query_pg", return_value=[]):
            response = self.client.get("/api/clients")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


if __name__ == "__main__":
    unittest.main()
