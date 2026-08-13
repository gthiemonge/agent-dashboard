#!/usr/bin/env python3
"""Agent Dashboard — lightweight server."""

import http.server
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("agent-dashboard")

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "agents.yaml"
STATIC_DIR = BASE_DIR / "static"

runs = {}
run_counter = 0
lock = threading.Lock()


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_template(template, params):
    def replacer(m):
        key = m.group(1)
        return str(params.get(key, m.group(0)))
    return re.sub(r"\{\{(\w+)\}\}", replacer, template)


def spawn_agent(agent_name, params):
    global run_counter
    config = load_config()
    agent = next((a for a in config.get("agents", []) if a["name"] == agent_name), None)
    if not agent:
        return None

    with lock:
        run_counter += 1
        run_id = run_counter

    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    base_output = Path(config.get("outputDir", "./outputs")).resolve()
    run_output = base_output / agent_name / f"run-{ts}"
    run_output.mkdir(parents=True, exist_ok=True)

    all_params = {**params, "outputDir": str(run_output)}
    command = resolve_template(agent["command"], all_params)

    meta_path = run_output / ".run.json"
    with open(meta_path, "w") as f:
        json.dump({"agent": agent_name, "params": params, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)

    run = {
        "id": run_id,
        "agent": agent_name,
        "params": params,
        "command": command,
        "outputDir": str(run_output),
        "status": "running",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "finishedAt": None,
        "exitCode": None,
        "stdout": "",
        "stderr": "",
    }
    with lock:
        runs[run_id] = run

    timeout_s = agent.get("timeout", 300)

    def execute():
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                cwd=str(BASE_DIR), timeout=timeout_s,
                env={**os.environ, "OUTPUT_DIR": str(run_output)},
            )
            with lock:
                run["stdout"] = result.stdout
                run["stderr"] = result.stderr
                run["exitCode"] = result.returncode
                run["status"] = "completed" if result.returncode == 0 else "failed"
                run["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_run_logs(run_output, result.stdout, result.stderr, run)
            log.info("Run #%d finished: agent=%s status=%s exit_code=%d", run_id, agent_name, run["status"], result.returncode)
            if result.stderr:
                log.debug("Run #%d stderr: %s", run_id, result.stderr.strip())
        except subprocess.TimeoutExpired:
            with lock:
                run["status"] = "failed"
                run["stderr"] = f"Command timed out after {timeout_s}s"
                run["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_run_logs(run_output, "", run["stderr"], run)
            log.warning("Run #%d timed out after %ds: agent=%s", run_id, timeout_s, agent_name)
        except Exception as e:
            with lock:
                run["status"] = "failed"
                run["stderr"] = str(e)
                run["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_run_logs(run_output, "", run["stderr"], run)
            log.error("Run #%d error: agent=%s %s", run_id, agent_name, e)

    log.info("Run #%d started: agent=%s params=%s", run_id, agent_name, params)
    log.debug("Run #%d command: %s", run_id, command)

    threading.Thread(target=execute, daemon=True).start()
    return {"runId": run_id, "outputDir": str(run_output)}


def save_run_logs(run_output, stdout, stderr, run):
    if stdout:
        (run_output / ".stdout.log").write_text(stdout)
    if stderr:
        (run_output / ".stderr.log").write_text(stderr)
    meta_path = run_output / ".run.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta["status"] = run["status"]
    meta["exitCode"] = run.get("exitCode")
    meta["finishedAt"] = run.get("finishedAt")
    meta_path.write_text(json.dumps(meta, default=str))


def cron_matches(expr, now):
    """Check if a cron expression matches the given datetime. Supports standard 5-field cron."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    checks = [
        (minute, now.tm_min, 0, 59),
        (hour, now.tm_hour, 0, 23),
        (dom, now.tm_mday, 1, 31),
        (month, now.tm_mon, 1, 12),
        (dow, (now.tm_wday + 1) % 7, 0, 6),  # cron: 0=Sun, time: 0=Mon
    ]
    for field, current, lo, hi in checks:
        if not _cron_field_matches(field, current, lo, hi):
            return False
    return True


def _cron_field_matches(field, value, lo, hi):
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                if (value - lo) % step == 0:
                    return True
            elif "-" in base:
                start, end = map(int, base.split("-", 1))
                if start <= value <= end and (value - start) % step == 0:
                    return True
        elif "-" in part:
            start, end = map(int, part.split("-", 1))
            if start <= value <= end:
                return True
        elif part == "*":
            return True
        else:
            if int(part) == value:
                return True
    return False


def start_scheduler():
    """Background thread that checks cron schedules every 60 seconds."""
    last_fired = {}

    def loop():
        while True:
            time.sleep(30)
            now = time.localtime()
            minute_key = time.strftime("%Y-%m-%dT%H:%M", now)
            try:
                config = load_config()
            except Exception:
                continue
            for agent in config.get("agents", []):
                schedule = agent.get("schedule")
                if not schedule:
                    continue
                fire_key = f"{agent['name']}:{minute_key}"
                if fire_key in last_fired:
                    continue
                if cron_matches(schedule, now):
                    last_fired[fire_key] = True
                    log.info("Scheduler triggered: agent=%s schedule=%s", agent["name"], schedule)
                    defaults = {}
                    for p in agent.get("params", []):
                        if p.get("default"):
                            defaults[p["name"]] = p["default"]
                    spawn_agent(agent["name"], defaults)
            # Prune old keys
            for k in list(last_fired):
                if not k.endswith(minute_key):
                    del last_fired[k]

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    log.info("Scheduler started")


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/config":
            self._json_response(load_config())
        elif path == "/api/runs":
            self._json_response(self._merged_runs())
        elif re.match(r"/api/runs/\d+$", path):
            run_id = int(path.split("/")[-1])
            with lock:
                run = runs.get(run_id)
            if run:
                self._enrich_run(run)
                self._json_response(run)
            else:
                self._json_response({"error": "Not found"}, 404)
        elif path == "/api/outputs":
            self._json_response(self._list_outputs())
        elif path.startswith("/api/outputs/"):
            self._serve_output_file(path)
        else:
            # Serve static files
            if path == "/":
                path = "/index.html"
            file_path = STATIC_DIR / path.lstrip("/")
            resolved = file_path.resolve()
            if not str(resolved).startswith(str(STATIC_DIR)):
                self._json_response({"error": "Forbidden"}, 403)
                return
            if file_path.exists() and file_path.is_file():
                self._serve_static(file_path)
            else:
                # SPA fallback
                self._serve_static(STATIC_DIR / "index.html")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if path == "/api/config":
            body = json.loads(raw) if raw else {}
            self._save_config(body)
        elif path == "/api/config/raw":
            self._save_config_raw(raw.decode("utf-8"))
        elif re.match(r"/api/agents/[^/]+/run", path):
            body = json.loads(raw) if raw else {}
            agent_name = path.split("/")[3]
            self._run_agent(agent_name, body)
        elif path == "/api/flag":
            body = json.loads(raw) if raw else {}
            self._toggle_flag(body)
        else:
            self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, file_path):
        ext = file_path.suffix
        content_types = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        ct = content_types.get(ext, "application/octet-stream")
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _save_config(self, body):
        try:
            with open(CONFIG_PATH, "w") as f:
                yaml.dump(body, f, default_flow_style=False, width=1000)
            self._json_response({"ok": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 400)

    def _save_config_raw(self, text):
        try:
            yaml.safe_load(text)
            with open(CONFIG_PATH, "w") as f:
                f.write(text)
            self._json_response({"ok": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 400)

    def _run_agent(self, agent_name, params):
        result = spawn_agent(agent_name, params)
        if result is None:
            self._json_response({"error": "Agent not found"}, 404)
        else:
            self._json_response(result)

    def _toggle_flag(self, body):
        output_dir = body.get("outputDir")
        flag = body.get("flag")
        if not output_dir or flag not in ("pinned", "starred"):
            self._json_response({"error": "outputDir and flag (pinned|starred) required"}, 400)
            return
        meta_path = Path(output_dir) / ".run.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                pass
        meta[flag] = not meta.get(flag, False)
        meta_path.write_text(json.dumps(meta, default=str))
        with lock:
            for r in runs.values():
                if r["outputDir"] == output_dir:
                    r[flag] = meta[flag]
                    break
        self._json_response({flag: meta[flag]})

    def _enrich_run(self, run):
        out_dir = Path(run.get("outputDir", ""))
        if out_dir.exists():
            run["files"] = sorted(f.name for f in out_dir.iterdir() if f.suffix == ".md")
        else:
            run["files"] = []

    def _merged_runs(self):
        config = load_config()
        base_dir = Path(config.get("outputDir", "./outputs")).resolve()

        with lock:
            in_memory = {r["outputDir"]: dict(r) for r in runs.values()}

        for r in in_memory.values():
            self._enrich_run(r)

        known_dirs = set(in_memory.keys())

        if base_dir.exists():
            for agent_dir in sorted(base_dir.iterdir()):
                if not agent_dir.is_dir():
                    continue
                for run_dir in sorted(agent_dir.iterdir(), reverse=True):
                    if not run_dir.is_dir() or str(run_dir) in known_dirs:
                        continue
                    files = sorted(f.name for f in run_dir.iterdir() if f.suffix == ".md")
                    if not files:
                        continue
                    params = {}
                    started = None
                    meta = {}
                    meta_path = run_dir / ".run.json"
                    if meta_path.exists():
                        try:
                            with open(meta_path) as mf:
                                meta = json.load(mf)
                                params = meta.get("params", {})
                                started = meta.get("startedAt")
                        except Exception:
                            pass
                    stdout_path = run_dir / ".stdout.log"
                    stderr_path = run_dir / ".stderr.log"
                    in_memory[str(run_dir)] = {
                        "id": None,
                        "agent": agent_dir.name,
                        "params": params,
                        "command": None,
                        "outputDir": str(run_dir),
                        "status": meta.get("status", "completed"),
                        "startedAt": started or run_dir.name.replace("run-", ""),
                        "finishedAt": meta.get("finishedAt"),
                        "exitCode": meta.get("exitCode"),
                        "stdout": stdout_path.read_text() if stdout_path.exists() else "",
                        "stderr": stderr_path.read_text() if stderr_path.exists() else "",
                        "files": files,
                        "pinned": meta.get("pinned", False),
                        "starred": meta.get("starred", False),
                    }

        for r in in_memory.values():
            if "pinned" not in r or "starred" not in r:
                meta_path = Path(r["outputDir"]) / ".run.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                        r.setdefault("pinned", meta.get("pinned", False))
                        r.setdefault("starred", meta.get("starred", False))
                    except Exception:
                        r.setdefault("pinned", False)
                        r.setdefault("starred", False)
                else:
                    r.setdefault("pinned", False)
                    r.setdefault("starred", False)

        result = sorted(in_memory.values(), key=lambda r: (r.get("pinned", False), r.get("startedAt") or ""), reverse=True)
        return result

    def _list_outputs(self):
        config = load_config()
        base_dir = Path(config.get("outputDir", "./outputs")).resolve()
        if not base_dir.exists():
            return []

        results = []
        for agent_dir in sorted(base_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            for run_dir in sorted(agent_dir.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
                files = [f.name for f in run_dir.iterdir() if f.suffix == ".md"]
                if files:
                    params = {}
                    meta_path = run_dir / ".run.json"
                    if meta_path.exists():
                        try:
                            with open(meta_path) as mf:
                                params = json.load(mf).get("params", {})
                        except Exception:
                            pass
                    results.append({
                        "agent": agent_dir.name,
                        "run": run_dir.name,
                        "path": str(run_dir),
                        "files": sorted(files),
                        "params": params,
                    })
        return results

    def _serve_output_file(self, path):
        parts = path.split("/")
        if len(parts) < 6:
            self._json_response({"error": "Invalid path"}, 400)
            return

        agent, run, filename = parts[3], parts[4], "/".join(parts[5:])
        config = load_config()
        base_dir = Path(config.get("outputDir", "./outputs")).resolve()
        file_path = (base_dir / agent / run / filename).resolve()

        if not str(file_path).startswith(str(base_dir)):
            self._json_response({"error": "Forbidden"}, 403)
            return
        if not file_path.exists():
            self._json_response({"error": "File not found"}, 404)
            return

        content = file_path.read_text()
        self._json_response({"content": content, "file": filename})

    def log_message(self, format, *args):
        pass  # quiet


def main():
    port = int(os.environ.get("PORT", 8080))

    class ReusableServer(http.server.HTTPServer):
        allow_reuse_address = True

    server = ReusableServer(("0.0.0.0", port), Handler)
    start_scheduler()
    print(f"Agent Dashboard running at http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
