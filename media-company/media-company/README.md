# MEDIA COMPANY: Factory Core

A channel-agnostic factory for automated media production. Channels are **data**
(`channels/<channel>/config.yaml`); the Factory is shared code that processes a
`ChannelConfig` + `DirectorPlan` through recoverable, retryable stages.

Status: **Factory Core skeleton with fake stages.** No real Gemini, Pollinations, Edge TTS,
FFmpeg or YouTube integration exists yet; those plug into the `Stage` protocol later.

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest                                   # no credentials needed
python -m factory validate-config                  # check every channels/*/config.yaml
FACTORY_MODE=TEST python -m factory run --channel drama --pipeline fake
```

`FACTORY_MODE` is `SHUTDOWN`, `TEST` or `AUTO`. A missing or invalid value fails safe to
`SHUTDOWN`.

## Add a channel

Create `channels/<id>/config.yaml` (copy `channels/drama/config.yaml`), then run
`python -m factory validate-config`. No Python changes.

## Layout

```
factory/contracts/   typed contracts: ChannelConfig, DirectorPlan, Job, Stage, ids, errors
factory/core/        orchestrator, state machine, retry, policy, workspace, events, redaction
factory/state/       StateStore protocol + local JSON-file implementation
factory/stages/      fake stages (real providers will implement the same Stage protocol)
channels/            one config.yaml per channel
schemas/             director_plan.schema.json (generated; `python -m factory schema --write`)
docs/ARCHITECTURE.md how the pieces interact, decisions, known gaps
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
