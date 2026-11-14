# error-analysis

Claude skills for analysing production errors from Datadog. Standalone for now;
QZQA can reference it later.

## Skills

### datadog-error-patterns (Milestone 1, draft)
Groups a service's HTTP 4xx/5xx errors from Datadog APM into a few categories
with weighted K-means and summarizes each one (share, endpoints, status codes,
example messages, Error Tracking links). Reference service: subscriptions.

Location: `.claude/skills/datadog-error-patterns/`

## Use it locally

Inside this repo, Claude Code picks the skill up automatically (project skill).
To use it in every project, link it into your personal skills folder:

```bash
./install.sh            # symlinks into ~/.claude/skills/
```

Requires a Datadog MCP connection in Claude Code (use the `/v1/mcp` endpoint)
and Python 3 with the pinned packages:

```bash
pip install -r .claude/skills/datadog-error-patterns/scripts/requirements.txt
```

## Tests

```bash
python3 tests/test_cluster.py
```

## Roadmap
- M1.1: "cluster by" presets (root cause, endpoint, dependency)
- M2: flag pattern changes after deploys of the service, web_php and renderer
