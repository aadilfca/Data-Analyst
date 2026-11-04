import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from database import ROW_OVERHEAD_BYTES
from routers.data_integrity import _module_row, get_data_integrity_overview
from routers.data_integrity import _database_safeguards, _matches_required_index


def _index_catalog():
    """Representative canonical definitions, independent of the requirement registry."""
    specifications = [
        ('client_data', ('id',), 'primary'),
        ('client_data', ('client_id', 'template_id', 'source_identity'), 'active'),
        ('client_data', ('client_id', 'template_id', 'is_deleted', 'source_identity'), 'lookup'),
        ('client_data', ('client_id', 'is_deleted', 'extract_date'), 'lookup'),
        ('client_data', ('client_id', 'is_deleted', 'derived_transaction_date'), 'lookup'),
        ('client_data', ('client_id', 'is_deleted', 'derived_treasury_type', 'derived_transaction_date'), 'lookup'),
        ('trial_balance_uploads', ('id',), 'primary'),
        ('trial_balance_uploads', ('client_id',), 'lookup'),
        ('trial_balance_accounts', ('id',), 'primary'),
        ('trial_balance_accounts', ('upload_id', 'account_code'), 'unique'),
        ('trial_balance_accounts', ('upload_id', 'parent_account_code'), 'lookup'),
        ('report_balance_inputs', ('id',), 'primary'),
        ('report_balance_inputs', ('client_id', 'group_label', 'account_code', 'period_month'), 'unique'),
        ('report_balance_inputs', ('client_id', 'group_label', 'period_month'), 'lookup'),
    ]
    return [dict(
        table_name=table, index_name=f'example_{number}', access_method='btree',
        key_columns=list(keys), is_valid=True, is_ready=True, is_live=True,
        standard_keys=True, is_unique=kind != 'lookup', is_immediate=True,
        is_primary=kind == 'primary',
        predicate=("((is_deleted = false) AND (change_detection_status <> 'deleted'::text) "
                   "AND (source_identity IS NOT NULL) AND (source_identity <> ''::text))")
        if kind == 'active' else None,
    ) for number, (table, keys, kind) in enumerate(specifications)]


def _clean_modules():
    return [_module_row(name, 0 if name == 'Reconciliation' else 4, 0, 100, 0, 0, 0, 'Scoped identity')
            for name in ('Client Data', 'Trial Balance', 'Reconciliation')]


class DataIntegrityOverviewTests(unittest.TestCase):
    def setUp(self):
        # No production connection: exercise the snapshot callback with mocked reads.
        snapshot = patch('routers.data_integrity.run_read_snapshot_pg', side_effect=lambda operation: operation(None))
        self.snapshot = snapshot.start()
        self.addCleanup(snapshot.stop)

    def _query_response(self, query, params=None):
        normalized = " ".join(query.split()).lower()
        self.assertTrue(normalized.startswith("select"))
        if "from clients" in normalized:
            return [{"id": 7, "client_name": "Leather Crop"}]
        if "sum(length(cd.row_data::text))" in normalized:
            return [{"row_count": 3, "data_bytes": 868, "unidentified_rows": 0}]
        if "from uploaded_files u" in normalized:
            return [{"id": 11, "file_path": self.source_path, "row_bytes": 12}]
        if "group by template_id, source_identity" in normalized:
            return [{"duplicate_groups": 1, "duplicate_extra_rows": 1}]
        if "sum(pg_column_size(a))" in normalized:
            return [{"row_count": 4, "data_bytes": 200, "unidentified_rows": 0}]
        if "from trial_balance_uploads u where" in normalized:
            return [{"id": 21, "row_bytes": 15}, {"id": 22, "row_bytes": 15}]
        if "group by a.upload_id, a.account_code" in normalized:
            return [{"duplicate_groups": 0, "duplicate_extra_rows": 0}]
        if "sum(pg_column_size(r))" in normalized:
            return [{"row_count": 1, "data_bytes": 30, "unidentified_rows": 0}]
        if "group by lower(btrim(group_label))" in normalized:
            return [{"duplicate_groups": 0, "duplicate_extra_rows": 0}]
        if "pg_database_size(current_database())" in normalized:
            return [{"database_bytes": 10000, "shared_index_bytes": 3000}]
        if 'from pg_class t' in normalized:
            self.assertEqual(params, (['client_data', 'report_balance_inputs', 'trial_balance_accounts', 'trial_balance_uploads'],))
            return _index_catalog()
        self.fail(f"Unexpected query: {query}")

    def test_inventory_keeps_module_duplicate_scopes_and_sizes_separate(self):
        with tempfile.TemporaryDirectory() as staging:
            self.source_path = os.path.join(staging, "source.xlsx")
            with open(self.source_path, "wb") as source:
                source.write(b"workbook-bytes")
            with patch.dict(os.environ, {"UPLOAD_STAGING_DIR": staging}), patch(
                "routers.data_integrity.execute_query_pg", side_effect=self._query_response
            ) as query:
                result = get_data_integrity_overview(client_id=7)

        self.assertEqual(result["overall_status"], "Review")
        self.assertEqual(result['safeguards']['status'], 'Needs attention')
        self.assertEqual(result['safeguards']['modules'][1]['status'], 'Passed')
        self.snapshot.assert_called_once()
        self.assertEqual(result["client_name"], "Leather Crop")
        self.assertEqual(
            [row["module"] for row in result["modules"]],
            ["Client Data", "Trial Balance", "Reconciliation"],
        )
        client_data, trial_balance, reconciliation = result["modules"]
        self.assertEqual(client_data["duplicate_groups"], 1)
        self.assertEqual(client_data["duplicate_extra_rows"], 1)
        self.assertEqual(client_data["total_data_bytes"], 894)
        self.assertEqual(trial_balance["duplicate_status"], "Clear")
        self.assertEqual(trial_balance["associated_record_count"], 2)
        self.assertEqual(trial_balance["total_data_bytes"], 230)
        self.assertEqual(reconciliation["total_data_bytes"], 30)
        self.assertEqual(result["database_context"]["shared_index_bytes"], 3000)
        self.assertTrue(all(call.args[0].lstrip().upper().startswith("SELECT") for call in query.call_args_list))
        client_size_query = next(
            call for call in query.call_args_list if "SUM(LENGTH(cd.row_data::text))" in call.args[0]
        )
        self.assertEqual(client_size_query.args[1], (ROW_OVERHEAD_BYTES, 7))

    def test_source_file_outside_upload_staging_is_not_read_or_counted(self):
        with tempfile.TemporaryDirectory() as staging, tempfile.TemporaryDirectory() as external:
            self.source_path = os.path.join(external, "outside.xlsx")
            with open(self.source_path, "wb") as source:
                source.write(b"private")
            with patch.dict(os.environ, {"UPLOAD_STAGING_DIR": staging}):
                from routers.data_integrity import _measure_source_files

                measured, unmeasured = _measure_source_files([{"file_path": self.source_path}])

        self.assertEqual(measured, 0)
        self.assertEqual(unmeasured, 1)

    def test_missing_identity_marks_duplicate_scan_incomplete_not_clear(self):
        row = _module_row(
            "Client Data", 4, 0, 100, 0, 0, 0, "Saved source identity.", unidentified_rows=1
        )

        self.assertEqual(row["duplicate_status"], "Not fully checked")
        self.assertEqual(row["unidentified_rows"], 1)

    def test_inactive_client_is_rejected_before_scanning(self):
        with patch("routers.data_integrity.execute_query_pg", return_value=[]) as query:
            with self.assertRaises(HTTPException) as raised:
                get_data_integrity_overview(client_id=7)

        self.assertEqual(raised.exception.status_code, 404)
        query.assert_called_once()

    def test_identification_checks_include_empty_keys_and_stay_client_scoped(self):
        self.source_path = None
        with patch('routers.data_integrity.execute_query_pg', side_effect=self._query_response) as query:
            get_data_integrity_overview(client_id=7)
        checks = [call for call in query.call_args_list if 'AS unidentified_rows' in call.args[0]]
        self.assertEqual(len(checks), 3)
        for call in checks:
            self.assertIn('BTRIM(', call.args[0])
            self.assertIn(7, call.args[1])
        self.assertIn('cd.template_id IS NULL', checks[0].args[0])


class DatabaseSafeguardTests(unittest.TestCase):
    def _scan(self, indexes=None, modules=None, error=None):
        with patch('routers.data_integrity.execute_query_pg', return_value=_index_catalog() if indexes is None else indexes,
                   side_effect=error):
            return _database_safeguards(_clean_modules() if modules is None else modules)

    def test_valid_definitions_pass_and_empty_module_does_not_claim_rows_checked(self):
        result = self._scan()
        self.assertEqual(result['status'], 'Passed')
        self.assertFalse(result['incomplete'])
        self.assertEqual(result['modules'][2]['checks'][1]['detail'], 'No records to check.')
        self.assertIn('physical index entry', result['limit_note'])

    def test_catalog_failure_is_not_checked_but_keeps_record_results(self):
        result = self._scan(error=RuntimeError('private connection detail'))
        self.assertEqual(result['status'], 'Not checked')
        self.assertTrue(result['incomplete'])
        for module in result['modules']:
            self.assertEqual(module['checks'][0]['status'], 'Not checked')
            self.assertEqual(module['checks'][1]['status'], 'Passed')
        self.assertNotIn('private connection detail', str(result))

    def test_missing_table_or_unique_index_needs_attention(self):
        for indexes in ([], _index_catalog()[1:], [i for i in _index_catalog() if i['predicate'] is None]):
            with self.subTest(indexes=len(indexes)):
                result = self._scan(indexes=indexes)
                self.assertEqual(result['status'], 'Needs attention')

    def test_invalid_or_different_index_is_never_accepted_by_its_name(self):
        mutations = [
            ('is_valid', False), ('is_ready', False), ('is_live', False),
            ('is_unique', False), ('is_immediate', False), ('standard_keys', False),
            ('access_method', 'hash'), ('key_columns', ['template_id', 'client_id', 'source_identity']),
            ('predicate', None),
            ('predicate', "is_deleted = FALSE OR change_detection_status <> 'deleted' AND source_identity IS NOT NULL AND source_identity <> ''"),
            ('predicate', "is_deleted = FALSE AND change_detection_status <> 'DELETED' AND source_identity IS NOT NULL AND source_identity <> ''"),
            ('predicate', "is_deleted = FALSE AND change_detection_status <> 'deleted' AND source_identity IS NOT NULL"),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                indexes = _index_catalog()
                indexes[1][key] = value
                result = self._scan(indexes=indexes)
                self.assertEqual(result['modules'][0]['checks'][0]['status'], 'Needs attention')

    def test_equivalent_renamed_and_covering_lookup_indexes_are_accepted(self):
        indexes = _index_catalog()
        for index in indexes:
            index['index_name'] = 'renamed_' + index['index_name']
        indexes[7]['key_columns'].append('uploaded_at')
        self.assertEqual(self._scan(indexes=indexes)['status'], 'Passed')
        # A different uniqueness scope must not pass as duplicate protection.
        indexes[9]['key_columns'].append('id')
        self.assertEqual(self._scan(indexes=indexes)['modules'][1]['status'], 'Needs attention')

    def test_identity_gaps_duplicates_and_unavailable_checks_are_reported_together(self):
        modules = _clean_modules()
        modules[0] = _module_row('Client Data', 4, 0, 100, 0, 2, 3, 'Scoped', unidentified_rows=1)
        result = self._scan(modules=modules, error=RuntimeError('offline'))
        self.assertEqual(result['status'], 'Needs attention')
        self.assertTrue(result['incomplete'])
        checks = result['modules'][0]['checks']
        self.assertEqual([check['status'] for check in checks], ['Not checked', 'Needs attention', 'Needs attention'])
        self.assertIn('1 rows need', checks[1]['detail'])

    def test_missing_identity_evidence_is_not_treated_as_zero(self):
        modules = _clean_modules()
        del modules[1]['unidentified_rows']
        self.assertEqual(self._scan(modules=modules)['modules'][1]['status'], 'Not checked')

    def test_non_primary_unique_index_is_not_a_primary_key(self):
        index = _index_catalog()[0]
        index['is_primary'] = False
        self.assertFalse(_matches_required_index(index, ('Row', 'client_data', ('id',), 'primary', None)))


if __name__ == "__main__":
    unittest.main()
