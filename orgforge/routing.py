"""Opt-in model selection, with review evidence separated from mere run completion."""
from __future__ import annotations

import math
import os
import shutil
import time

from .costs import price
from .db import now


class ModelRouter:
    def __init__(self, db, settings):
        self.db, self.s = db, settings
        self.learning = None

    def select(self, agent, kind, meta):
        config = self.s.raw.get('routing') or {}
        rules = config.get('rules') or {}
        candidates = rules.get(meta.get('purpose')) or rules.get(agent['role']) or rules.get(kind) or []
        if config.get('enabled', True) is False or not candidates:     # on by default; without a rule, nothing changes
            return agent['model'], 'Assigned agent model'
        if not isinstance(candidates, list) or any(not isinstance(m, str) or not m.strip() for m in candidates):
            raise ValueError('routing.rules values must be nonempty model lists.')
        candidates = list(dict.fromkeys(candidates))
        if config.get('independent_reviews') and meta.get('author_model'):
            candidates = [m for m in candidates if m != meta['author_model']]
            if not candidates:
                raise ValueError('Independent review requires a candidate different from the builder model.')
        candidates = [m for m in candidates if self.available(m)]
        if not candidates:
            raise RuntimeError('No configured routing candidate is available. Check engine installations and credential bindings.')
        evidence = {r['model']: r for r in self.summary(kind)}
        if meta.get('signature'):
            evidence.update({r['model']:r for r in self.summary(kind,meta['signature']) if r['evaluated']})
        def rank(model):
            e = evidence.get(model)
            # Beta prior: a new model is neutral, not a perfect performer.
            success = (e['successes'] + 1) / (e['evaluated'] + 2) if e else .5
            quality = e['quality'] / 100 if e and e['quality'] is not None else .5
            latency = e['latency'] if e else 0
            rate = price(self.s, model)
            cost = (sum(rate) / 100 if rate is not None else 1)
            if e and e['priced'] and e['evaluated']:
                cost = e['cost'] / e['evaluated']
            return .6 * success + .3 * quality - .05 * math.log1p(latency) - .05 * math.log1p(cost)
        model = max(candidates, key=rank)   # deterministic configured order breaks ties
        return model, f'Role/kind rule; reviewed quality, success, latency and estimated cost ({kind})'

    def available(self, model):
        """Local readiness only; a present credential does not prove remote API access."""
        if model.startswith('cli:'):
            name = model[4:].split('/')[0]
            engine = self.s.engines.get(name) or {}
            command = engine.get('command') or []
            return bool(command and shutil.which(command[0]))
        prefix, sep, _ = model.partition(':')
        if sep:
            endpoint = self.s.endpoints.get(prefix)
            return bool(endpoint and (not endpoint.get('key_env') or os.environ.get(endpoint['key_env'])))
        return self.s.provider != 'anthropic' or bool(os.environ.get('ANTHROPIC_API_KEY'))

    def choose(self, agent, meta, kind):
        return (agent['model'], 'Explicit arena candidate') if meta.get('model_override') else self.select(agent, kind, meta)

    def start(self, agent, project_id, meta, kind, model=None, reason=None):
        if model is None:
            model, reason = self.choose(agent, meta, kind)
        route = self.db.run('INSERT INTO routing_decisions(agent_id,project_id,task_id,task_kind,model,reason,created_at) '
                            'VALUES (?,?,?,?,?,?,?)', agent['id'], project_id, meta.get('ticket_id'),
                            kind, model, reason, now())
        self.db.run('UPDATE routing_decisions SET strategy=?,signature=? WHERE id=?',meta.get('strategy','baseline'),meta.get('signature','general'),route)
        return {**agent, 'model': model}, route, time.monotonic()

    def finish(self, route, start, status, run_id):
        # Per-run usage is attributed explicitly; concurrent calls cannot contaminate it.
        usage = self.db.one('SELECT COALESCE(SUM(cost),0) AS cost, COALESCE(MIN(priced),1) AS priced '
                            'FROM usage WHERE run_id=?', run_id) if run_id else {'cost': 0, 'priced': 0}
        self.db.run('UPDATE routing_decisions SET status=?,latency=?,cost=?,priced=? WHERE id=?',
                    status, time.monotonic() - start, usage['cost'], usage['priced'], route)

    def evaluate(self, route, success, quality=None, lessons=""):
        if quality is not None and (not math.isfinite(quality) or not 0 <= quality <= 100):
            raise ValueError('Routing quality must be between 0 and 100.')
        self.db.run('UPDATE routing_decisions SET success=?,quality=? WHERE id=?', bool(success), quality, route)
        if self.learning:self.learning.record(route,success,quality,lessons)

    def summary(self, kind=None, signature=None):
        filters,args=[],[]
        for name,value in [('task_kind',kind),('signature',signature)]:
            if value is not None:filters.append(name+'=?');args.append(value)
        where = ' WHERE '+' AND '.join(filters) if filters else ''
        return self.db.all('SELECT model, task_kind, COUNT(*) AS runs, COUNT(success) AS evaluated, '
                           'COALESCE(SUM(success),0) AS successes, AVG(quality) AS quality, AVG(latency) AS latency, '
                           'SUM(cost) AS cost, MIN(priced) AS priced, '
                           'CASE WHEN MIN(priced)=1 AND SUM(success)>0 THEN SUM(cost)/SUM(success) '
                           'ELSE NULL END AS cost_per_success FROM routing_decisions' + where +
                           ' GROUP BY model,task_kind ORDER BY model,task_kind', *args)
