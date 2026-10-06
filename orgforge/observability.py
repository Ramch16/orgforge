"""Deduplicated observations become triageable tickets, never automatic deployments."""
import hashlib
import re

from .db import now
from .operations_store import atomic


def redact(text):
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b','[email]',str(text))
    return re.sub(r'(?i)(authorization\s*[:=]\s*bearer\s+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*)[^\s,;]+',r'\1[redacted]',text)


def sanitize(value):
    if isinstance(value,dict):
        return {str(k): '[redacted]' if re.fullmatch(r'(?i)password|api[_-]?key|authorization|access[_-]?token|secret',str(k))
                else sanitize(v) for k,v in value.items()}
    if isinstance(value,list):return [sanitize(v) for v in value]
    if isinstance(value,str):return redact(value)
    return value


class Observability:
    def __init__(self, company):
        self.co,self.db=company,company.db

    def ingest(self, project_id, source, title, body, severity='error', event_key=None):
        self.co.pipeline.project(project_id)
        if severity not in ('info','warning','error','critical'):
            raise ValueError('Unsupported observation severity.')
        source,title,body=redact(source)[:200],redact(title).strip()[:200],redact(body).strip()[:8000]
        if not source or not title or not body:
            raise ValueError('Observation needs a source, title, and body.')
        key=str(event_key or hashlib.sha256((title+'\n'+body).encode()).hexdigest())[:200]
        with atomic(self.db):
            existing=self.db.one('SELECT * FROM observations WHERE project_id=? AND source=? AND event_key=?',project_id,source,key)
            if existing:
                severity=max((severity,existing['severity']),key=('info','warning','error','critical').index)
                self.db.run('UPDATE observations SET occurrences=occurrences+1,last_seen=?,severity=?,body=? WHERE id=?',now(),severity,body,existing['id'])
                oid=existing['id']
            else:
                oid=self.db.run('INSERT INTO observations(project_id,source,event_key,severity,title,body,created_at,last_seen) '
                                'VALUES(?,?,?,?,?,?,?,?)',project_id,source,key,severity,title,body,now(),now())
            if severity!='info':
                priority='urgent' if severity=='critical' else 'high' if severity=='error' else 'medium'
                if not existing or not existing['ticket_id']:
                    ticket=self.co.tickets.create(project_id,title,body,'Observability',type='bug',priority=priority,status='backlog',origin='observation')
                    self.db.run('UPDATE observations SET ticket_id=? WHERE id=?',ticket['id'],oid)
                else:
                    self.db.run('UPDATE tasks SET priority=? WHERE id=?',priority,existing['ticket_id'])
        return self.db.one('SELECT * FROM observations WHERE id=?',oid)

    def list(self, project_id=None):
        return self.db.all('SELECT * FROM observations'+(' WHERE project_id=?' if project_id is not None else '')+
                           ' ORDER BY id DESC LIMIT 200',*([project_id] if project_id is not None else []))
