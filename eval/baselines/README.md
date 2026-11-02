# Baseline recordings

A recording is the raw output of one evaluation run: `gira-iso eval --record <path>` writes the
model id, the panel and every case's raw response. Replaying it re-scores the same answers without
touching the network (`gira-iso eval --replay <path>`), which is what makes the numbers auditable.

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

## How a recording is used

There is no CI in this repository. A committed recording is replayed locally:

```bash
for recording in eval/baselines/*.json; do
  uv run gira-iso eval --replay "$recording"
done
```

A replay fails only when the recording itself is unreadable or invalid (exit 2 or 3). A score below
the expectation is reported, not imposed: the baseline is a regression reference, not a gate on model
quality.

## Current state

No recording is committed yet. The endpoint answers and lists its models, but `/v1/models` is public,
so it proves no credential: `/v1/chat/completions` rejects the key available in this environment with
`401 invalid_api_key`. Until a working key exists there is nothing to record. This is tracked in
`backlog.md` (B-01) and in `docs/plan-v1.md` (F5).
