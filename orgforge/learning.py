"""Review-derived trajectories and bounded exploration of company-approved strategies."""
import re

from .db import now

LABELS = {'python':r'python|pytest|django|fastapi', 'frontend':r'react|frontend|css|browser|\bui\b',
          'security':r'auth|security|permission|token', 'testing':r'test|coverage|regression',
          'refactor':r'refactor|migration|cleanup'}


class Learning:
    def __init__(self, company):
        self.co, self.db = company, company.db

    def strategies(self, kind):
        strategies = {'baseline': {'prompt':'', 'kinds':[]}}
        strategies.update((self.co.s.raw.get('learning') or {}).get('strategies') or {})
        return {n: c for n,c in strategies.items() if not c.get('kinds') or kind in c['kinds']}

    def prepare(self, kind, instructions, requested=None):
        signature = ','.join(k for k,v in LABELS.items() if re.search(v,instructions,re.I)) or 'general'
        candidates = self.strategies(kind)
        if requested is not None and requested not in candidates:
            raise ValueError('Strategy is not approved for this role kind.')
        config = self.co.s.raw.get('learning') or {}
        choice = requested or 'baseline'
        if requested is None and config.get('enabled') is True:
            evidence = {r['strategy']:r for r in self.summary(kind, signature)}
            trials = max(1, min(20, int(config.get('min_trials',2))))
            missing = [n for n in candidates if evidence.get(n,{}).get('attempts',0)<trials]
            if missing:
                choice = min(missing,key=lambda n:evidence.get(n,{}).get('attempts',0))
            else:
                choice = max(candidates, key=lambda n:(evidence[n]['successes']+1)/(evidence[n]['attempts']+2)
                             + .2*(evidence[n]['quality'] or 0)/100)
        prompt = str(candidates[choice].get('prompt') or '')[:8000]
        return choice, signature, instructions + ('\n\nCompany-approved approach (human instructions take precedence):\n'+prompt if prompt else '')

    def record(self, route, success, quality, lessons):
        r = self.db.one('SELECT * FROM routing_decisions WHERE id=?',route)
        if not r:
            raise ValueError('Unknown routed run.')
        self.db.run('INSERT INTO trajectories VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(route_id) DO UPDATE '
                    'SET success=excluded.success,quality=excluded.quality,lessons=excluded.lessons',
                    route,r['strategy'],r['task_kind'],r['signature'],int(success),quality,str(lessons)[:4000],now())

    def summary(self, kind=None, signature=None):
        filters, args = [], []
        for name,value in [('task_kind',kind),('signature',signature)]:
            if value is not None:
                filters.append(name+'=?');args.append(value)
        where = ' WHERE '+' AND '.join(filters) if filters else ''
        return self.db.all('SELECT strategy,task_kind,signature,COUNT(*) AS attempts,SUM(success) AS successes,'
                           'AVG(quality) AS quality FROM trajectories'+where+' GROUP BY strategy,task_kind,signature',*args)
