"""Authenticated, scoped federation of observations and task proposals, with replay auditing."""
import hashlib
import hmac
import json
import os
import time
import urllib.request
import uuid
from urllib.parse import urlsplit

from .db import now
from .observability import sanitize
from .operations_store import atomic


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


class Federation:
    def __init__(self,company):self.co,self.db=company,company.db

    def config(self):return self.co.s.raw.get('federation') or {}

    def _peer(self,name):
        config=self.config()
        if config.get('enabled') is not True:raise ValueError('Federation is disabled.')
        peer=(config.get('peers') or {}).get(name)
        if not peer:raise PermissionError('Federation peer is not trusted.')
        key=os.environ.get(peer.get('key_env',''))
        if not key or len(key)<32:raise ValueError('Peer signing key requires a secure binding of at least 32 characters.')
        return peer,key.encode()

    def envelope(self,peer_name,kind,payload):
        peer,key=self._peer(peer_name)
        if kind not in ('observation','task_proposal','task_result'):raise ValueError('Unsupported federation message type.')
        message={'version':1,'sender':self.config().get('identity'),'recipient':peer_name,'id':uuid.uuid4().hex,
                 'issued_at':int(time.time()),'expires_at':int(time.time())+300,'kind':kind,'payload':sanitize(payload)}
        if not message['sender']:raise ValueError('Federation requires a company identity.')
        return {**message,'signature':hmac.new(key,canonical(message),hashlib.sha256).hexdigest()}

    def receive(self,envelope):
        if not isinstance(envelope,dict) or set(envelope)!={'version','sender','recipient','id','issued_at','expires_at','kind','payload','signature'}:
            raise ValueError('Invalid federation envelope.')
        if len(canonical(envelope))>64000:raise ValueError('Federation message exceeds 64KB.')
        if not isinstance(envelope['sender'],str):raise ValueError('Invalid sender identity.')
        peer,key=self._peer(envelope['sender'])
        signed={k:v for k,v in envelope.items() if k!='signature'}
        expected=hmac.new(key,canonical(signed),hashlib.sha256).hexdigest()
        if not isinstance(envelope['signature'],str) or not hmac.compare_digest(expected,envelope['signature']):
            raise PermissionError('Federation signature is invalid.')
        issued,expiry=envelope['issued_at'],envelope['expires_at']
        if type(issued) is not int or type(expiry) is not int or not 0<expiry-issued<=300 or issued>time.time()+60 or expiry<time.time():
            raise PermissionError('Federation envelope is expired or has invalid timestamps.')
        if envelope['version']!=1 or envelope['recipient']!=self.config().get('identity'):
            raise PermissionError('Federation recipient or protocol is invalid.')
        if not isinstance(envelope['id'],str) or not 1<=len(envelope['id'])<=100:raise ValueError('Invalid message ID.')
        payload=sanitize(envelope['payload'])
        if not isinstance(payload,dict) or type(payload.get('project_id')) is not int:raise ValueError('Payload requires a project ID.')
        pid=payload['project_id'];kind=envelope['kind']
        if pid not in (peer.get('projects') or []) or kind not in (peer.get('capabilities') or []):
            raise PermissionError('Peer capability or project scope is not authorized.')
        self.co.pipeline.project(pid)
        with atomic(self.db):
            existing=self.db.one('SELECT * FROM federation_inbox WHERE peer=? AND message_id=?',envelope['sender'],envelope['id'])
            if existing:return {**json.loads(existing['receipt']),'duplicate':True}
            if kind=='observation':
                if set(payload)-{'project_id','title','body','severity','event_key'}:raise ValueError('Unsupported observation fields.')
                result=self.co.observability.ingest(pid,'peer:'+envelope['sender'],payload['title'],payload['body'],payload.get('severity','error'),payload.get('event_key'))
                receipt={'observation_id':result['id']}
            elif kind=='task_proposal':
                if set(payload)-{'project_id','title','body','role'}:raise ValueError('Unsupported proposal fields.')
                if payload.get('role') not in (peer.get('roles') or []):raise PermissionError('Peer cannot propose this role.')
                task=self.co.tickets.create(pid,str(payload['title'])[:200],str(payload['body'])[:8000],'Federation',
                                            status='backlog',role=payload['role'],origin='federation')
                receipt={'ticket_id':task['id']}
            elif kind=='task_result':
                if set(payload)-{'project_id','ticket_id','body'}:raise ValueError('Unsupported task result fields.')
                ticket=self.co.tickets.get(payload['ticket_id'])
                if ticket['project_id']!=pid:raise PermissionError('Task result is outside the authorized project.')
                self.co.tickets.comment(ticket['id'],'Peer '+envelope['sender'],str(payload['body'])[:8000])
                receipt={'ticket_id':ticket['id'],'review_required':True}
            else:raise ValueError('Unsupported federation message type.')
            self.db.run('INSERT INTO federation_inbox VALUES(?,?,?,?)',envelope['sender'],envelope['id'],json.dumps(receipt),now())
            self.db.log('federation',f"Accepted {kind} from peer {envelope['sender']} (message {envelope['id']}).",pid)
        return receipt

    def send(self,peer_name,kind,payload):
        peer,_=self._peer(peer_name);url=peer.get('url','');parsed=urlsplit(url)
        if parsed.username or parsed.password or parsed.scheme not in ('http','https'):
            raise ValueError('Peer URL requires authenticated HTTPS without URL credentials.')
        if parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
            raise ValueError('HTTP federation is permitted only for loopback testing.')
        request=urllib.request.Request(url.rstrip('/')+'/api/federation/inbox',data=canonical(self.envelope(peer_name,kind,payload)),
                                       headers={'Content-Type':'application/json'})
        # Do not follow redirects: credentials and signed payloads must stay on the configured destination.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=30) as response:
            return json.loads(response.read(64000))
