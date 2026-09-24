"""Regression coverage for published Account Set treasury routing."""

import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from routers import account_control_center as control_center
from routers import verification


class TreasuryRoutingTests(unittest.TestCase):
    def test_published_set_is_used_before_prefixes(self):
        published = {
            "set_key": "cash_accounts",
            "version_number": 3,
            "accounts": [{"account_code": "10-15-115-10-0001", "account_name": "CASH IN HAND"}],
        }
        with patch.object(
            verification,
            "get_published_account_set_leaf_accounts",
            return_value=published,
        ), patch.object(verification, "get_client_voucher_prefixes_pg") as prefixes:
            routing = verification.get_treasury_routing(7, "cash")

        self.assertEqual(routing["mode"], "account_set")
        self.assertEqual(routing["account_codes"], {"10-15-115-10-0001"})
        self.assertEqual(routing["version_number"], 3)
        prefixes.assert_not_called()

    def test_prefixes_remain_the_safe_fallback_when_no_set_is_published(self):
        configured = {
            "Cash Payment": ["CPV"],
            "Cash Receipt": ["CRV"],
            "Bank Payment": ["BPV"],
            "Bank Receipt": ["BRV"],
        }
        with patch.object(
            verification,
            "get_published_account_set_leaf_accounts",
            return_value=None,
        ), patch.object(
            verification,
            "get_client_voucher_prefixes_pg",
            return_value=configured,
        ):
            routing = verification.get_treasury_routing(7, "cash")

        self.assertEqual(routing, {
            "mode": "voucher_prefix",
            "prefixes": {"Cash Payment": ["CPV"], "Cash Receipt": ["CRV"]},
        })

    def test_control_centre_lookup_queries_only_published_versions(self):
        calls = []

        def query(sql, params):
            calls.append((sql, params))
            return []

        with patch.object(control_center, "execute_query_pg", side_effect=query):
            result = control_center.get_published_account_set_leaf_accounts(7, "cash_accounts")

        self.assertIsNone(result)
        self.assertEqual(calls[0][1], (7, "cash_accounts"))
        self.assertIn("v.status = 'published'", calls[0][0])


class AccountSetSessionTests(unittest.TestCase):
    def _start_session(self, rows, routing, cashbook_name, session_type):
        detail_batches = []

        def query(sql, params):
            if "FROM clients" in sql:
                return [{"id": 7}]
            if "FROM client_data" in sql:
                return rows
            raise AssertionError(f"Unexpected query: {sql}")

        def write(sql, params):
            if "INSERT INTO verification_sessions" in sql:
                return 91
            return []

        def batch_insert(conn, sql, params, template):
            detail_batches.append(params)
            return []

        with patch.object(verification, "execute_query_pg", side_effect=query), patch.object(
            verification, "execute_write_pg", side_effect=write
        ), patch.object(
            verification, "execute_batch_insert_with_ids_pg", side_effect=batch_insert
        ), patch.object(verification, "get_raw_conn_pg", return_value=object()), patch.object(
            verification, "release_raw_conn_pg"
        ), patch.object(verification, "get_audit_rules_config_version", return_value=1), patch.object(
            verification, "get_treasury_routing", return_value=routing
        ), patch.object(verification, "log_audit"):
            result = verification.start_verification_session(
                client_id=7,
                cashbook_name=cashbook_name,
                period_start="2026-01-01",
                period_end="2026-01-31",
                session_type=session_type,
            )

        self.assertEqual(result["session_id"], 91)
        self.assertEqual(len(detail_batches), 1)
        return detail_batches[0]

    def test_journal_voucher_with_cash_account_routes_to_cash_verification(self):
        rows = [
            {
                "id": 101,
                "row_data": {
                    "Voucher ID": "JV-0126-9",
                    "Account Code": "10-15-115-10-0001",
                    "Account Name": "CASH IN HAND",
                    "Debit": "0",
                    "Credit": "5000",
                },
                "extract_date": "2026-01-14",
                "extract_amount": 5000,
            },
            {
                "id": 102,
                "row_data": {
                    "Voucher ID": "JV-0126-9",
                    "Account Code": "60-10-15-0001",
                    "Account Name": "Salaries - Admin",
                    "Debit": "5000",
                    "Credit": "0",
                },
                "extract_date": "2026-01-14",
                "extract_amount": 5000,
            },
        ]
        routing = {"mode": "account_set", "account_codes": {"10-15-115-10-0001"}}

        details = self._start_session(rows, routing, "CASH IN HAND", "cash")

        self.assertEqual(details, [(91, 102, "JV-0126-9")])

    def test_cash_bank_transfer_routes_to_both_workstreams(self):
        rows = [
            {
                "id": 201,
                "row_data": {
                    "Voucher ID": "JV-0126-10",
                    "Account Code": "10-15-115-10-0001",
                    "Account Name": "CASH IN HAND",
                    "Debit": "1000",
                    "Credit": "0",
                },
                "extract_date": "2026-01-15",
                "extract_amount": 1000,
            },
            {
                "id": 202,
                "row_data": {
                    "Voucher ID": "JV-0126-10",
                    "Account Code": "10-15-115-15-0001",
                    "Account Name": "Leathercrop (Meezan Bank)",
                    "Debit": "0",
                    "Credit": "1000",
                },
                "extract_date": "2026-01-15",
                "extract_amount": 1000,
            },
        ]

        cash_details = self._start_session(
            rows,
            {"mode": "account_set", "account_codes": {"10-15-115-10-0001"}},
            "CASH IN HAND",
            "cash",
        )
        bank_details = self._start_session(
            rows,
            {"mode": "account_set", "account_codes": {"10-15-115-15-0001"}},
            "Leathercrop (Meezan Bank)",
            "bank",
        )

        self.assertEqual(cash_details, [(91, 202, "JV-0126-10")])
        self.assertEqual(bank_details, [(91, 201, "JV-0126-10")])


if __name__ == "__main__":
    unittest.main()
class CashbookPreviewPresentationTests(unittest.TestCase):
    def test_payment_uses_negative_counterpart_allocations_and_cash_credit_total(self):
        presentation = verification.cashbook_preview_presentation(
            "CPV",
            {"debit": 0, "credit": 10000},
            [{"debit": 6000, "credit": 0}, {"debit": 4000, "credit": 0}],
        )

        self.assertEqual(presentation["direction"], "Payment")
        self.assertEqual(presentation["amount_sign"], -1)
        self.assertEqual(presentation["treasury_amount"], 10000)
        self.assertEqual(presentation["counterpart_total"], 10000)
        self.assertTrue(presentation["is_balanced"])

    def test_receipt_uses_positive_counterpart_allocations_and_cash_debit_total(self):
        presentation = verification.cashbook_preview_presentation(
            "CRV",
            {"debit": 8500, "credit": 0},
            [{"debit": 0, "credit": 5000}, {"debit": 0, "credit": 3500}],
        )

        self.assertEqual(presentation["direction"], "Receipt")
        self.assertEqual(presentation["amount_sign"], 1)
        self.assertEqual(presentation["treasury_amount"], 8500)
        self.assertEqual(presentation["counterpart_total"], 8500)
        self.assertTrue(presentation["is_balanced"])

    def test_difference_is_flagged_when_counterpart_total_does_not_equal_treasury_leg(self):
        presentation = verification.cashbook_preview_presentation(
            "BPV",
            {"debit": 0, "credit": 10000},
            [{"debit": 9750, "credit": 0}],
        )

        self.assertEqual(presentation["imbalance_amount"], 250)
        self.assertFalse(presentation["is_balanced"])

class VerificationTableAmountTests(unittest.TestCase):
    def test_actual_frontend_helper_uses_the_non_zero_debit_or_credit_side(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "verification.js").read_text(
            encoding="utf-8"
        )

        def extract(pattern):
            match = re.search(pattern, source, re.DOTALL)
            self.assertIsNotNone(match, f"Could not extract frontend helper: {pattern}")
            return match.group(0)

        helper_source = "\n".join([
            extract(r"const VOUCHER_LINE_DEBIT_ALIASES = .*?;\nconst VOUCHER_LINE_CREDIT_ALIASES = .*?;"),
            extract(r"function getVoucherLineAlias\(.*?\n}"),
            extract(r"function getVoucherLineAmount\(.*?\n}"),
            extract(r"function parseVoucherAmount\(.*?\n}"),
        ])
        script = helper_source + "\nconsole.log(JSON.stringify([" + ",".join([
            "getVoucherLineAmount({'Debit Amount': 0, 'Credit Amount': 5000000})",
            "getVoucherLineAmount({'Debit Amount': 8000, 'Credit Amount': 0})",
            "getVoucherLineAmount({'Debit Amount': 0, 'Credit Amount': 0})",
        ]) + "]))"

        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(json.loads(completed.stdout), [5000000, 8000, 0])


class LocalVendorRoutingFoundationTests(unittest.TestCase):
    def test_local_vendor_requires_a_published_set(self):
        with patch.object(verification, 'get_published_account_set_leaf_accounts', return_value=None), patch.object(verification, 'get_client_voucher_prefixes_pg') as prefixes:
            routing = verification.get_local_vendor_routing(7)
        self.assertEqual(routing['mode'], 'not_configured')
        prefixes.assert_not_called()

    def test_local_vendor_uses_published_account_codes(self):
        published = {'set_key': 'local_vendors', 'version_number': 1, 'accounts': [{'account_code': '20-15-50-0001'}]}
        with patch.object(verification, 'get_published_account_set_leaf_accounts', return_value=published):
            routing = verification.get_local_vendor_routing(7)
        self.assertEqual(routing['account_codes'], {'20-15-50-0001'})

    def test_treasury_priority_excludes_vendor_voucher(self):
        voucher = {'voucher_id': 'JV-1', 'rows': [{'account_code': '20-15-50-0001'}, {'account_code': '10-15-115-10-0001'}]}
        self.assertFalse(verification.is_local_vendor_voucher(voucher, '20-15-50-0001', {'mode': 'account_set', 'account_codes': {'10-15-115-10-0001'}}, {'mode': 'account_set', 'account_codes': set()}))

    def test_code_label_round_trip_is_not_name_based(self):
        label = verification.local_vendor_label('20-15-50-0001', 'Duplicate Vendor Name')
        self.assertEqual(verification.local_vendor_code_from_label(label), '20-15-50-0001')

class ImportVendorRoutingFoundationTests(unittest.TestCase):
    import_code = '20-15-45-10-0001'

    def test_import_vendor_requires_a_published_set(self):
        with patch.object(verification, 'get_published_account_set_leaf_accounts', return_value=None):
            routing = verification.get_import_vendor_routing(7)
        self.assertEqual(routing['mode'], 'not_configured')
        self.assertEqual(routing['set_key'], 'import_vendors')

    def test_import_vendor_uses_its_own_published_account_codes(self):
        published = {'set_key': 'import_vendors', 'version_number': 1, 'accounts': [{'account_code': self.import_code}]}
        with patch.object(verification, 'get_published_account_set_leaf_accounts', return_value=published):
            routing = verification.get_import_vendor_routing(7)
        self.assertEqual(routing['account_codes'], {self.import_code})

    def test_import_vendor_jv_is_eligible_but_treasury_jv_is_excluded(self):
        cash = {'mode': 'account_set', 'account_codes': {'10-15-115-10-0001'}}
        bank = {'mode': 'account_set', 'account_codes': {'10-15-115-15-0001'}}
        self.assertTrue(verification.is_import_vendor_voucher(
            {'voucher_id': 'JV-1', 'rows': [{'account_code': self.import_code}, {'account_code': '50-10-0001'}]},
            self.import_code, cash, bank,
        ))
        self.assertFalse(verification.is_import_vendor_voucher(
            {'voucher_id': 'JV-2', 'rows': [{'account_code': self.import_code}, {'account_code': '10-15-115-15-0001'}]},
            self.import_code, cash, bank,
        ))

    def test_local_and_import_vendor_sets_remain_separate(self):
        def published(_client_id, set_key):
            return {'set_key': set_key, 'version_number': 1, 'accounts': [{'account_code': '20-15-50-0001' if set_key == 'local_vendors' else self.import_code}]}
        with patch.object(verification, 'get_published_account_set_leaf_accounts', side_effect=published):
            self.assertEqual(verification.get_local_vendor_routing(7)['account_codes'], {'20-15-50-0001'})
            self.assertEqual(verification.get_import_vendor_routing(7)['account_codes'], {self.import_code})


class LocalVendorWorkflowTests(unittest.TestCase):
    vendor_code = '20-15-50-0001'
    vendor_name = 'Local Leather Supplier'

    def _rows(self):
        return [
            {'id': 301, 'row_data': {'Voucher ID': 'JV-1', 'Account Code': self.vendor_code, 'Account Name': self.vendor_name, 'Debit': '0', 'Credit': '150'}, 'extract_date': '2026-01-10', 'extract_amount': 150},
            {'id': 302, 'row_data': {'Voucher ID': 'JV-1', 'Account Code': '50-10-0001', 'Account Name': 'Raw Material', 'Debit': '150', 'Credit': '0'}, 'extract_date': '2026-01-10', 'extract_amount': 150},
            {'id': 303, 'row_data': {'Voucher ID': 'JV-2', 'Account Code': self.vendor_code, 'Account Name': self.vendor_name, 'Debit': '25', 'Credit': '0'}, 'extract_date': '2026-01-12', 'extract_amount': 25},
            {'id': 304, 'row_data': {'Voucher ID': 'JV-2', 'Account Code': '50-10-0001', 'Account Name': 'Raw Material', 'Debit': '0', 'Credit': '25'}, 'extract_date': '2026-01-12', 'extract_amount': 25},
            {'id': 305, 'row_data': {'Voucher ID': 'BPV-3', 'Account Code': self.vendor_code, 'Account Name': self.vendor_name, 'Debit': '80', 'Credit': '0'}, 'extract_date': '2026-01-15', 'extract_amount': 80},
            {'id': 306, 'row_data': {'Voucher ID': 'BPV-3', 'Account Code': '10-15-115-15-0001', 'Account Name': 'Main Bank', 'Debit': '0', 'Credit': '80'}, 'extract_date': '2026-01-15', 'extract_amount': 80},
        ]

    def _treasury_routing(self, _client_id, workstream):
        return {'mode': 'account_set', 'account_codes': {'10-15-115-15-0001'} if workstream == 'bank' else set()}

    def test_discovery_lists_only_non_treasury_vendor_accounts(self):
        labels = verification.collect_local_vendor_labels(
            self._rows(), {'account_codes': {self.vendor_code}},
            self._treasury_routing(7, 'cash'), self._treasury_routing(7, 'bank'),
        )
        self.assertEqual(labels, {f'{self.vendor_code} :: {self.vendor_name}'})

    def test_session_captures_vendor_jvs_but_not_bank_payment(self):
        details = []

        def query(sql, _params):
            if 'FROM clients' in sql:
                return [{'id': 7}]
            if 'FROM client_data' in sql:
                return self._rows()
            raise AssertionError(f'Unexpected query: {sql}')

        def write(sql, _params):
            return 92 if 'INSERT INTO verification_sessions' in sql else []

        def batch_insert(_conn, _sql, params, template):
            self.assertIn('Pending', template)
            details.extend(params)
            return []

        with patch.object(verification, 'execute_query_pg', side_effect=query), patch.object(verification, 'execute_write_pg', side_effect=write), patch.object(verification, 'execute_batch_insert_with_ids_pg', side_effect=batch_insert), patch.object(verification, 'get_raw_conn_pg', return_value=object()), patch.object(verification, 'release_raw_conn_pg'), patch.object(verification, 'get_audit_rules_config_version', return_value=1), patch.object(verification, 'get_local_vendor_routing', return_value={'mode': 'account_set', 'account_codes': {self.vendor_code}}), patch.object(verification, 'get_treasury_routing', side_effect=self._treasury_routing), patch.object(verification, 'log_audit'):
            result = verification.start_verification_session(
                client_id=7, cashbook_name=f'{self.vendor_code} :: {self.vendor_name}',
                period_start='2026-01-01', period_end='2026-01-31', session_type='local_vendor',
            )

        self.assertEqual(result['total_transactions'], 2)
        self.assertEqual(details, [(92, 301, 'JV-1'), (92, 303, 'JV-2')])

    def test_preview_uses_vendor_bill_and_debit_note_signs(self):
        def query(sql, _params):
            if 'FROM clients' in sql:
                return [{'id': 7}]
            if 'FROM client_data' in sql:
                return self._rows()
            if 'FROM verification_details' in sql:
                return []
            raise AssertionError(f'Unexpected query: {sql}')

        with patch.object(verification, 'execute_query_pg', side_effect=query), patch.object(verification, 'get_local_vendor_routing', return_value={'mode': 'account_set', 'account_codes': {self.vendor_code}}), patch.object(verification, 'get_treasury_routing', side_effect=self._treasury_routing):
            result = verification.preview_cashbook(
                client_id=7, cashbook_name=f'{self.vendor_code} :: {self.vendor_name}',
                date_start='2026-01-01', date_end='2026-01-31', page=1, limit=50, session_type='local_vendor',
            )

        self.assertEqual(result['total_rows'], 2)
        self.assertEqual([row['amount'] for row in result['rows']], [150, -25])
        self.assertEqual([row['direction'] for row in result['rows']], ['Vendor bill / payable', 'Debit note / adjustment'])


    def test_latest_vendor_date_uses_vendor_candidates_not_a_date_by_date_scan(self):
        queries = []

        def query(sql, _params):
            queries.append(sql)
            if 'FROM clients' in sql:
                return [{'id': 7}]
            if 'SELECT id, document_group_id, row_data, extract_date' in sql:
                return self._rows()
            if 'SELECT id, row_data, extract_date FROM client_data' in sql:
                return self._rows()
            raise AssertionError(f'Unexpected query: {sql}')

        with patch.object(verification, 'execute_query_pg', side_effect=query), patch.object(verification, 'get_local_vendor_routing', return_value={'mode': 'account_set', 'account_codes': {self.vendor_code}}), patch.object(verification, 'get_treasury_routing', side_effect=self._treasury_routing):
            result = verification.get_latest_local_vendor_date(7)

        self.assertEqual(str(result['latest_date']), '2026-01-12')
        self.assertFalse(any('SELECT DISTINCT extract_date' in sql for sql in queries))
        self.assertEqual(sum('FROM client_data' in sql for sql in queries), 2)


class ImportVendorWorkflowTests(unittest.TestCase):
    vendor_code = '20-15-45-10-0001'
    vendor_name = 'Imported Leather Supplier'

    def _rows(self):
        return [
            {'id': 301, 'row_data': {'Voucher ID': 'JV-1', 'Account Code': self.vendor_code, 'Account Name': self.vendor_name, 'Debit': '0', 'Credit': '150'}, 'extract_date': '2026-01-10', 'extract_amount': 150},
            {'id': 302, 'row_data': {'Voucher ID': 'JV-1', 'Account Code': '50-10-0001', 'Account Name': 'Raw Material', 'Debit': '150', 'Credit': '0'}, 'extract_date': '2026-01-10', 'extract_amount': 150},
            {'id': 303, 'row_data': {'Voucher ID': 'JV-2', 'Account Code': self.vendor_code, 'Account Name': self.vendor_name, 'Debit': '25', 'Credit': '0'}, 'extract_date': '2026-01-12', 'extract_amount': 25},
            {'id': 304, 'row_data': {'Voucher ID': 'JV-2', 'Account Code': '50-10-0001', 'Account Name': 'Raw Material', 'Debit': '0', 'Credit': '25'}, 'extract_date': '2026-01-12', 'extract_amount': 25},
            {'id': 305, 'row_data': {'Voucher ID': 'BPV-3', 'Account Code': self.vendor_code, 'Account Name': self.vendor_name, 'Debit': '80', 'Credit': '0'}, 'extract_date': '2026-01-15', 'extract_amount': 80},
            {'id': 306, 'row_data': {'Voucher ID': 'BPV-3', 'Account Code': '10-15-115-15-0001', 'Account Name': 'Main Bank', 'Debit': '0', 'Credit': '80'}, 'extract_date': '2026-01-15', 'extract_amount': 80},
        ]

    def _treasury_routing(self, _client_id, workstream):
        return {'mode': 'account_set', 'account_codes': {'10-15-115-15-0001'} if workstream == 'bank' else set()}

    def test_discovery_lists_only_non_treasury_import_vendor_accounts(self):
        def query(sql, _params):
            if 'FROM clients' in sql:
                return [{'id': 7}]
            if 'SELECT id, document_group_id, row_data, extract_date' in sql:
                return self._rows()
            if 'SELECT id, row_data, extract_date FROM client_data' in sql:
                return self._rows()
            raise AssertionError(f'Unexpected query: {sql}')

        with patch.object(verification, 'execute_query_pg', side_effect=query), patch.object(verification, 'get_import_vendor_routing', return_value={'mode': 'account_set', 'account_codes': {self.vendor_code}}), patch.object(verification, 'get_treasury_routing', side_effect=self._treasury_routing):
            result = verification.get_import_vendors(7, '2026-01-01', '2026-01-31')

        self.assertEqual(result, {'vendors': [f'{self.vendor_code} :: {self.vendor_name}'], 'count': 1, 'setup_required': False})

    def test_session_captures_import_vendor_jvs_but_not_bank_payment(self):
        details = []

        def query(sql, _params):
            if 'FROM clients' in sql:
                return [{'id': 7}]
            if 'FROM client_data' in sql:
                return self._rows()
            raise AssertionError(f'Unexpected query: {sql}')

        def write(sql, _params):
            return 93 if 'INSERT INTO verification_sessions' in sql else []

        def batch_insert(_conn, _sql, params, template):
            details.extend(params)
            return []

        with patch.object(verification, 'execute_query_pg', side_effect=query), patch.object(verification, 'execute_write_pg', side_effect=write), patch.object(verification, 'execute_batch_insert_with_ids_pg', side_effect=batch_insert), patch.object(verification, 'get_raw_conn_pg', return_value=object()), patch.object(verification, 'release_raw_conn_pg'), patch.object(verification, 'get_audit_rules_config_version', return_value=1), patch.object(verification, 'get_import_vendor_routing', return_value={'mode': 'account_set', 'account_codes': {self.vendor_code}}), patch.object(verification, 'get_treasury_routing', side_effect=self._treasury_routing), patch.object(verification, 'log_audit'):
            result = verification.start_verification_session(
                client_id=7, cashbook_name=f'{self.vendor_code} :: {self.vendor_name}',
                period_start='2026-01-01', period_end='2026-01-31', session_type='import_vendor',
            )

        self.assertEqual(result['total_transactions'], 2)
        self.assertEqual(details, [(93, 301, 'JV-1'), (93, 303, 'JV-2')])

    def test_preview_uses_import_vendor_bill_and_debit_note_signs(self):
        def query(sql, _params):
            if 'FROM clients' in sql:
                return [{'id': 7}]
            if 'FROM client_data' in sql:
                return self._rows()
            if 'FROM verification_details' in sql:
                return []
            raise AssertionError(f'Unexpected query: {sql}')

        with patch.object(verification, 'execute_query_pg', side_effect=query), patch.object(verification, 'get_import_vendor_routing', return_value={'mode': 'account_set', 'account_codes': {self.vendor_code}}), patch.object(verification, 'get_treasury_routing', side_effect=self._treasury_routing):
            result = verification.preview_cashbook(
                client_id=7, cashbook_name=f'{self.vendor_code} :: {self.vendor_name}',
                date_start='2026-01-01', date_end='2026-01-31', page=1, limit=50, session_type='import_vendor',
            )

        self.assertEqual(result['total_rows'], 2)
        self.assertEqual([row['amount'] for row in result['rows']], [150, -25])
        self.assertEqual([row['direction'] for row in result['rows']], ['Import vendor bill / payable', 'Import vendor debit note / adjustment'])

    def test_latest_import_vendor_date_uses_vendor_candidates_not_a_date_by_date_scan(self):
        queries = []

        def query(sql, _params):
            queries.append(sql)
            if 'FROM clients' in sql:
                return [{'id': 7}]
            if 'SELECT id, document_group_id, row_data, extract_date' in sql:
                return self._rows()
            if 'SELECT id, row_data, extract_date FROM client_data' in sql:
                return self._rows()
            raise AssertionError(f'Unexpected query: {sql}')

        with patch.object(verification, 'execute_query_pg', side_effect=query), patch.object(verification, 'get_import_vendor_routing', return_value={'mode': 'account_set', 'account_codes': {self.vendor_code}}), patch.object(verification, 'get_treasury_routing', side_effect=self._treasury_routing):
            result = verification.get_latest_import_vendor_date(7)

        self.assertEqual(str(result['latest_date']), '2026-01-12')
        self.assertFalse(any('SELECT DISTINCT extract_date' in sql for sql in queries))
        self.assertEqual(sum('FROM client_data' in sql for sql in queries), 2)


class ImportVendorUiContractTests(unittest.TestCase):
    def test_import_vendor_uses_its_own_live_workstream_contract(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'static' / 'verification.js').read_text(encoding='utf-8')

        self.assertIn("import_vendor: { title: 'Import Vendor'", source)
        self.assertIn("function isImportVendorVerification() { return currentType === 'import_vendor'; }", source)
        self.assertIn('/api/verification/latest_import_vendor_date?client_id=', source)
        self.assertIn('/api/verification/import_vendors?', source)
        self.assertIn("session_type: currentType", source)

    def test_import_vendor_state_tile_navigation_and_signed_debit_note_are_live(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'static' / 'verification.js').read_text(encoding='utf-8')
        html = (root / 'static' / 'verification.html').read_text(encoding='utf-8')
        navigation = (root / 'static' / 'client_context.js').read_text(encoding='utf-8')

        self.assertIn("VERIFICATION_DATE_RANGE_STORAGE_KEY + ':import_vendor'", source)
        self.assertIn("VERIFICATION_SELECTED_CASHBOOK_STORAGE_KEY + ':import_vendor'", source)
        self.assertIn("'Import vendor debit note / adjustment'", source)
        self.assertIn('id="importVendorTile" data-verification-type="import_vendor"', html)
        self.assertIn('Cash/Bank payments remain in Treasury verification.', html)
        self.assertIn("{ label: 'Import vendor', url: '/static/verification.html?type=import_vendor', state: 'ready' }", navigation)


class LocalVendorUiContractTests(unittest.TestCase):
    def test_local_vendor_type_uses_its_own_discovery_contract_and_safe_range_boundary(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'static' / 'verification.js').read_text(encoding='utf-8')

        self.assertIn("local_vendor: { title: 'Local Vendor'", source)
        self.assertIn('/api/verification/latest_local_vendor_date', source)
        self.assertIn('/api/verification/local_vendors?', source)
        self.assertIn("session_type: currentType", source)
        self.assertIn("currentSessionId && isLocalVendorVerification()", source)
        self.assertIn('Date-range changes for an active Local Vendor session', source)

    def test_local_vendor_date_state_isolated_from_cash_and_bank(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'static' / 'verification.js').read_text(encoding='utf-8')

        self.assertIn('function verificationDateRangeStorageKey()', source)
        self.assertIn("VERIFICATION_DATE_RANGE_STORAGE_KEY + ':local_vendor'", source)
        self.assertIn('sessionStorage.setItem(verificationDateRangeStorageKey()', source)
        self.assertIn('sessionStorage.getItem(verificationDateRangeStorageKey())', source)

    def test_local_vendor_hub_tile_and_global_navigation_are_live(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'static' / 'verification.html').read_text(encoding='utf-8')
        navigation = (root / 'static' / 'client_context.js').read_text(encoding='utf-8')

        self.assertIn('id="localVendorTile" data-verification-type="local_vendor"', html)
        self.assertIn('Cash/Bank payments remain in Treasury verification.', html)
        self.assertIn("{ label: 'Local vendor', url: '/static/verification.html?type=local_vendor', state: 'ready' }", navigation)
