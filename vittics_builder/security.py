"""Local exposure checks and optional authoritative OSV queries for exact dependency pins."""
import json
import re
import urllib.request

from .db import now
from .tools import SKIP_DIRS


class Security:
    def __init__(self,company):self.co=company

    def scan(self,project_id):
        ws=self.co.pipeline.workspace(self.co.pipeline.project(project_id));findings=[];scanned=0
        for path in sorted(ws.root.rglob('*')):
            relative=path.relative_to(ws.root)
            if any(p in SKIP_DIRS or p=='.vittics' for p in relative.parts) or path.name.startswith('.env'):
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size>500000:continue
            if scanned>=1000:raise ValueError('Security scan exceeds the 1000-file bound; configure a narrower project.')
            scanned+=1
            try:text=path.read_text(encoding="utf-8")
            except UnicodeError:continue
            for index,line in enumerate(text.splitlines(),1):
                if re.search(r'\b(?:sk-ant-|sk-proj-)[A-Za-z0-9_-]{20,}',line):
                    findings.append({'path':relative.as_posix(),'line':index,'kind':'embedded API credential'})
        report={'checked_at':now(),'files_scanned':scanned,'findings':findings,'passed':not findings}
        if findings:
            self.co.observability.ingest(project_id,'security-scan','Possible embedded credentials found',json.dumps(findings),severity='critical')
        return report

    def dependencies(self,project_id):
        if (self.co.s.raw.get('security') or {}).get('osv_enabled') is not True:
            raise ValueError('OSV scanning is opt-in; enable security.osv_enabled and network access to api.osv.dev.')
        ws=self.co.pipeline.workspace(self.co.pipeline.project(project_id))
        if not ws.resolve('requirements.txt').is_file():raise ValueError('OSV adapter requires an exact-pinned requirements.txt.')
        packages=[]
        for line in ws.read_file('requirements.txt').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):continue
            match=re.fullmatch(r'\s*([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s*(?:#.*)?',line)
            if not match:raise ValueError('Dependency scan requires exact package pins; resolve dependencies first.')
            packages.append({'package':{'name':match[1],'ecosystem':'PyPI'},'version':match[2]})
        if not packages or len(packages)>200:raise ValueError('Scan needs 1–200 pinned packages.')
        request=urllib.request.Request('https://api.osv.dev/v1/querybatch',data=json.dumps({'queries':packages}).encode(),
                                       headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=30) as response:
            data=json.loads(response.read(2_000_000))
        if len(data.get('results',[]))!=len(packages):raise ValueError('OSV response did not cover every package.')
        findings=[{'package':p['package']['name'],'version':p['version'],'vulnerabilities':r.get('vulns',[])}
                  for p,r in zip(packages,data['results']) if r.get('vulns')]
        if findings:self.co.observability.ingest(project_id,'osv','Dependency vulnerabilities reported by OSV',json.dumps(findings),severity='critical')
        return {'packages_checked':len(packages),'findings':findings,'passed':not findings}
