"""Restartable scheduled employees, coordinated with persistent expiring leases."""
import json
import time
import threading
import uuid
import sqlite3

from .db import now
from .tools import ToolError

KINDS={'check','security','performance','documentation','report','work','customers'}


class Workers:
    def __init__(self, company):
        self.co,self.db=company,company.db
        self.owner=uuid.uuid4().hex
        self._stop=threading.Event()
        self._thread=None

    def add(self,name,project_id,kind,interval_seconds,config=None):
        self.co.pipeline.project(project_id)
        if kind not in KINDS or not isinstance(interval_seconds,int) or interval_seconds<10:
            raise ValueError('Use a supported worker kind and an interval of at least 10 seconds.')
        config=config or {}
        if not isinstance(config,dict) or len(json.dumps(config))>16000:raise ValueError('Worker configuration must be a bounded object.')
        if kind in ('check','performance','documentation') and not config.get('command'):
            raise ValueError('Check workers require a company-configured command.')
        if 'command' in config and (not isinstance(config['command'],str) or not config['command'].strip() or len(config['command'])>8000):
            raise ValueError('Worker command must be bounded nonempty text.')
        if not isinstance(name,str) or not name.strip() or len(name)>100:
            raise ValueError('Worker needs a name of at most 100 characters.')
        try:
            job=self.db.run('INSERT INTO worker_jobs(name,project_id,kind,config,interval_seconds,next_run) VALUES(?,?,?,?,?,?)',
                             name,project_id,kind,json.dumps(config),interval_seconds,time.time())
        except sqlite3.IntegrityError as exc:
            raise ValueError('Worker names must be unique within the company.') from exc
        return self.db.one('SELECT * FROM worker_jobs WHERE id=?',job)

    def list(self):
        return self.db.all('SELECT * FROM worker_jobs ORDER BY id')

    def enable(self,job_id,enabled):
        if not self.db.one('SELECT id FROM worker_jobs WHERE id=?',job_id):
            raise ValueError('Unknown worker.')
        self.db.run('UPDATE worker_jobs SET enabled=? WHERE id=?',int(enabled),job_id)

    def tick(self,limit=5):
        if not isinstance(limit,int) or not 1<=limit<=100:
            raise ValueError('Worker tick limit must be 1–100.')
        results=[]
        for _ in range(limit):
            with self.db.lock:
                self.db.conn.execute('BEGIN IMMEDIATE')
                try:
                    job=self.db.one('SELECT * FROM worker_jobs WHERE enabled=1 AND next_run<=? AND lease_until<=? ORDER BY next_run,id LIMIT 1',time.time(),time.time())
                    if job:
                        self.db.run("UPDATE worker_runs SET status='abandoned',finished_at=? WHERE job_id=? AND status='running'",now(),job['id'])
                        lease=max(60,self.co.s.command_timeout+120)
                        self.db.run('UPDATE worker_jobs SET lease_until=?,lease_owner=? WHERE id=?',time.time()+lease,self.owner,job['id'])
                    self.db.conn.execute('COMMIT')
                except BaseException:
                    self.db.conn.execute('ROLLBACK');raise
            if not job:break
            heartbeat_stop=threading.Event()
            def renew(job_id=job['id']):
                while not heartbeat_stop.wait(20):
                    self.db.run('UPDATE worker_jobs SET lease_until=? WHERE id=? AND lease_owner=?',
                                time.time()+max(60,self.co.s.command_timeout+120),job_id,self.owner)
            run=self.db.run('INSERT INTO worker_runs(job_id,status,started_at) VALUES(?,?,?)',job['id'],'running',now())
            heartbeat=threading.Thread(target=renew,daemon=True);heartbeat.start()
            try:
                result=self._execute(job)
                status='passed'
                failures=0
            except Exception as exc:
                result={'error':f'{type(exc).__name__}: {exc}'}
                status='failed';failures=job['failures']+1
            finally:
                heartbeat_stop.set();heartbeat.join(timeout=1)
            from .observability import sanitize
            self.db.run('UPDATE worker_runs SET status=?,result=?,finished_at=? WHERE id=?',status,json.dumps(sanitize(result)),now(),run)
            delay=job['interval_seconds']*2**min(3,failures)
            self.db.run('UPDATE worker_jobs SET next_run=?,lease_until=0,lease_owner=NULL,failures=? WHERE id=? AND lease_owner=?',
                        time.time()+delay,failures,job['id'],self.owner)
            results.append(self.db.one('SELECT * FROM worker_runs WHERE id=?',run))
        return results

    def _execute(self,job):
        config=json.loads(job['config']);pid=job['project_id'];project=self.co.pipeline.project(pid)
        if job['kind']=='work':
            return self.co.pipeline.advance(pid,max_cycles=max(1,min(100,int(config.get('cycles',5)))),max_seconds=300)
        if job['kind']=='report':
            self.co.pipeline.queue_report(pid,'scheduled employee','Workers')
            self.co.pipeline.write_reports(pid)
            return self.db.one('SELECT * FROM reports WHERE project_id=? ORDER BY id DESC',pid)
        if job['kind']=='customers':
            return self.co.assessments.customers(pid,config.get('journeys') or {})
        if job['kind']=='security' and not config.get('command'):
            report=self.co.security.scan(pid)
            if not report['passed']:raise ToolError('Security findings require triage.')
            return report
        lock=self.co.pipeline._locks.setdefault(pid,threading.Lock())
        if not lock.acquire(blocking=False):raise ValueError('Project is busy; scheduled check will retry.')
        try:
            ws=self.co.pipeline.workspace(project)
            output=ws.run_command(config['command'])
        finally:lock.release()
        passed=output.splitlines()[0]=='exit code 0'
        if not passed:
            self.co.observability.ingest(pid,'worker:'+job['name'],job['name']+' detected a failure',output,
                                        severity='critical' if job['kind']=='security' else 'error',event_key=job['name'])
            raise ToolError(output)
        return {'passed':True,'output':output}

    def start(self):
        if self._thread and self._thread.is_alive():return
        poll=max(1,min(60,float((self.co.s.raw.get('workers') or {}).get('poll_seconds',5))))
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                try:self.tick()
                except Exception as exc:self.db.log('worker_error',f'Scheduler error: {type(exc).__name__}')
                self._stop.wait(poll)
        self._thread=threading.Thread(target=loop,name='orgforge-workers',daemon=True);self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=2)

    def service(self,enabled=None):
        if enabled is not None:
            self.db.run('INSERT INTO worker_service VALUES(1,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled',int(enabled))
            if enabled:self.start()
            else:self.stop()
        saved=self.db.one('SELECT enabled FROM worker_service WHERE id=1')
        active=bool(saved['enabled']) if saved else (self.co.s.raw.get('workers') or {}).get('enabled') is True
        return {'enabled':active,'running':bool(self._thread and self._thread.is_alive())}
