#!/usr/bin/env python3
"""
logwatchd.py v1.1.0

Enterprise-grade daemon to tail log files, apply regex rules, send alerts (Slack/Email),
and expose Prometheus metrics, with robust logging, config validation, graceful shutdown,
and retry/backoff for alerting.
"""

import os
import sys
import signal
import asyncio
import time
import re
import logging
import logging.handlers
from pathlib import Path
from typing import List, Dict, Any
import yaml
import argparse
from collections import deque
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from aiohttp import web
from slack_sdk import WebClient
import smtplib
from email.message import EmailMessage
from pydantic import BaseModel, ValidationError, Field
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class AlertConfig(BaseModel):
    rate_limit: float = Field(..., ge=0, description="Minimum seconds between alerts")
    dedupe_window: float = Field(..., ge=0, description="Seconds to suppress duplicate messages")

class SlackConfig(BaseModel):
    token: str
    channel: str

class EmailConfig(BaseModel):
    smtp_server: str
    smtp_port: int
    username: str
    password: str
    sender: str
    recipients: List[str]

class MetricsConfig(BaseModel):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

class Config(BaseModel):
    log_paths: List[str]
    rule_dir: str
    check_interval: float = 0.5
    alert: AlertConfig
    slack: SlackConfig = None
    email: EmailConfig = None
    metrics: MetricsConfig = MetricsConfig()
    log_file: str = "logwatchd.log"
    debug: bool = False

# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------
logger = logging.getLogger("logwatchd")

def setup_logging(log_file: str, debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(fmt))
    fh = logging.handlers.TimedRotatingFileHandler(log_file, when="midnight", backupCount=7)
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(fmt))
    logger.addHandler(ch)
    logger.addHandler(fh)
    logger.debug("Logging initialized")

# -----------------------------------------------------------------------------
# Metrics
# -----------------------------------------------------------------------------
_METRICS: Dict[str, int] = {
    "lines_processed": 0,
    "rules_matched":   0,
    "alerts_sent":     0
}

# -----------------------------------------------------------------------------
# Rule Engine
# -----------------------------------------------------------------------------
class Rule:
    def __init__(self, spec: Dict[str, Any], defaults: AlertConfig):
        self.name = spec["name"]
        self.regex = re.compile(spec["pattern"])
        self.rate_limit = spec.get("rate_limit", defaults.rate_limit)
        self.dedupe_window = spec.get("dedupe_window", defaults.dedupe_window)
        self._last_sent = 0.0
        self._recent = deque()

    def match(self, line: str) -> bool:
        return bool(self.regex.search(line))

    def should_alert(self, line: str) -> bool:
        now = time.time()
        if now - self._last_sent < self.rate_limit:
            return False
        # cleanup old entries
        while self._recent and now - self._recent[0][0] > self.dedupe_window:
            self._recent.popleft()
        if any(msg == line for _, msg in self._recent):
            return False
        self._recent.append((now, line))
        self._last_sent = now
        return True

class RuleEngine:
    def __init__(self, defaults: AlertConfig):
        self.defaults = defaults
        self.rules: Dict[str, Rule] = {}

    def load_rule_file(self, path: str):
        logger.debug(f"Loading rules from {path}")
        data = yaml.safe_load(Path(path).read_text())
        for spec in data:
            rule = Rule(spec, self.defaults)
            self.rules[rule.name] = rule
            logger.info(f"Loaded rule: {rule.name}")

    def apply(self, line: str) -> List[str]:
        alerts = []
        for rule in self.rules.values():
            if rule.match(line) and rule.should_alert(line):
                _METRICS["rules_matched"] += 1
                alerts.append(rule.name)
        return alerts

# -----------------------------------------------------------------------------
# Watchdog Handler
# -----------------------------------------------------------------------------
class RuleDirHandler(FileSystemEventHandler):
    def __init__(self, engine: RuleEngine, rule_dir: str):
        super().__init__()
        self.engine = engine
        self.rule_dir = rule_dir

    def on_any_event(self, event):
        if event.is_directory: return
        if event.src_path.lower().endswith((".yaml", ".yml", ".json")):
            try:
                self.engine.load_rule_file(event.src_path)
            except Exception as e:
                logger.error(f"Failed to load rules from {event.src_path}: {e}")

# -----------------------------------------------------------------------------
# Alert Dispatchers
# -----------------------------------------------------------------------------
class AlertDispatcher:
    def __init__(self, cfg: Config):
        self.plugins = []
        if cfg.slack:
            self.plugins.append(SlackPlugin(cfg.slack))
        if cfg.email:
            self.plugins.append(EmailPlugin(cfg.email))

    async def dispatch(self, rule_name: str, line: str):
        for plugin in self.plugins:
            try:
                await plugin.send(rule_name, line)
                _METRICS["alerts_sent"] += 1
            except Exception as e:
                logger.warning(f"Plugin {plugin.name} error: {e}")

class SlackPlugin:
    name = "slack"

    def __init__(self, cfg: SlackConfig):
        self.client = WebClient(token=cfg.token)
        self.channel = cfg.channel

    async def send(self, rule_name: str, line: str):
        text = f"*{rule_name}* matched:\n```{line.rstrip()}```"
        async for attempt in AsyncRetrying(stop=stop_after_attempt(5), wait=wait_exponential()):
            with attempt:
                logger.debug("Sending Slack alert")
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.client.chat_postMessage(channel=self.channel, text=text)
                )

class EmailPlugin:
    name = "email"

    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg

    async def send(self, rule_name: str, line: str):
        msg = EmailMessage()
        msg["Subject"] = f"[Alert] {rule_name}"
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(self.cfg.recipients)
        msg.set_content(f"Rule {rule_name} matched:\n\n{line}")
        async for attempt in AsyncRetrying(stop=stop_after_attempt(5), wait=wait_exponential()):
            with attempt:
                logger.debug("Sending Email alert")
                def _send():
                    with smtplib.SMTP(self.cfg.smtp_server, self.cfg.smtp_port, timeout=10) as s:
                        s.starttls()
                        s.login(self.cfg.username, self.cfg.password)
                        s.send_message(msg)
                await asyncio.get_event_loop().run_in_executor(None, _send)

# -----------------------------------------------------------------------------
# File Tailer
# -----------------------------------------------------------------------------
async def tail_file(path: str, engine: RuleEngine, dispatcher: AlertDispatcher, interval: float):
    """Tail a file, detect rotation, apply rules, send alerts."""
    inode = None
    fh = None
    while True:
        try:
            stat = os.stat(path)
            if inode != stat.st_ino:
                inode = stat.st_ino
                if fh:
                    fh.close()
                    logger.info(f"Reopened rotated file: {path}")
                fh = open(path, "r")
                fh.seek(0, os.SEEK_END)
            line = fh.readline()
            if line:
                _METRICS["lines_processed"] += 1
                for rule_name in engine.apply(line):
                    await dispatcher.dispatch(rule_name, line)
            else:
                await asyncio.sleep(interval)
        except FileNotFoundError:
            logger.warning(f"File not found, retrying: {path}")
            await asyncio.sleep(interval)
        except Exception as e:
            logger.error(f"Error tailing {path}: {e}")
            await asyncio.sleep(interval)

# -----------------------------------------------------------------------------
# Metrics Server
# -----------------------------------------------------------------------------
async def metrics_handler(request):
    lines = [f"logwatchd_{k} {v}" for k, v in _METRICS.items()]
    text = "\n".join(lines) + "\n"
    return web.Response(text=text, content_type="text/plain")

async def start_metrics(cfg: MetricsConfig):
    if not cfg.enabled:
        return
    app = web.Application()
    app.add_routes([web.get("/metrics", metrics_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    logger.info(f"Metrics endpoint running at http://{cfg.host}:{cfg.port}/metrics")

# -----------------------------------------------------------------------------
# Graceful Shutdown
# -----------------------------------------------------------------------------
async def shutdown(loop, observer: Observer):
    logger.info("Shutting down...")
    observer.stop()
    observer.join()
    tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Log Monitoring & Alerting Daemon")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    try:
        raw = yaml.safe_load(Path(args.config).read_text())
        cfg = Config(**raw, debug=args.debug)
    except (FileNotFoundError, ValidationError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg.log_file, cfg.debug)
    logger.info("Starting logwatchd v1.1.0")

    engine = RuleEngine(cfg.alert)
    # initial load of all rule files
    for fn in Path(cfg.rule_dir).glob("*.y*ml"):
        engine.load_rule_file(str(fn))

    # watch rule directory
    observer = Observer()
    observer.schedule(RuleDirHandler(engine, cfg.rule_dir), cfg.rule_dir, recursive=False)
    observer.start()

    dispatcher = AlertDispatcher(cfg)

    loop = asyncio.get_event_loop()

    # schedule tailers
    for lp in cfg.log_paths:
        loop.create_task(tail_file(lp, engine, dispatcher, cfg.check_interval))

    # start metrics server
    loop.create_task(start_metrics(cfg.metrics))

    # handle signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(loop, observer)))

    try:
        loop.run_forever()
    finally:
        loop.close()
        logger.info("Exited cleanly")

if __name__ == "__main__":
    main()
