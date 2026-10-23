"""Isolated temporary export jobs; no application database or private files."""
import threading
import time

import pytest

from services.reconciliation_export_jobs import ReconciliationExportJobs, ExportJobError, _lock


SCOPE = dict(client_id=7, period_start='2026-01-01', period_end='2026-01-31',
             treasury_view='combined', group_by='month', expected_identity='a'*64, expected_fingerprint='b'*64)


def test_default_storage_separates_actual_os_accounts_even_with_the_same_environment(tmp_path, monkeypatch):
    import os
    import services.reconciliation_export_jobs as jobs
    monkeypatch.setattr(jobs.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setenv('USERNAME', 'same-desktop-user')
    account = ['sandbox-account' if os.name == 'nt' else 1001]
    monkeypatch.setattr(jobs.os, 'getlogin' if os.name == 'nt' else 'getuid', lambda: account[0])
    sandbox = ReconciliationExportJobs()
    account[0] = 'server-account' if os.name == 'nt' else 1002
    server = ReconciliationExportJobs()
    another_server = ReconciliationExportJobs()
    assert sandbox.root != server.root
    assert server.root == another_server.root
    assert sandbox.root.parent == server.root.parent == tmp_path.resolve()
    job = server.submit(SCOPE, 1)
    assert another_server.status(job['id'], 1, 7)['state'] == 'queued'
    with pytest.raises(ExportJobError):
        sandbox.status(job['id'], 1, 7)


def test_storage_access_failure_reports_service_unavailable_without_exposing_private_paths(tmp_path, monkeypatch):
    from pathlib import Path
    def denied(*args, **kwargs):
        raise PermissionError('private system path')
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(ExportJobError) as error:
        with _lock(tmp_path/'state.lock'):
            pytest.fail('Inaccessible storage must not acquire a lock')
    assert error.value.status == 503
    assert 'Private export storage is unavailable' in error.value.detail
    assert 'private system path' not in error.value.detail


def wait(manager, identity, states=('ready', 'failed', 'cancelled')):
    for _ in range(300):
        result = manager.status(identity, 1, 7)
        if result['state'] in states:
            return result
        time.sleep(.01)
    pytest.fail('Fixture worker did not reach the expected state')


def write_fixture(scope, path, progress):
    progress('Writing source evidence', 250, 500)
    path.write_bytes(b'complete-fixture')
    return 'Reconciliation.xlsx'


def test_worker_recovers_after_a_temporary_storage_access_failure(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import services.reconciliation_export_jobs as jobs
    manager = ReconciliationExportJobs(tmp_path/'exports')
    job = manager.submit(SCOPE, 1)
    attempted = []
    @contextmanager
    def interrupted(path, **kwargs):
        if path.name == 'worker.lock' and not attempted:
            attempted.append(True)
            raise ExportJobError(503, 'Temporary storage interruption')
        with _lock(path, **kwargs) as acquired:
            yield acquired
    monkeypatch.setattr(jobs, '_lock', interrupted)
    manager.start(write_fixture)
    try:
        assert wait(manager, job['id'])['state'] == 'ready'
        assert attempted == [True] and manager._thread.is_alive()
    finally:
        assert manager.stop()


def test_jobs_deduplicate_scope_and_keep_owner_files_private(tmp_path):
    manager = ReconciliationExportJobs(tmp_path/'exports')
    manager.start(write_fixture)
    try:
        job = manager.submit(SCOPE, 1)
        assert wait(manager, job['id'])['state'] == 'ready'
        assert manager.submit(SCOPE, 1)['id'] == job['id']
        assert manager.submit(SCOPE, 2)['id'] != job['id']
        for owner, client in [(2,7),(1,8)]:
            for action in (manager.status,manager.cancel,manager.acquire_download):
                with pytest.raises(ExportJobError) as error:
                    action(job['id'],owner,client)
                assert error.value.status == 404
        path, filename, scope, lease = manager.acquire_download(job['id'],1,7)
        assert path.read_bytes() == b'complete-fixture' and filename.endswith('.xlsx')
        assert scope == SCOPE
        manager.release_download(job['id'],lease)
        for path in manager.root.rglob('state.json'):
            text = path.read_text()
            assert 'complete-fixture' not in text and 'debit' not in text
    finally:
        assert manager.stop()


def test_jobs_bound_queue_and_cancel_before_or_during_generation(tmp_path):
    manager = ReconciliationExportJobs(tmp_path/'exports')
    started, release = threading.Event(), threading.Event()
    calls = []
    def processor(scope,path,progress):
        calls.append(scope['period_start']); started.set(); release.wait(3)
        progress('Writing evidence',0,1)
        return write_fixture(scope,path,progress)
    manager.start(processor)
    try:
        first = manager.submit(SCOPE,1)
        assert started.wait(2)
        second = manager.submit(SCOPE|{'period_start':'2026-01-02'},1)
        third = manager.submit(SCOPE|{'period_start':'2026-01-03'},1)
        with pytest.raises(ExportJobError) as error:
            manager.submit(SCOPE|{'period_start':'2026-01-04'},1)
        assert error.value.status == 429
        assert manager.submit(SCOPE,1)['id'] == first['id']
        assert manager.cancel(second['id'],1,7)['state'] == 'cancelled'
        assert manager.cancel(first['id'],1,7)['state'] == 'cancelling'
        release.set()
        assert wait(manager,first['id'])['state'] == 'cancelled'
        assert wait(manager,third['id'])['state'] == 'ready'
        assert calls == ['2026-01-01','2026-01-03']
        assert not (manager._directory(first['id'])/'result.partial').exists()
    finally:
        release.set(); assert manager.stop()


@pytest.mark.parametrize('failure', ['exception','timeout','capacity'])
def test_job_failures_discard_partial_files_and_allow_a_fresh_retry(tmp_path,failure):
    now = [1000.]
    manager = ReconciliationExportJobs(tmp_path/'exports',clock=lambda:now[0],max_job_bytes=4096)
    def processor(scope,path,progress):
        path.write_bytes(b'x'*(5000 if failure=='capacity' else 20))
        if failure == 'exception':
            raise ValueError('private source details must not enter the API error')
        if failure == 'timeout':
            now[0] += 601
        progress('Writing evidence',1,1)
        return 'Fixture.xlsx'
    manager.start(processor)
    try:
        job = manager.submit(SCOPE,1); status = wait(manager,job['id'])
        assert status['state'] == 'failed'
        assert status['error_status'] == {'exception':500,'timeout':408,'capacity':413}[failure]
        assert 'private source details' not in status['error']
        assert list(manager._directory(job['id']).iterdir()) == [manager._directory(job['id'])/'state.json']
        assert manager.submit(SCOPE,1)['id'] != job['id']
    finally:
        assert manager.stop()


def test_expiry_cleanup_respects_download_leases_and_removes_finished_files(tmp_path):
    now=[1000.]
    manager=ReconciliationExportJobs(tmp_path/'exports',clock=lambda:now[0],lifetime=1)
    manager.start(write_fixture)
    try:
        job=manager.submit(SCOPE,1);wait(manager,job['id'])
        path,_,_,lease=manager.acquire_download(job['id'],1,7)
        now[0]+=2
        with _lock(manager.root/'state.lock'):
            manager._cleanup()
        assert path.exists()
        with pytest.raises(ExportJobError) as error:
            manager.status(job['id'],1,7)
        assert error.value.status==410
        manager.release_download(job['id'],lease)
        with _lock(manager.root/'state.lock'):
            manager._cleanup()
        assert not path.parent.exists()
    finally:
        assert manager.stop()


def test_expired_queued_tickets_cannot_permanently_fill_the_waiting_queue(tmp_path):
    now=[1000.]
    manager=ReconciliationExportJobs(tmp_path/'exports',clock=lambda:now[0],lifetime=1)
    manager.submit(SCOPE,1)
    manager.submit(SCOPE|{'period_start':'2026-01-02'},1)
    now[0]+=2
    job=manager.submit(SCOPE,1)
    assert job['state']=='queued'
    assert len(list(manager.root.glob('job-*')))==1


def test_worker_lock_allows_only_one_generator_across_app_instances(tmp_path):
    first=ReconciliationExportJobs(tmp_path/'exports')
    second=ReconciliationExportJobs(tmp_path/'exports')
    lock=threading.Lock(); active=[0]; peak=[0]
    def processor(scope,path,progress):
        with lock:
            active[0]+=1;peak[0]=max(peak[0],active[0])
        try:
            time.sleep(.08)
            return write_fixture(scope,path,progress)
        finally:
            with lock:active[0]-=1
    first.start(processor);second.start(processor)
    try:
        job=first.submit(SCOPE,1)
        wait(first,job['id'],states=('running','ready'))
        other=second.submit(SCOPE|{'period_start':'2026-01-02'},1)
        wait(second,other['id'])
        assert peak[0]==1
        assert second.status(job['id'],1,7)['state']=='ready'
    finally:
        assert first.stop() and second.stop()


def test_restart_marks_interrupted_build_failed_and_removes_partial_artifacts(tmp_path):
    manager=ReconciliationExportJobs(tmp_path/'exports')
    job=manager.submit(SCOPE,1)
    with _lock(manager.root/'state.lock'):
        metadata=manager._read(job['id']);metadata['state']='running';manager._write(metadata)
        (manager._directory(job['id'])/'result.partial').write_bytes(b'incomplete')
    manager.start(write_fixture)
    try:
        status=wait(manager,job['id'])
        assert status['state']=='failed' and status['error_status']==410
        assert not (manager._directory(job['id'])/'result.partial').exists()
    finally:
        assert manager.stop()


def test_shutdown_cancels_remaining_work_and_preserves_the_running_slot_until_acknowledged(tmp_path):
    manager=ReconciliationExportJobs(tmp_path/'exports')
    gate=threading.Event();started=threading.Event()
    def processor(scope,path,progress):
        path.write_bytes(b'partial');started.set();gate.wait(3)
        progress('Writing evidence',1,1)
        return 'Fixture.xlsx'
    manager.start(processor)
    job=manager.submit(SCOPE,1);assert started.wait(2)
    assert not manager.stop(timeout=.01)
    gate.set();assert manager.stop()
    assert manager.status(job['id'],1,7)['state']=='cancelled'
    assert not (manager._directory(job['id'])/'result.partial').exists()


@pytest.mark.parametrize('identity',['../outside','bad','0'*32])
def test_unknown_or_invalid_job_paths_never_reach_other_files(tmp_path,identity):
    manager=ReconciliationExportJobs(tmp_path/'exports')
    outside=tmp_path/'outside';outside.write_text('preserve')
    with pytest.raises(ExportJobError):manager.status(identity,1,7)
    assert outside.read_text()=='preserve'


@pytest.mark.parametrize('stopped', [True, False])
def test_application_lifecycle_starts_export_worker_and_closes_pool_only_after_it_stops(stopped):
    from unittest.mock import Mock, patch
    import main
    manager=Mock();manager.stop.return_value=stopped
    with patch.object(main,'init_db') as init, patch.object(main.reconciliation,'export_jobs',manager), \
            patch.dict('os.environ',{'DATABASE_URL':''}), patch('database.close_pg_pool') as close_pool:
        main.startup();main.shutdown()
    init.assert_called_once()
    manager.start.assert_called_once_with(main.reconciliation._prepare_reconciliation_export)
    manager.stop.assert_called_once()
    assert close_pool.call_count==int(stopped)
