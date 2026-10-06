"""Production: deploy a signed-off release, check it is healthy, and roll back when it is not.

Deployment is configured by people in org.yaml, never by agents: the commands run with production
credentials, so a product's own files cannot change what runs. A project with no `production:` entry
stops at "Ready for deployment" exactly as before.

    production:
      projects:
        Greeter:                              # project name or id
          env: [FLY_API_TOKEN]                # secrets these commands may use (from your environment)
          environments:
            - name: staging
              deploy: ./scripts/deploy.sh staging {version}
              health: curl -fsS https://staging.example.com/healthz
              rollback: ./scripts/deploy.sh staging {previous}
            - name: production
              approval: ceo                   # wait for a human go-ahead (ceo or cto)
              deploy: ./scripts/deploy.sh production {version}
              health: curl -fsS https://example.com/healthz
              rollback: ./scripts/deploy.sh production {previous}
              monitor_minutes: 5              # keep checking health once live (needs the workers service)

Commands run in the product workspace at the signed-off release, with {version}, {previous}, {commit}
and {environment} filled in.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from .db import now
from .observability import redact
from .tools import SECRET_ENV

ROLES = ("ceo", "cto")


class ProductionError(ValueError):
    pass


class Production:
    def __init__(self, company) -> None:
        self.co, self.db, self.s = company, company.db, company.s

    # ---- configuration ---------------------------------------------------
    def config(self, project: dict) -> dict | None:
        projects = (self.s.raw.get("production") or {}).get("projects") or {}
        for key in (project["name"], str(project["id"]), f"P{project['id']}"):
            for name, conf in projects.items():
                if str(name).lower() == key.lower():
                    return self._checked(conf)
        return None

    def configured(self, project: dict) -> bool:
        """Whether the project has a production entry at all; a broken one is reported when deploying."""
        projects = (self.s.raw.get("production") or {}).get("projects") or {}
        keys = {project["name"].lower(), str(project["id"]), f"p{project['id']}"}
        return any(str(name).lower() in keys for name in projects)

    @staticmethod
    def _checked(conf) -> dict:
        envs = conf.get("environments") if isinstance(conf, dict) else None
        if not isinstance(envs, list) or not envs:
            raise ProductionError("production: each project needs a list of environments.")
        names = set()
        for e in envs:
            if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip():
                raise ProductionError("production: every environment needs a name.")
            if e["name"] in names:
                raise ProductionError(f"production: environment '{e['name']}' is listed twice.")
            names.add(e["name"])
            for key in ("deploy", "health"):
                if not isinstance(e.get(key), str) or not e[key].strip():
                    raise ProductionError(f"production: environment '{e['name']}' needs a {key} command.")
            if e.get("approval") not in (None, *ROLES):
                raise ProductionError("production: approval must be ceo or cto.")
        if not isinstance(conf.get("env", []), list):
            raise ProductionError("production: env must be a list of variable names.")
        return conf

    # ---- the deploying stage ---------------------------------------------
    def run(self, project: dict) -> None:
        """Deploy the signed-off version to each environment in order. Always leaves the deploying stage."""
        pid, version = project["id"], project["version"]
        pipeline = self.co.pipeline
        try:
            conf = self.config(project)
        except ProductionError as exc:
            return self._failed(project, None, str(exc), rollback_note="")
        if not conf:
            pipeline._stage(pid, "done")
            return
        ws = pipeline.workspace(project)
        commit = ws.git("rev-parse", "HEAD")
        if ws.changed_files() != "(no uncommitted changes)" or commit != ws.git("rev-parse", f"v{version}"):
            return self._failed(project, None, f"The workspace no longer matches signed-off version {version}. "
                                "Nothing was deployed.", rollback_note="")
        for env in conf["environments"]:
            name = env["name"]
            fill = {"{version}": str(version), "{previous}": str(self._previous(pid, name, version) or ""),
                    "{commit}": commit, "{environment}": name}
            if self._live(pid, name, version):
                continue                                     # already done in this rollout
            if env.get("approval") and not self._approved(pid, name, version):
                if not self._pending(pid, name, version):
                    pipeline._approval(pid, "deploy", env["approval"], f"Deploy {project['name']} v{version} to {name}",
                                       f"Version {version} ({commit[:10]}) is ready for {name}.\n"
                                       f"Deploy: {self._fill(env['deploy'], fill)}\n"
                                       f"Health check: {self._fill(env['health'], fill)}\n\n"
                                       "Approve to deploy now. Reject to hold it at Ready for deployment.",
                                       {"environment": name, "version": version})
                pipeline._stage(pid, "deploy_approval")
                return
            if not self._deploy(project, conf, env, version, commit):
                return
        self.db.log("deploy", f"Version {version} is live in " + ", ".join(e["name"] for e in conf["environments"]) + ".",
                    pid, actor="Production")
        pipeline._stage(pid, "live")

    def _deploy(self, project: dict, conf: dict, env: dict, version: int, commit: str) -> bool:
        pid, name = project["id"], env["name"]
        previous = self._previous(pid, name, version)
        fill = {"{version}": str(version), "{previous}": str(previous or ""), "{commit}": commit, "{environment}": name}
        did = self.db.run("INSERT INTO deployments(project_id, environment, version, commit_sha, status, started_at) "
                          "VALUES (?,?,?,?,?,?)", pid, name, version, commit, "running", now())
        self.db.log("deploy", f"Deploying v{version} to {name}.", pid, actor="Production")
        log: list[str] = []
        ok, out = self._command(project, conf, env["deploy"], fill, env)
        log.append(f"$ deploy\n{out}")
        if ok:
            ok, out = self._healthy(project, conf, env, fill)
            log.append(f"$ health\n{out}")
        if ok:
            self.db.run("UPDATE deployments SET status='superseded' WHERE project_id=? AND environment=? AND status='live'",
                        pid, name)
            self._finish(did, "live", log)
            self._monitor(project, env)
            return True
        rollback_note = "No rollback command is set for this environment."
        status = "failed"
        if env.get("rollback") and previous:
            back = {**fill, "{version}": str(previous)}
            rok, rout = self._command(project, conf, env["rollback"], back, env)
            log.append(f"$ rollback to v{previous}\n{rout}")
            if rok:
                rok, rout = self._healthy(project, conf, env, back)
                log.append(f"$ health after rollback\n{rout}")
            status = "rolled_back" if rok else "rollback_failed"
            rollback_note = (f"Rolled back to v{previous}, which is healthy." if rok else
                             f"Rollback to v{previous} did NOT come back healthy: {name} may be down.")
        elif env.get("rollback"):
            rollback_note = "This was the first deployment to this environment, so there was nothing to roll back to."
        self._finish(did, status, log)
        self._failed(project, name, "\n\n".join(log), rollback_note)
        return False

    def _failed(self, project: dict, env: str | None, detail: str, rollback_note: str) -> None:
        pid, version = project["id"], project["version"]
        where = f" to {env}" if env else ""
        body = redact((rollback_note + "\n\n" if rollback_note else "") + detail)[-6000:]
        if env:
            self.co.observability.ingest(pid, f"deploy:{env}", f"Deployment of v{version}{where} failed", body,
                                         severity="critical", event_key=f"deploy-{env}-v{version}")
        self.co.pipeline._approval(pid, "deploy_failed", "cto", f"Deployment of {project['name']} v{version}{where} failed",
                                   body + "\n\nApprove to try the deployment again. Reject with guidance to send it "
                                   "back to the team.", {"environment": env, "version": version})
        self.co.pipeline._stage(pid, "deploy_failed")

    # ---- commands --------------------------------------------------------
    @staticmethod
    def _fill(template: str, fill: dict) -> str:
        for key, value in fill.items():
            template = template.replace(key, value)
        return template

    def _command(self, project: dict, conf: dict, template: str, fill: dict, env: dict) -> tuple[bool, str]:
        command = self._fill(template, fill)
        allowed = {str(v) for v in conf.get("env") or []}
        environ = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k) or k in allowed}
        timeout = int(env.get("timeout_seconds") or conf.get("timeout_seconds") or 900)
        try:
            from .platforms import shell_command
            args, shell = shell_command(command)
            proc = subprocess.run(args, shell=shell, cwd=project["workspace"], env=environ, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return proc.returncode == 0, redact(f"exit code {proc.returncode}\n{output}")[-4000:]

    def _healthy(self, project: dict, conf: dict, env: dict, fill: dict) -> tuple[bool, str]:
        tries = max(1, min(60, int(env.get("health_retries", 5))))
        wait = max(0.0, float(env.get("health_interval_seconds", 10)))
        out = ""
        for attempt in range(1, tries + 1):
            ok, out = self._command(project, conf, env["health"], fill, env)
            if ok:
                return True, f"healthy on try {attempt} of {tries}\n{out}"
            if attempt < tries:
                time.sleep(wait)
        return False, f"unhealthy after {tries} tries\n{out}"

    def check(self, pid: int, environment: str) -> dict:
        """One health check of what is live (used by the monitoring worker)."""
        project = self.co.pipeline.project(pid)
        conf = self.config(project)
        env = next((e for e in (conf or {}).get("environments", []) if e["name"] == environment), None)
        if not env:
            raise ProductionError(f"No production environment '{environment}' for {project['name']}.")
        live = self.db.one("SELECT * FROM deployments WHERE project_id=? AND environment=? AND status='live' "
                           "ORDER BY id DESC", pid, environment)
        fill = {"{version}": str(live["version"] if live else ""), "{previous}": "",
                "{commit}": live["commit_sha"] if live else "", "{environment}": environment}
        ok, out = self._command(project, conf, env["health"], fill, env)
        if not ok:
            self.co.observability.ingest(pid, f"health:{environment}", f"{project['name']} is unhealthy in {environment}",
                                         out, severity="critical", event_key=f"health-{environment}")
        return {"healthy": ok, "output": out}

    def _monitor(self, project: dict, env: dict) -> None:
        minutes = env.get("monitor_minutes")
        if not minutes:
            return
        name = f"health:{project['id']}:{env['name']}"
        seconds = max(60, int(float(minutes) * 60))
        if self.db.one("SELECT id FROM worker_jobs WHERE name=?", name):
            self.db.run("UPDATE worker_jobs SET interval_seconds=?, enabled=1 WHERE name=?", seconds, name)
        else:
            self.co.workers.add(name, project["id"], "health", seconds, {"environment": env["name"]})

    # ---- records ---------------------------------------------------------
    def _finish(self, did: int, status: str, log: list[str]) -> None:
        self.db.run("UPDATE deployments SET status=?, log=?, finished_at=? WHERE id=?",
                    status, "\n\n".join(log)[-8000:], now(), did)

    def _live(self, pid: int, env: str, version: int) -> bool:
        return bool(self.db.one("SELECT 1 FROM deployments WHERE project_id=? AND environment=? AND version=? "
                                "AND status='live'", pid, env, version))

    def _previous(self, pid: int, env: str, version: int) -> int | None:
        row = self.db.one("SELECT version FROM deployments WHERE project_id=? AND environment=? AND version<? "
                          "AND status IN ('live','superseded') ORDER BY id DESC", pid, env, version)
        return row["version"] if row else None

    def _approved(self, pid: int, env: str, version: int) -> bool:
        return any(self._matches(a, env, version) for a in self.db.all(
            "SELECT * FROM approvals WHERE project_id=? AND kind='deploy' AND status='approved'", pid))

    def _pending(self, pid: int, env: str, version: int) -> bool:
        return any(self._matches(a, env, version) for a in self.db.all(
            "SELECT * FROM approvals WHERE project_id=? AND kind='deploy' AND status='pending'", pid))

    @staticmethod
    def _matches(approval: dict, env: str, version: int) -> bool:
        payload = json.loads(approval["payload"])
        return payload.get("environment") == env and payload.get("version") == version

    def list(self, pid: int | None = None, limit: int = 50) -> list[dict]:
        if pid is not None:
            return self.db.all("SELECT * FROM deployments WHERE project_id=? ORDER BY id DESC LIMIT ?", pid, limit)
        return self.db.all("SELECT * FROM deployments ORDER BY id DESC LIMIT ?", limit)
