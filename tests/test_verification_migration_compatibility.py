"""Schema-capability checks without importing the app or using a real database."""
import ast
from functools import lru_cache
from pathlib import Path

import pytest


def load_predicate(rows):
    source = Path(__file__).resolve().parents[1] / 'routers' / 'verification.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_legacy_verification_conflict_predicate_pg')
    calls = []

    def query(sql):
        calls.append(sql)
        return rows

    namespace = {'lru_cache': lru_cache, 'execute_query_pg': query}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[function.name], calls


def test_original_schema_keeps_original_conflict_target():
    predicate, _ = load_predicate([])
    assert predicate() == ''


def test_migrated_schema_selects_legacy_namespace_and_checks_catalog_once():
    predicate, calls = load_predicate([
        {'table_name': table, 'data_type': 'text'} for table in
        ('voucher_line_observations', 'voucher_line_procedure_checks', 'voucher_check_status')])
    assert predicate() == " WHERE rulebook_source = 'legacy'"
    assert predicate() == " WHERE rulebook_source = 'legacy'"
    assert len(calls) == 1


@pytest.mark.parametrize('rows', [
    [{'table_name': 'voucher_line_observations', 'data_type': 'text'}],
    [{'table_name': table, 'data_type': 'integer'} for table in
     ('voucher_line_observations', 'voucher_line_procedure_checks', 'voucher_check_status')],
])
def test_partial_or_incompatible_schema_blocks_writes(rows):
    predicate, _ = load_predicate(rows)
    with pytest.raises(RuntimeError, match='partially migrated'):
        predicate()
