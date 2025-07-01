# LogWatch
#### Author: Bocaletto Luca

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/version-1.1.0-green.svg)](https://github.com/bocaletto-luca/LogWatch)

LogWatch is an enterprise-grade daemon that tails multiple log files with seamless rotation support, applies dynamic regex rules loaded from JSON/YAML, and dispatches alerts via Slack or email with built-in rate limiting and deduplication. It exposes Prometheus-style metrics, supports custom plugins, robust logging, graceful shutdown, and retry-backoff for fault resilience.

---

## Table of Contents

- [Overview](#overview)  
- [Features](#features)  
- [Prerequisites](#prerequisites)  
- [Installation](#installation)  
- [Configuration](#configuration)  
- [Usage](#usage)  
- [Configuration Reference](#configuration-reference)  
- [Metrics Endpoint](#metrics-endpoint)  
- [Logging](#logging)  
- [Contributing](#contributing)  
- [License](#license)  
- [Author](#author)  

---

## Overview

LogWatch continuously monitors one or more log files, handles file rotation automatically, applies user-defined regex rules with rate-limiting and deduplication, and sends alerts to Slack channels or via email. It also serves a `/metrics` endpoint for integration with Prometheus.

---

## Features

- Dynamic loading and reloading of rules (JSON/YAML) without restarting  
- Multi-file tailing with seamless rotation support  
- Rate-limiting and duplicate suppression per rule  
- Plug-in architecture for alert handlers (Slack, SMTP, custom)  
- Asynchronous, low-latency processing with `asyncio`  
- Exposed Prometheus-compatible metrics over HTTP  
- Robust logging with daily file rotation and adjustable verbosity  
- Graceful shutdown on SIGINT/SIGTERM with task cleanup  
- Retry/backoff logic for transient alert delivery errors  
- Schema-validated configuration using Pydantic  

---

## Prerequisites

- Python 3.8 or newer  
- IMAP/SMTP credentials (if using the email plugin)  
- Slack API token & channel (if using the Slack plugin)  
- GPG for PGP support (optional)  
- A terminal or container environment supporting Python  

---

## Installation

```bash
git clone https://github.com/bocaletto-luca/LogWatch.git
cd LogWatch
pip install -r requirements.txt
chmod +x logwatchd.py
```

---

## Configuration

Create or edit a `config.yaml` in the repository root:

```yaml
log_paths:
  - /var/log/nginx/access.log
  - /var/log/app/app.log

rule_dir: ./rules

check_interval: 0.5

alert:
  rate_limit: 60
  dedupe_window: 300

slack:
  token: xoxb-XXXXXXXXXXXX
  channel: alerts

email:
  smtp_server: smtp.example.com
  smtp_port: 587
  username: user@example.com
  password: secret
  sender: alerts@example.com
  recipients:
    - oncall@example.com

metrics:
  enabled: true
  host: 0.0.0.0
  port: 8000

log_file: logwatchd.log
debug: false
```

Place rule files in the `rules/` directory, for example:

```yaml
- name: High-Error-Rate
  pattern: 'ERROR'
  rate_limit: 120
  dedupe_window: 600
```

---

## Usage

```bash
./logwatchd.py --config config.yaml
```

Command-line options:

- `-c, --config` Path to configuration file (default: `config.yaml`)  
- `--debug` Enable debug-level logging  

LogWatch runs as a long-lived process. Press `Ctrl+C` to shut down gracefully.

---

## Configuration Reference

| Field             | Type     | Description                                       |
|-------------------|----------|---------------------------------------------------|
| `log_paths`       | list     | Paths of log files to monitor                     |
| `rule_dir`        | string   | Directory containing rule files                   |
| `check_interval`  | float    | Seconds between file reads                        |
| `alert`           | object   | Rate-limit/dedupe defaults for all rules          |
| `slack`           | object   | Slack plugin config (token, channel)              |
| `email`           | object   | Email plugin config (SMTP details, recipients)    |
| `metrics`         | object   | Metrics server config (enabled, host, port)       |
| `log_file`        | string   | Path for rotated log output                       |
| `debug`           | boolean  | Toggle debug logging                              |

---

## Metrics Endpoint

If enabled, visit `http://<host>:<port>/metrics` to retrieve Prometheus metrics:

```
logwatchd_lines_processed 12345
logwatchd_rules_matched   67
logwatchd_alerts_sent     12
```

Configure Prometheus scrape:

```yaml
- job_name: 'logwatchd'
  static_configs:
    - targets: ['localhost:8000']
```

---

## Logging

LogWatch uses Python’s `logging` module:

- Console output at INFO (or DEBUG with `--debug`)  
- Daily rotated log files (`log_file`) retained for 7 days  

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository  
2. Create a feature branch  
3. Write tests and update documentation  
4. Submit a pull request  

Refer to [PEP 8](https://peps.python.org/pep-0008/) for coding style.

---

## License

This project is licensed under the **GNU GPL v3**. See [LICENSE](LICENSE) for details.

---

## Author

Luca Bocaletto  
- Website: https://bocaletto-luca.github.io  
- GitHub: https://github.com/bocaletto-luca  
- Portfolio: https://bocalettoluca.altervista.org
