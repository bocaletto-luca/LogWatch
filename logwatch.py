#!/usr/bin/env python3
"""
logwatchd.py v1.0.0

Background daemon to tail log files, apply regex rules, send alerts (Slack/Email),
and expose Prometheus metrics.
"""

import os
import asyncio
import time
import re
import yaml
import argparse
from collections import deque, defaultdict
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from aiohttp import web
from slack_sdk import WebClient
import smtplib
from email.message import EmailMessage

# -----------------------------------------------------------------------------
# Configuration Loading
# -----------------------------------------------------------------------------
def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)

# -----------------------------------------------------------------------------
# Metrics
# -----------------------------------------------------------------------------
METRICS = {
    "lines_processed": 0,
    "rules_matched":   0,
    "alerts_sent":     0
}

# -----------------------------------------------------------------------------
# Rule Definition & Engine
# -----------------------------------------------------------------------------
class Rule:
    def __init__(self, spec, defaults):
        self.name = spec["name"]
        self.pattern = spec["pattern"]
        self.regex = re.compile(self.pattern)
        self.rate_limit = spec.get("rate_limit", defaults["rate_limit"])
        self.dedupe_window = spec.get("dedupe_window", defaults["dedupe_window"])
        self.last_sent = 0.0
        self.recent_messages = deque()  # (timestamp, message)

    def match(self, line):
        return self.regex.search(line)

    def should_alert(self, line):
        now = time.time()
        if now - self.last_sent < self.rate_limit:
            return False
        # dedupe: remove expired
        while self.recent_messages and now - self.recent_messages[0][0] > self.dedupe_window:
            self.recent_messages.popleft()
        # check duplicate
        if any(msg == line for _, msg in self.recent_messages):
            return False
        self.recent_messages.append((now, line))
        self.last_sent = now
        return True

class RuleEngine:
    def __init__(self, defaults):
        self.defaults = defaults
        self.rules = {}

    def load_rules(self, path):
        specs = yaml.safe_load(open(path))
        for spec in specs:
            self.rules[spec["name"]] = Rule(spec, self.defaults)

    def apply(self, line):
        alerts = []
        for rule in self.rules.values():
            if rule.match(line) and rule.should_alert(line):
                METRICS["rules_matched"] += 1
                alerts.append((rule.name, line))
        return alerts

# -----------------------------------------------------------------------------
# Dynamic Rule Loader
# -----------------------------------------------------------------------------
class RuleDirHandler(FileSystemEventHandler):
    def __init__(self, engine, rule_dir):
        self.engine = engine
        self.rule_dir = rule_dir
        super().__init__()

    def on_created(self, event):
        self._reload(event)

    def on_modified(self, event):
        self._reload(event)

    def _reload(self, event):
        if event.is_directory: return
        if event.src_path.endswith((".yaml", ".yml", ".json")):
            try:
                self.engine.load_rules(event.src_path)
                print(f"[RuleLoader] Reloaded rules from {event.src_path}")
            except Exception as e:
                print(f"[RuleLoader] Failed to load {event.src_path}: {e}")

# -----------------------------------------------------------------------------
# Alert Dispatcher & Plugins
# -----------------------------------------------------------------------------
class AlertDispatcher:
    def __init__(self, config):
        self.plugins = []
        if "slack" in config:
            self.plugins.append(SlackPlugin(config["slack"]))
        if "email" in config:
            self.plugins.append(EmailPlugin(config["email"]))

    async def dispatch(self, rule_name, line):
        for plugin in self.plugins:
            try:
                await plugin.send(rule_name, line)
                METRICS["alerts_sent"] += 1
            except Exception as e:
                print(f"[AlertDispatcher] Plugin error: {e}")

class SlackPlugin:
    def __init__(self, cfg):
        self.client = WebClient(token=cfg["token"])
        self.channel = cfg["channel"]

    async def send(self, rule_name, line):
        text = f":warning: *{rule_name}* matched: `{line.strip()}`"
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.client.chat_postMessage(channel=self.channel, text=text)
        )

class EmailPlugin:
    def __init__(self, cfg):
        self.cfg = cfg

    async def send(self, rule_name, line):
        msg = EmailMessage()
        msg["Subject"] = f"[Alert] {rule_name}"
        msg["From"] = self.cfg["from"]
        msg["To"] = ", ".join(self.cfg["to"])
        msg.set_content(f"Rule *{rule_name}* matched:\n\n{line}")
        def _send():
            with smtplib.SMTP(self.cfg["smtp_server"], self.cfg["smtp_port"]) as s:
                s.starttls()
                s.login(self.cfg["username"], self.cfg["password"])
                s.send_message(msg)
        await asyncio.get_event_loop().run_in_executor(None, _send)

# -----------------------------------------------------------------------------
# Log Tailer
# -----------------------------------------------------------------------------
async def tail_file(path, engine, dispatcher, interval):
    """Asynchronously tail a file, handle rotation."""
    try:
        stat = os.stat(path)
        inode = stat.st_ino
        with open(path, "r") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    METRICS["lines_processed"] += 1
                    for rule_name, text in engine.apply(line):
                        await dispatcher.dispatch(rule_name, text)
                else:
                    await asyncio.sleep(interval)
                    # detect rotation
                    try:
                        new_stat = os.stat(path)
                        if new_stat.st_ino != inode:
                            f.close()
                            stat = new_stat
                            inode = stat.st_ino
                            f = open(path, "r")
                            print(f"[Tailer] Detected rotation, reopened {path}")
                    except FileNotFoundError:
                        await asyncio.sleep(interval)
    except Exception as e:
        print(f"[Tailer] Error tailing {path}: {e}")

# -----------------------------------------------------------------------------
# Metrics HTTP Server
# -----------------------------------------------------------------------------
async def metrics_handler(request):
    lines = []
    for k, v in METRICS.items():
        lines.append(f"logwatchd_{k} {v}")
    return web.Response(text="\n".join(lines)+"\n", content_type="text/plain")

async def start_metrics_server(host, port):
    app = web.Application()
    app.add_routes([web.get("/metrics", metrics_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[Metrics] Serving on http://{host}:{port}/metrics")

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Log Monitoring & Alerting Daemon")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Parse rules but do not alert")
    args = parser.parse_args()

    cfg = load_config(args.config)
    defaults = cfg.get("alert", {})
    engine = RuleEngine(defaults)
    # initial load
    for fn in os.listdir(cfg["rule_dir"]):
        path = os.path.join(cfg["rule_dir"], fn)
        if os.path.isfile(path):
            engine.load_rules(path)

    # watch rule dir
    observer = Observer()
    handler = RuleDirHandler(engine, cfg["rule_dir"])
    observer.schedule(handler, cfg["rule_dir"], recursive=False)
    observer.start()

    dispatcher = AlertDispatcher(cfg.get("alert_plugins", cfg))

    loop = asyncio.get_event_loop()

    # schedule tailers
    for lp in cfg["log_paths"]:
        loop.create_task(tail_file(lp, engine, dispatcher, cfg.get("check_interval", 0.5)))

    # start metrics if enabled
    if cfg.get("metrics", {}).get("enabled", False):
        m = cfg["metrics"]
        loop.create_task(start_metrics_server(m.get("host", "0.0.0.0"), m.get("port", 8000)))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()

if __name__ == "__main__":
    main()
