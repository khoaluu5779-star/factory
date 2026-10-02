# Architecture

## 1. Shape of the system

```
ChannelConfig (YAML) ─┐
                      ├─> Director stage ──> DirectorPlan (versioned) ──> Factory stages
Research output ──────┘                                   │
                                                          ▼
        research → director → images → tts → render → qc → publish      (default pipeline)
              │        │         │      │       │      │       │
              └────────┴─────────┴──────┴───────┴──────┴───────┴──> Job record (StateStore)
                                                                    + temp media (Workspace)
```

The Factory never asks "is this a Drama video?". It processes a `ChannelConfig` and a
`DirectorPlan`. Everything channel-specific is data in `channels/<id>/config.yaml`.

## 2. Contracts (`factory/contracts`)

| Contract | Purpose | Version field |
|---|---|---|
| `ChannelConfig` | Niche, DNA, visual style, formats, voices, CTA, publishing defaults, secret *names* | `config_version` |
| `DirectorPlan` | Everything downstream needs: concept, hook, characters, scenes, lines, image prompts, sound/VFX intent, ending, CTA, publishing metadata, experiment block | `schema_version` |
| `Job` | One persisted record per run: stage records, failures, costs, experiment metadata | `state_schema_version` |
| `Stage` / `StageResult` / `StageContext` | What a stage receives and returns | n/a |

Design points:

* **DirectorPlan** keeps a character "bible" in one central list. Scenes reference characters
  by id and carry only a short (`<= 600` chars) image prompt. Voice *roles* (`narrator`,
  `protagonist`) are abstract; the channel maps roles to provider voices.
* Models are strict (`extra="forbid"`): unknown keys are a contract violation, not ignored.
* `parse_director_plan` checks `schema_version` first and raises `UnsupportedSchemaVersion`.
  Adding version `1.1` means extending `SchemaVersion` and adding a migration.
* `schemas/director_plan.schema.json` is generated from the model; a test fails if it drifts.
  Use it to instruct the Director and for external validation.
* Cross-contract checks (`check_plan_against_channel`) cover allowed format, duration range,
  voice roles defined by the channel, and experiment id prefix.
* YAML is loaded with `safe_load` and holds data only. Secrets appear as environment-variable
  *names* (`secret_env`); a value that does not look like a variable name is rejected.

## 3. Job ID

`JOB-YYYYMMDD-<channel>-<github_actions_run_id>` (for example `JOB-20260927-drama-9876543210`).
Locally, a millisecond timestamp replaces the run id. The ID is created once and appears in the
job record, every event, the stored documents, the DirectorPlan (`job_id` must match), the
workspace directory name and the video record. Because the run id is stable across "re-run
jobs" in Actions, a re-run maps to the same Job ID and resumes instead of duplicating.

## 4. State machine (`core/state_machine.py`)

Stage: `PENDING → RUNNING → SUCCESS | FAILED`; `FAILED → RUNNING` (retry) or `PENDING`
(reset); `SUCCESS → PENDING` only by invalidation; `PENDING → SKIPPED | CANCELLED`;
`SKIPPED → PENDING` (re-evaluated on resume). `CANCELLED` is terminal.

Job: `PENDING → RUNNING → COMPLETED | FAILED | CANCELLED`; `FAILED → RUNNING` only through an
explicit resume. `COMPLETED` and `CANCELLED` are terminal. A TEST job ends `COMPLETED` with the
publish stage `SKIPPED`.

All status changes go through `transition_stage` / `transition_job`, which raise
`InvalidTransition` on anything outside the tables.

## 5. Retries (`core/retry.py`)

Retries happen inside the failed stage. Completed stages are never re-run by a retry.

* `RetryPolicy`: `max_attempts` (1 to 10, hard cap), exponential backoff, capped delay,
  deterministic (no jitter). Per-stage overrides via `RetryConfig.per_stage`.
* `PermanentStageError` is never retried (bad input, failed quality gate).
  `RetryableStageError`, `ContractViolation` and unknown exceptions are retried until the cap.
* Every failure is kept in `StageRecord.failures` (attempt, type, redacted message, redacted
  traceback). History is never trimmed, even after invalidation or an explicit resume.

## 6. Kill safety and recovery

1. The attempt is counted and the job saved **before** a stage runs.
2. A stage found `RUNNING` on resume was interrupted: it becomes a failed attempt
   (`Interrupted`), so repeated kills exhaust the bounded budget rather than loop.
3. Each successful stage records what it produced:
   * **Workspace artifacts** (media): path, size, sha256. Verified on resume.
   * **Documents** (small JSON: research output, DirectorPlan, QC report, publication info):
     saved through the `StateStore`, so they survive a lost runner.
4. On resume the earliest `SUCCESS` stage whose artifacts or documents are missing or changed is
   reset **together with every later stage**; earlier stages are untouched. Example: after a
   lost runner, `research` and `director` (documents) are kept and `images → tts → render`
   run again, with no new Gemini call.
5. Explicitly resuming a `FAILED` job gives the failed stage a fresh attempt budget.
6. `run_channel` resumes an unfinished (`PENDING`/`RUNNING`) job for the channel before starting
   a new one. Starting with an already-seen run id also resumes.

Not solved here: two runners resuming the same job at once. Use a per-channel Actions
`concurrency` group (already in `factory.yml`); a lease in the durable StateStore is the
proper fix.

## 7. Modes and policy (`core/policy.py`)

The only place that decides what is allowed. Stages ask nothing; they are simply not invoked.

| Global mode | Channel state | Produce | Publish |
|---|---|---|---|
| SHUTDOWN | any | no | no |
| any | IDEA, PAUSE, KILL | no | no |
| TEST | any other | yes | **never** |
| AUTO | TEST | yes | no (channel not promoted) |
| AUTO | EVALUATING, CONTINUE, SCALE | yes | yes |

Assumption to confirm: a channel in lifecycle `TEST` does not publish even under `AUTO`
(publishing is irreversible, so promotion to `EVALUATING` is required). It is one row in
`_PUBLISH_ALLOWED`.

Publishing safety, layered:
1. The policy issues a sealed `PublishAuthorization`; it cannot be constructed elsewhere, and
   a test verifies that no module other than the policy calls the issuing function.
2. A stage with `requires_publish_authorization = True` is not invoked without one
   (recorded as `SKIPPED` with the reason), and the orchestrator raises `PolicyViolation` if
   asked to run it anyway.
3. `FACTORY_MODE` missing or invalid means `SHUTDOWN`.

Pausing or killing a channel = change `lifecycle_state` in its YAML. `cancel_job` cancels
unfinished work.

## 8. Persistence (`factory/state`)

`StateStore` is a small protocol: jobs, per-job documents, video records. `FileStateStore`
(atomic temp-file + rename writes, strict id validation, version checks) exists for local
development and tests.

**It is not durable on GitHub-hosted runners.** Until a private or external StateStore is
connected, cross-run recovery only works where the state directory persists. Nothing in the
Core depends on the file layout, so a replacement only has to implement the protocol.
Operational state is never written to a public branch.

Channel lifecycle currently comes from the config file. A store-backed override (so analytics
can flip a channel to `PAUSE` without a commit) is a natural extension and needs no Core change.

## 9. Temporary media (`core/workspace.py`)

`<workspace>/jobs/<job_id>/{images,audio,render,qc}`. Paths are confined to the job directory
(no absolute paths, `..` or symlink escapes). Cleanup deletes only that job's directory,
refuses symlinks and unexpected paths, never raises into the pipeline, and runs after a
completed job and after a terminal failure (configurable through `RunOptions`). `.gitignore`
and a test keep media and secrets out of Git.

## 10. Events, costs, experiments

* `Event` (job id, stage, attempt/max, message, data) goes to any `EventSink`. `LoggingSink`
  writes JSON lines; `MultiSink` isolates sink failures. A future Discord sink should react
  only to `JOB_COMPLETED`, `JOB_FAILED`, `STAGE_FAILED` and `STAGE_RETRY`, formatting from the
  event fields.
* Failure messages are redacted against secret-looking environment values before they are
  stored or emitted.
* Stages return `CostEntry` items (provider, amount, units); `Job.total_cost_usd()` and
  `cost_by_provider()` aggregate them. Amounts are 0.0 for free tiers.
* `ExperimentMeta` is filled from the DirectorPlan's `experiment` block once the Director
  succeeds. A published video produces a `VideoRecord` (`video_id → job_id, experiment_id`),
  which answers "what experiment produced this video?". `result` is reserved for analytics.

## 11. Adding things

* **New channel:** add `channels/<id>/config.yaml`; run `python -m factory validate-config`.
* **Real stage:** implement `name`, `requires_publish_authorization` and
  `run(ctx) -> StageResult`; write media under `ctx.workspace`, return relative paths, return
  small JSON as `documents`; raise `RetryableStageError` or `PermanentStageError`. Provider
  quota/rate-limit handling and fallbacks (for example TTS voice fallback) belong inside the
  stage or a provider interface behind it; the Core only sees success or a classified error.
* **New DirectorPlan version:** extend `SchemaVersion`, add a migration, regenerate the schema.

## 12. Known gaps and risks

* No durable StateStore yet (see section 8). Largest gap for production recovery.
* Publish is not idempotent across a crash between "video uploaded" and "job saved": a resume
  could upload twice. The real publisher must look up an existing upload (for example by a
  title/description marker or stored upload session) before uploading.
* A failed QC verdict should raise `PermanentStageError`; regenerating upstream stages on QC
  failure is intentionally not built (it would break the "smallest stage" rule).
* No concurrency or rate-limit enforcement beyond one job per channel via `concurrency`.
  Provider interfaces are the seam for quota tracking.
* Cost figures depend on stages reporting them; nothing verifies them.
* Redaction is best effort (environment values with sensitive-looking names, length >= 6).
