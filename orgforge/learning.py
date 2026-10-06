"""The learning loop: what worked, what failed, and which reviewers can be trusted.

- Strategies are approaches added to an assignment ("write the failing test first"). The baseline and
  any written in org.yaml are always candidates; so are strategies the team proposed from its own
  failed reviews, once the CTO approved them. Each candidate is tried `min_trials` times for a kind
  of work, then the one with the best reviewed results is used.
- After `propose_after` failed reviews of the same kind of work, an architect reads the review
  findings and proposes a strategy. Nothing is used until the CTO approves it.
- Every review verdict is settled later by what actually happened: the CTO's call on an escalated
  ticket, release checks failing, or the release being accepted. A reviewer's accuracy feeds their
  performance score and the routing evidence for their model.

On by default; `learning.enabled: false` turns all of it off.
"""
import re

from .db import now

LABELS = {'python': r'python|pytest|django|fastapi', 'frontend': r'react|frontend|css|browser|\bui\b',
          'security': r'auth|security|permission|token', 'testing': r'test|coverage|regression',
          'refactor': r'refactor|migration|cleanup'}
NAME = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
RIGHT, WRONG = 92, 35          # the same weights as a human approving or rejecting work
SHARED_MISS = 60               # a release-level failure is shared and vague: one mild mark per reviewer


class Learning:
    def __init__(self, company):
        self.co, self.db = company, company.db

    @property
    def config(self) -> dict:
        return self.co.s.raw.get('learning') or {}

    @property
    def enabled(self) -> bool:
        return self.config.get('enabled', True) is not False

    # ---- strategies ------------------------------------------------------
    def strategies(self, kind, signature=None):
        strategies = {'baseline': {'prompt': '', 'kinds': []}}
        strategies.update(self.config.get('strategies') or {})
        for s in self.db.all("SELECT * FROM learned_strategies WHERE status='approved' AND task_kind=?", kind):
            if signature is None or s['signature'] == 'general' or set(s['signature'].split(',')) <= set(signature.split(',')):
                strategies.setdefault(s['name'], {'prompt': s['prompt'], 'kinds': [kind]})
        return {n: c for n, c in strategies.items() if not c.get('kinds') or kind in c['kinds']}

    def prepare(self, kind, instructions, requested=None):
        signature = ','.join(k for k, v in LABELS.items() if re.search(v, instructions, re.I)) or 'general'
        candidates = self.strategies(kind, signature)
        if requested is not None and requested not in candidates:
            raise ValueError('Strategy is not approved for this role kind.')
        choice = requested or 'baseline'
        if requested is None and self.enabled:
            evidence = {r['strategy']: r for r in self.summary(kind, signature)}
            trials = max(1, min(20, int(self.config.get('min_trials', 2))))
            missing = [n for n in candidates if evidence.get(n, {}).get('attempts', 0) < trials]
            if missing:
                choice = min(missing, key=lambda n: evidence.get(n, {}).get('attempts', 0))
            else:
                choice = max(candidates, key=lambda n: (evidence[n]['successes'] + 1) / (evidence[n]['attempts'] + 2)
                             + .2 * (evidence[n]['quality'] or 0) / 100)
        prompt = str(candidates[choice].get('prompt') or '')[:8000]
        return choice, signature, instructions + ('\n\nCompany-approved approach (human instructions take precedence):\n'
                                                  + prompt if prompt else '')

    def record(self, route, success, quality, lessons):
        r = self.db.one('SELECT * FROM routing_decisions WHERE id=?', route)
        if not r:
            raise ValueError('Unknown routed run.')
        self.db.run('INSERT INTO trajectories VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(route_id) DO UPDATE '
                    'SET success=excluded.success,quality=excluded.quality,lessons=excluded.lessons',
                    route, r['strategy'], r['task_kind'], r['signature'], int(success), quality, str(lessons)[:4000], now())

    def summary(self, kind=None, signature=None):
        filters, args = [], []
        for name, value in [('task_kind', kind), ('signature', signature)]:
            if value is not None:
                filters.append(name + '=?')
                args.append(value)
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        return self.db.all('SELECT strategy,task_kind,signature,COUNT(*) AS attempts,SUM(success) AS successes,'
                           'AVG(quality) AS quality FROM trajectories' + where + ' GROUP BY strategy,task_kind,signature', *args)

    # ---- proposing strategies from failed reviews ------------------------
    def after_failure(self, route_id, pid):
        """Called after a failed review. Proposes a strategy once the same kind of work keeps failing."""
        threshold = int(self.config.get('propose_after', 3))
        route = self.db.one('SELECT task_kind, signature FROM routing_decisions WHERE id=?', route_id) if route_id else None
        if not self.enabled or threshold <= 0 or not route:
            return None
        kind, signature = route['task_kind'], route['signature']
        last = self.db.one('SELECT status, created_at FROM learned_strategies WHERE task_kind=? AND signature=? '
                           'ORDER BY id DESC', kind, signature)
        if last and last['status'] == 'proposed':
            return None                                  # one open proposal at a time for this kind of work
        failures = self.db.all('SELECT t.lessons, r.task_id FROM trajectories t JOIN routing_decisions r ON r.id=t.route_id '
                               "WHERE t.task_kind=? AND t.signature=? AND t.success=0 AND t.lessons!='' AND t.created_at>? "
                               'ORDER BY t.created_at DESC LIMIT 10', kind, signature, last['created_at'] if last else '')
        if len(failures) < threshold or len({f['task_id'] for f in failures}) < 2:
            return None                                  # one ticket failing over and over is not a pattern
        return self.propose(kind, signature, [f['lessons'] for f in failures], pid)

    def propose(self, kind, signature, lessons, pid):
        author = self.co.org.pick(kind='planner') or self.co.org.pick(kind='reviewer')
        if not author:
            return None
        work = 'general work' if signature == 'general' else f'{signature.replace(",", ", ")} work'
        existing = [s['name'] for s in self.db.all('SELECT name FROM learned_strategies')]
        ask = (f"Our {kind}s keep failing review on {work}. These are the review findings from the last "
               f"{len(lessons)} failed attempts:\n\n" + '\n\n---\n\n'.join(str(l)[:1200] for l in lessons) +
               "\n\nPropose one strategy: a short approach (at most 1,500 characters) that would have prevented "
               "these failures, written as instructions to the engineer. Be concrete about what to do, not what "
               "went wrong. Give it a short lowercase name with dashes"
               + (f" (not one of: {', '.join(existing)})" if existing else "") +
               ", and say why in a sentence or two. The CTO decides whether the team uses it. "
               "Finish by calling submit_strategy.")
        res = self.co.runtime.run(author, ask, None, project_id=pid, meta={'purpose': 'strategy'},
                                  only_tools={'submit_strategy'}, extra_tools=['submit_strategy'])
        s = res.strategy
        if not s or self.db.one('SELECT 1 FROM learned_strategies WHERE name=?', s['name']):
            self.db.log('learning', f"{author['name']} could not propose a usable strategy for {work}.", pid)
            return None
        sid = self.db.run('INSERT INTO learned_strategies(name, task_kind, signature, prompt, why, evidence, '
                          'proposed_by, created_at) VALUES (?,?,?,?,?,?,?,?)', s['name'], kind, signature,
                          s['prompt'], s['why'], len(lessons), author['name'], now())
        self.co.pipeline._approval(
            pid, 'strategy', 'cto', f"New approach for {kind}s on {work}: {s['name']}",
            f"{author['name']} proposes this after {len(lessons)} failed reviews of {work}.\n\nWhy: {s['why']}\n\n"
            f"The approach:\n{s['prompt']}\n\nApprove to let the team try it: it is used a few times next to the "
            "current approach, and the one with better reviewed results wins. Reject to drop it.",
            {'strategy_id': sid})
        return sid

    def decide(self, payload, ok, who):
        self.db.run('UPDATE learned_strategies SET status=?, decided_by=?, decided_at=? WHERE id=?',
                    'approved' if ok else 'rejected', who, now(), payload['strategy_id'])

    def proposals(self):
        return self.db.all('SELECT * FROM learned_strategies ORDER BY id DESC LIMIT 50')

    # ---- reviewer calibration --------------------------------------------
    def verdict(self, pid, task_id, reviewer_id, route_id, verdict, score):
        self.db.run("UPDATE review_verdicts SET settled_by='superseded', settled_at=? WHERE task_id=? AND reviewer_id=? "
                    "AND settled_at IS NULL", now(), task_id, reviewer_id)     # only the latest round is judged
        self.db.run('INSERT INTO review_verdicts(project_id, task_id, reviewer_id, route_id, verdict, score, created_at) '
                    'VALUES (?,?,?,?,?,?,?)', pid, task_id, reviewer_id, route_id, verdict, score, now())

    def settle_task(self, task_id, accepted, by):
        """The CTO decided an escalated ticket: blocking reviewers were right if it was sent back."""
        rows = self.db.all('SELECT * FROM review_verdicts WHERE task_id=? AND settled_at IS NULL', task_id)
        self._settle(rows, lambda v: (v['verdict'] == 'approve') == accepted,
                     f"{by} {'accepted' if accepted else 'sent back'} the ticket", by, shared=False)

    def settle_project(self, pid, passed, by, why):
        """Release checks or a human's review of the release show whether approvals were deserved."""
        rows = self.db.all("SELECT * FROM review_verdicts WHERE project_id=? AND settled_at IS NULL AND verdict='approve'", pid)
        self._settle(rows, lambda v: passed, why, by, shared=True)

    def _settle(self, rows, right, why, by, shared):
        if not rows or not self.enabled:
            return
        score = self.config.get('score_reviewers', True) is not False
        router = self.co.router
        marks: dict[int, list[tuple[bool, dict]]] = {}
        for v in rows:
            ok = bool(right(v))
            self.db.run('UPDATE review_verdicts SET correct=?, settled_by=?, settled_at=? WHERE id=?',
                        int(ok), why[:200], now(), v['id'])
            if v['route_id'] and router:
                router.evaluate(v['route_id'], ok, None)
            marks.setdefault(v['reviewer_id'], []).append((ok, v))
        if not score:
            return
        for reviewer, results in marks.items():
            if not self.db.one("SELECT 1 FROM agents WHERE id=? AND status!='fired'", reviewer):
                continue
            if shared:                                   # one mark for the whole release, not one per ticket
                ok = all(r for r, _ in results)
                tickets = ", ".join(f"T-{v['task_id']}" for _, v in results if v['task_id'])
                self.co.perf.record(reviewer, RIGHT if ok else SHARED_MISS, source='calibration', reviewer=by,
                                    notes=f"You approved {tickets}; {why}.", project_id=results[0][1]['project_id'])
            else:
                for ok, v in results:
                    self.co.perf.record(reviewer, RIGHT if ok else WRONG, source='calibration', reviewer=by,
                                        notes=(f"Your {v['verdict'].replace('_', ' ')} on T-{v['task_id']} was "
                                               f"{'borne out' if ok else 'wrong'}: {why}."),
                                        task_id=v['task_id'], project_id=v['project_id'])
            self.co.perf.evaluate(reviewer)

    def reviewers(self):
        """Each reviewer's settled verdicts: how often they were right, missed problems, or blocked good work."""
        return self.db.all(
            "SELECT a.name, a.role, COUNT(*) AS settled, SUM(v.correct) AS right, "
            "SUM(v.correct=0 AND v.verdict='approve') AS missed, SUM(v.correct=0 AND v.verdict!='approve') AS too_strict, "
            "(SELECT COUNT(*) FROM review_verdicts u WHERE u.reviewer_id=a.id AND u.settled_at IS NULL) AS pending "
            "FROM review_verdicts v JOIN agents a ON a.id=v.reviewer_id WHERE v.correct IS NOT NULL "
            "GROUP BY a.id ORDER BY a.name")


def parse_strategy(args) -> dict:
    name = str(args.get('name', '')).strip().lower()
    prompt, why = str(args.get('prompt', '')).strip(), str(args.get('why', '')).strip()
    if not NAME.fullmatch(name):
        raise ValueError('Name the strategy in lowercase letters, digits and dashes (2–41 characters).')
    if not 40 <= len(prompt) <= 1500:
        raise ValueError('The approach must be 40–1,500 characters.')
    return {'name': name, 'prompt': prompt, 'why': why[:600]}

