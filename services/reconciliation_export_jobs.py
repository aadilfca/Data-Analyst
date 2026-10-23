"""Bounded, owner-scoped Cashflow exports; financial rows never enter job metadata.

An OS-held worker lock permits one generator across web processes on this host.
Atomic compact metadata lets any local web worker serve progress/cancellation.
Only XLSX/XML artifacts live in the private temporary directory, with expiry.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

logger = logging.getLogger(__name__)
_JOB_ID = re.compile(r'^[0-9a-f]{32}$')
_TERMINAL = {'ready', 'failed', 'cancelled'}


class ExportJobError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


@contextmanager
def _lock(path, *, wait=True):
    """Cross-process advisory lock, released by the OS even after a crash."""
    try:
        handle = path.open('a+b')
    except OSError as error:
        raise ExportJobError(503, 'Private export storage is unavailable. Please contact the server administrator.') from error
    acquired = False
    deadline = time.monotonic() + (5 if wait else 0)
    try:
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    if wait:
                        raise ExportJobError(503, 'Export service is busy; please retry shortly.')
                    break
                time.sleep(.02)
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class ReconciliationExportJobs:
    def __init__(self, root=None, *, lifetime=1800, max_seconds=600, max_waiting=2,
                 max_disk_bytes=512*1024*1024, max_job_bytes=256*1024*1024, clock=time.time):
        if root is None:
            # Environment USERNAME can identify the desktop user even inside a
            # different Windows account. Keep each actual OS account's ACL private.
            account = os.getlogin().casefold() if os.name == 'nt' else str(os.getuid())
            identity = str(Path(__file__).resolve().parents[1]) + '\0' + account
            identifier = hashlib.sha256(identity.encode()).hexdigest()[:16]
            root = Path(tempfile.gettempdir()) / ('system-analyst-exports-' + identifier)
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lifetime, self.max_seconds, self.max_waiting = lifetime, max_seconds, max_waiting
        self.max_disk_bytes, self.max_job_bytes, self.clock = max_disk_bytes, max_job_bytes, clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._start_lock = threading.Lock()

    def _directory(self, identity):
        if not _JOB_ID.fullmatch(identity):
            raise ExportJobError(404, 'Export not found.')
        path = self.root / ('job-' + identity)
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ExportJobError(404, 'Export not found.')
        return path

    def _read(self, identity):
        path = self._directory(identity) / 'state.json'
        if not path.is_file() or path.is_symlink():
            raise ExportJobError(410, 'Export expired or the server restarted. Please prepare it again.')
        return json.loads(path.read_text(encoding='utf-8'))

    def _write(self, job):
        directory = self._directory(job['id'])
        temporary = directory / 'state.tmp'
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, directory / 'state.json')

    def _all(self):
        for path in self.root.glob('job-*'):
            identity = path.name[4:]
            if _JOB_ID.fullmatch(identity) and not path.is_symlink():
                try:
                    yield self._read(identity)
                except (ExportJobError, ValueError, OSError):
                    logger.warning('Unreadable temporary export metadata: %s', identity)

    def _owned(self, identity, owner, client_id):
        job = self._read(identity)
        if job['owner'] != owner or job['scope']['client_id'] != client_id:
            raise ExportJobError(404, 'Export not found.')
        return job

    def _artifacts(self, job):
        directory = self._directory(job['id'])
        for path in directory.iterdir():
            if path.name in ('state.json', 'state.tmp'):
                continue
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    def _bytes(self, directory):
        return sum(path.stat().st_size for path in directory.rglob('*')
                   if path.is_file() and not path.is_symlink())

    def _cleanup(self):
        now = self.clock()
        for job in list(self._all()):
            if job['state'] not in _TERMINAL | {'queued'} or job['expires'] > now:
                continue
            if any(until > now for until in job.get('downloads', {}).values()):
                continue
            try:
                shutil.rmtree(self._directory(job['id']))
            except OSError:
                logger.warning('Export cleanup deferred: %s', job['id'])

    @staticmethod
    def _public(job):
        return {key: job.get(key) for key in ('id', 'state', 'stage', 'current', 'total',
            'error', 'error_status', 'expires', 'filename')}

    def submit(self, scope, owner):
        with _lock(self.root / 'state.lock'):
            self._cleanup()
            jobs = list(self._all())
            # Idempotent retries never launch a second build of the same evidence.
            for job in jobs:
                if (job['owner'] == owner and job['scope'] == scope
                        and job['state'] in ('queued', 'running', 'ready') and job['expires'] > self.clock()):
                    return self._public(job)
            if sum(job['state'] == 'queued' for job in jobs) >= self.max_waiting:
                raise ExportJobError(429, 'Two exports are already waiting. Please retry after one finishes.')
            if len(jobs) >= 32 or self._bytes(self.root) >= self.max_disk_bytes:
                raise ExportJobError(429, 'Temporary export storage is busy. Please retry after cleanup.')
            identity = uuid.uuid4().hex
            self._directory(identity).mkdir(mode=0o700)
            now = self.clock()
            job = dict(id=identity, owner=owner, scope=dict(scope), state='queued', stage='Waiting to prepare export',
                       current=0, total=None, created=now, expires=now+self.lifetime, downloads={})
            self._write(job)
        self._wake.set()
        return self._public(job)

    def status(self, identity, owner, client_id):
        with _lock(self.root / 'state.lock'):
            job = self._owned(identity, owner, client_id)
            if job['expires'] <= self.clock():
                raise ExportJobError(410, 'Export expired. Please prepare it again.')
            return self._public(job)

    def cancel(self, identity, owner, client_id):
        with _lock(self.root / 'state.lock'):
            job = self._owned(identity, owner, client_id)
            job['state'] = 'cancelling' if job['state'] in ('running', 'cancelling') else 'cancelled'
            job['stage'] = 'Cancellation requested' if job['state'] == 'cancelling' else 'Export cancelled'
            self._write(job)
            if job['state'] == 'cancelled' and not job.get('downloads'):
                self._artifacts(job)
            return self._public(job)

    def acquire_download(self, identity, owner, client_id):
        with _lock(self.root / 'state.lock'):
            job = self._owned(identity, owner, client_id)
            if job['expires'] <= self.clock():
                raise ExportJobError(410, 'Export expired. Please prepare it again.')
            if job['state'] != 'ready':
                raise ExportJobError(409, 'Export is not ready to download.')
            token = uuid.uuid4().hex
            job['downloads'] = {key: until for key, until in job.get('downloads', {}).items() if until > self.clock()}
            if len(job['downloads']) >= 4:
                raise ExportJobError(429, 'This file is already being downloaded. Please wait.')
            path = self._directory(identity) / 'result.xlsx'
            if not path.is_file() or path.is_symlink():
                raise ExportJobError(410, 'Export file is unavailable. Please prepare it again.')
            job['downloads'][token] = self.clock() + 600
            self._write(job)
            return path, job['filename'], dict(job['scope']), token

    def release_download(self, identity, token):
        with _lock(self.root / 'state.lock'):
            try:
                job = self._read(identity)
            except ExportJobError:
                return
            job['downloads'].pop(token, None)
            self._write(job)
            if job['state'] == 'cancelled' and not job['downloads']:
                self._artifacts(job)

    def start(self, processor):
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, args=(processor,),
                                            name='cashflow-export-worker', daemon=True)
            self._thread.start()

    def stop(self, timeout=10):
        self._stop.set(); self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        return not self._thread or not self._thread.is_alive()

    def _loop(self, processor):
        while not self._stop.is_set():
            try:
                with _lock(self.root / 'worker.lock', wait=False) as leader:
                    if leader:
                        # Holding the OS lock proves a previous generator is no longer running.
                        with _lock(self.root / 'state.lock'):
                            for job in list(self._all()):
                                if job['state'] in ('running', 'cancelling'):
                                    job.update(state='failed', error='Server restarted; please prepare the export again.', error_status=410)
                                    self._artifacts(job); self._write(job)
                        while not self._stop.is_set():
                            with _lock(self.root / 'state.lock'):
                                self._cleanup()
                                queued = sorted((job for job in self._all() if job['state'] == 'queued' and job['expires'] > self.clock()), key=lambda job: job['created'])
                                job = queued[0] if queued else None
                                if job:
                                    job.update(state='running', started=self.clock(), stage='Reading and validating evidence',
                                               expires=self.clock()+self.max_seconds+self.lifetime)
                                    self._write(job)
                            if job:
                                self._run(job, processor)
                            else:
                                self._wake.wait(.5); self._wake.clear()
            except (ExportJobError, OSError):
                logger.exception('Private export storage is unavailable; worker will retry.')
            self._stop.wait(.5)

    def _run(self, job, processor):
        identity = job['id']
        directory = self._directory(identity)

        def progress(stage, current=0, total=None):
            with _lock(self.root / 'state.lock'):
                latest = self._read(identity)
                if self._stop.is_set() or latest['state'] == 'cancelling':
                    raise ExportJobError(499, 'Export cancelled.')
                if self.clock() - job['started'] > self.max_seconds:
                    raise ExportJobError(408, 'Export exceeded its preparation limit. Please retry with a shorter period.')
                if self._bytes(directory) > self.max_job_bytes or self._bytes(self.root) > self.max_disk_bytes:
                    raise ExportJobError(413, 'Export exceeds temporary storage capacity; no evidence was truncated.')
                latest.update(stage=stage, current=current, total=total)
                self._write(latest)

        try:
            progress('Reading and validating evidence')
            filename = processor(dict(job['scope']), directory / 'result.partial', progress)
            progress('Finalizing Excel file')
            if Path(filename).name != filename or not filename.endswith('.xlsx'):
                raise ValueError('Invalid export filename')
            with _lock(self.root / 'state.lock'):
                latest = self._read(identity)
                if latest['state'] == 'cancelling' or self._stop.is_set():
                    raise ExportJobError(499, 'Export cancelled.')
                os.replace(directory / 'result.partial', directory / 'result.xlsx')
                latest.update(state='ready', stage='Ready to download', filename=filename, expires=self.clock()+self.lifetime)
                self._write(latest)
        except Exception as error:
            status = getattr(error, 'status', getattr(error, 'status_code', 500))
            detail = getattr(error, 'detail', None)
            if status == 500:
                logger.exception('Cashflow export failed: %s', identity)
                detail = 'The export could not be prepared. Please retry or report this export error.'
            with _lock(self.root / 'state.lock'):
                latest = self._read(identity)
                latest.update(state='cancelled' if status == 499 else 'failed', error=detail or str(error),
                              error_status=status, expires=self.clock()+self.lifetime)
                self._artifacts(latest); self._write(latest)


export_jobs = ReconciliationExportJobs()
