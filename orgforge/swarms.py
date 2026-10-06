"""Project teams adapt to ready work; organizational reporting lines stay intact."""
from __future__ import annotations

import threading

from .db import now


class Swarms:
    def __init__(self, db, settings, org):
        self.db, self.s, self.org = db, settings, org
        self._staffing_lock = threading.Lock()

    @property
    def config(self):
        return self.s.raw.get('swarms') or {}

    def pick(self, task, project_id, busy):
        # Rework remains with the current holder of the original seat.
        roles = [task['role'], *(self.config.get('capabilities', {}).get(task['role']) or [])]
        if task.get('assignee_id'):
            previous = self.db.one('SELECT seat FROM agents WHERE id=?', task['assignee_id'])
            holder = previous and self.db.one("SELECT * FROM agents WHERE seat=? AND status!='fired'", previous['seat'])
            if holder and holder['role'] in roles:
                return None if holder['id'] in busy else holder
        for role in dict.fromkeys(roles):
            candidate = self.org.pick(role=role, project_id=project_id, exclude=busy)
            if candidate:
                return candidate
        # Preserve the legacy fallback only for roles with no staff or configured substitute.
        if not any(self.org.staff(role=r) for r in roles):
            return self.org.pick(kind='builder', project_id=project_id, exclude=busy)
        return None

    def assign(self, project_id, task_id, agent):
        self.db.run('INSERT INTO swarm_members(project_id, agent_id, task_id, assigned_at) VALUES (?,?,?,?) '
                    'ON CONFLICT(project_id,task_id) DO UPDATE SET agent_id=excluded.agent_id, '
                    'assigned_at=excluded.assigned_at', project_id, agent['id'], task_id, now())

    def scale(self, project_id):
        """Auto-staff only with explicit company configuration, within existing role caps."""
        if self.config.get('auto_staff') is not True:
            return False
        with self._staffing_lock:
            self._scale(project_id)
        return True

    def _scale(self, project_id):
        rows = self.db.all("SELECT role, COUNT(*) AS n FROM tasks WHERE project_id=? AND status='todo' "
                           "AND origin!='stage' AND type!='question' GROUP BY role", project_id)
        for row in rows:
            staff = self.org.staff(role=row['role'])
            if not staff:
                continue
            target = min(self.s.max_per_role, max(1, (row['n'] + self.s.hire_when_waiting - 1)
                                                  // self.s.hire_when_waiting))
            while len(staff) < target:
                hired = self.org.hire(row['role'], by='adaptive swarm')
                staff.append(hired)
                self.db.log('swarm', f"Added {hired['name']} to address {row['role']} workload.", project_id)

    def snapshot(self, project_id):
        members = self.db.all('SELECT m.*, a.name, a.role, a.status AS agent_status, t.title, '
                              't.status AS task_status FROM swarm_members m JOIN agents a ON a.id=m.agent_id '
                              'JOIN tasks t ON t.id=m.task_id WHERE m.project_id=? ORDER BY t.id', project_id)
        queues = self.db.all("SELECT role, COUNT(*) AS waiting FROM tasks WHERE project_id=? AND status='todo' "
                             "AND origin!='stage' AND type!='question' GROUP BY role", project_id)
        for q in queues:
            q['staff'] = len(self.org.staff(role=q['role']))
            q['bottleneck'] = q['waiting'] >= self.s.hire_when_waiting * max(1, q['staff'])
        return {'project_id': project_id, 'members': members, 'queues': queues,
                'auto_staff': self.config.get('auto_staff') is True}
