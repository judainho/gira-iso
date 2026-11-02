# Baseline recordings

A recording is the raw output of one evaluation run: `gira-iso eval --record <path>` writes the
model id, the panel and every case's raw response. Replaying it re-scores the same answers without
touching the network (`gira-iso eval --replay <path>`), which is what makes the numbers auditable and
what CI repeats on every push.

## How to add one

```bash
export GIRA_ISO_LLM_API_KEY=...
export GIRA_ISO_LLM_MODEL=deepseek-v4.1-flash-1017
uv run gira-iso eval --record eval/runs/2026-11-01-current.json
cp eval/runs/2026-11-01-current.json eval/baselines/
```

Name the file after the date and the model: `<date>-<model>.json`. Before committing it, read it: it
holds prompts and model responses, and it must never hold a credential.

`eval/runs/` stays git-ignored (those are run artifacts); this directory is the committed record.

## What CI does with it

The `ci` workflow replays every `*.json` here. It fails only when the replay itself fails (exit 2 or
3: unreadable or invalid recording). A score below the expectation is reported, not imposed: the
baseline is a regression reference, not a gate on model quality.

## Current state

No recording is committed yet. The endpoint answers and lists its models, but `/v1/models` is public,
so it proves no credential: `/v1/chat/completions` rejects the key available in this environment with
`401 invalid_api_key`. Until a working key exists there is nothing to record. This is tracked in
`backlog.md` (1.1) and in `docs/plan-v1.md` (F5).
