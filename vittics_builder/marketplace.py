"""Checksum-pinned agent/skill packages; no install hooks or executable plugin payloads."""
import hashlib
import json
import re

from .db import now
from .operations_store import atomic
from .org import KINDS
from .tools import TOOL_SPECS

NAME=re.compile(r'^[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)?$')


def digest(manifest,assets):
    return hashlib.sha256(json.dumps({'manifest':manifest,'assets':assets},sort_keys=True,separators=(',',':')).encode()).hexdigest()


class Marketplace:
    def __init__(self,company):self.co,self.db=company,company.db

    def register(self,manifest,assets):
        if not isinstance(manifest,dict) or not NAME.fullmatch(str(manifest.get('name',''))) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',str(manifest.get('version',''))):
            raise ValueError('Package needs a namespaced name and semantic version.')
        if set(manifest)-{'name','version','description','roles'}:raise ValueError('Unsupported manifest fields; executable hooks are not permitted.')
        if not isinstance(assets,dict) or not 1<=len(assets)<=20:raise ValueError('Package requires 1–20 Markdown skills.')
        for name,body in assets.items():
            if not re.fullmatch(r'[a-z0-9_-]+\.md',name) or not isinstance(body,str) or not 20<=len(body)<=50000:
                raise ValueError('Skills must be bounded Markdown files with safe names.')
        if len(json.dumps({'manifest':manifest,'assets':assets}))>500000:raise ValueError('Package exceeds 500KB.')
        for role in manifest.get('roles') or []:
            if not isinstance(role,dict) or set(role)-{'id','department','kind','tools','prompt','title'}:
                raise ValueError('Invalid agent role specification.')
            if not isinstance(role.get('department'),str) or not role['department'].strip():
                raise ValueError('Role requires a department identifier.')
            if not NAME.fullmatch(str(role.get('id',''))) or '/' in role['id'] or role.get('kind') not in KINDS:
                raise ValueError('Role needs a safe identifier and supported kind.')
            if not isinstance(role.get('tools'),list) or any(t not in TOOL_SPECS for t in role['tools']) or not str(role.get('prompt','')).strip():
                raise ValueError('Role needs approved tools and a prompt.')
        identity=manifest['name']+'@'+manifest['version'];pin=digest(manifest,assets)
        prior=self.db.one('SELECT * FROM marketplace WHERE name=?',identity)
        if prior and prior['digest']!=pin:raise ValueError('A published package version cannot change content.')
        if not prior:self.db.run('INSERT INTO marketplace(name,version,digest,manifest,assets,created_at) VALUES(?,?,?,?,?,?)',
                                 identity,manifest['version'],pin,json.dumps(manifest),json.dumps(assets),now())
        return {'name':identity,'sha256':pin,'manifest':manifest}

    def list(self):
        return self.db.all('SELECT name,version,digest,installed,created_at FROM marketplace ORDER BY name')

    def install(self,name,expected_sha256):
        row=self.db.one('SELECT * FROM marketplace WHERE name=?',name)
        if not row or row['digest']!=expected_sha256:raise ValueError('Package does not match the reviewed SHA256 pin.')
        if row['installed']:return {'name':name,'installed':True,'already_installed':True}
        manifest,assets=json.loads(row['manifest']),json.loads(row['assets'])
        if digest(manifest,assets)!=expected_sha256:raise ValueError('Stored package integrity check failed.')
        folder=self.co.s.root/'skills'
        if self.co.s.root not in folder.resolve().parents:raise ValueError('Skill directory escapes the company.')
        folder.mkdir(exist_ok=True)
        prefix=re.sub(r'[^a-z0-9_-]','-',name)
        targets={folder/(prefix+'-'+n):v for n,v in assets.items()}
        if any(p.exists() for p in targets):raise ValueError('Installation would overwrite a company skill.')
        roles=manifest.get('roles') or []
        for role in roles:
            if self.db.one('SELECT id FROM roles WHERE id=?',role['id']):raise ValueError('Installation would overwrite an existing role.')
            if not self.db.one('SELECT id FROM departments WHERE id=?',role['department']):raise ValueError('Package targets an unknown department.')
        created=[]
        try:
            with atomic(self.db):
                for path,body in targets.items():
                    with path.open('x') as target:target.write(body)
                    created.append(path)
                for r in roles:self.co.org.add_role(r['id'],r['department'],r['kind'],r['tools'],r['prompt'],r.get('title'))
                self.db.run('UPDATE marketplace SET installed=1 WHERE name=?',name)
                self.db.log('marketplace',f'Installed pinned package {name}.',actor='CEO')
        except BaseException:
            for path in created:path.unlink(missing_ok=True)
            raise
        return {'name':name,'installed':True,'skills':[p.name for p in targets],'roles':[r['id'] for r in roles]}
