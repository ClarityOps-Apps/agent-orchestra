# Agent Orchestra Runtime

Phase 1 runtime scaffold for Agent Orchestra.

Day 1 proves the skeleton:

```bash
python orchestra.py "hello team"
```

Expected result: signed log entries from Atlas and Cody, plus lifecycle entries in `memory/activity.log`.

No real model or MCP calls are made on Day 1. API credentials belong in `.env` on the target machine and are never committed.
