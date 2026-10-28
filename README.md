# REDI Lab / VIRTUTES

**An adaptive AI software factory.** REDI Lab treats software delivery like a
production line: fixed, governed **stations** do the work, and the AI models
that staff them are **replaceable workers**. Every step passes explicit quality
gates and leaves evidence that can be replayed and audited later.

![VIRTUTES Workbench](docs/media/workbench.png)

## See it work

![VIRTUTES demo](docs/media/virtutes-demo.gif)

The demo (full-quality [MP4](docs/media/virtutes-demo.mp4)) opens a retained real-provider build, **ILIRIA**,
in the Workbench. ILIRIA is a creative production board that the Professional product factory built
from one request in 6 model calls, about 49k tokens, USD 0.15 and 1 min 14 s. The demo then
opens the generated app and creates real records. The app enforces its own workflow: new videos
must start in *Idea* and move forward stage by stage.

| Factory view: every station verified | Evidence and instrumentation |
| --- | --- |
| ![Factory view](docs/media/factory-app-ready.png) | ![Evidence](docs/media/evidence.png) |
| **The generated app with real records** | **Mobile Workbench** |
| ![Generated app](docs/media/generated-app-board.png) | <img src="docs/media/mobile-workbench.png" width="260" alt="Mobile Workbench"> |

### Governance in action: live builds from scratch

![Live build of a workshop job tracker](docs/media/live-build-workshop.gif)

This recording ([MP4](docs/media/live-build-workshop.mp4), sped up 3×) is a real-provider build from
scratch of a car-repair **Workshop Job Tracker**. It did **not** reach APP READY, and the factory said
so instead of shipping. Across four live runs, totalling USD 1.03 of real spend under a USD 2.00 owner
budget, the line:

- **Paused for the owner.** Ora stopped planning whenever the brief left something open. Each answer
  becomes a new append-only segment of the same run, hash-chained to the previous one, and there can be
  any number of them. One run went through four owner decisions in a row.
- **Built in parallel, live.** Andinus (data) and Bindus (workflow) worked at the same time, then Agron
  and Teuta. The factory graph and crew update from the running worker, also inside a resumed segment.
- **Refused to fake a pass.** Teuta's UI was checked in a real browser. When a check failed, the line
  tried a same-station correction and then a stronger worker, and finally stopped with the exact failed
  assertion as evidence.

These runs exposed three defects in the factory's own checks, and they are fixed with tests:
- Empty boards rendered no workflow columns.
- A prohibition ("cannot / not move backward") was read as a required reverse move.
- Board summaries were expected where the renderer never places them.

The last run used the final remaining budget before the reverse-move fix existed, so a passing live
build of this app is still outstanding.

| Personas working in parallel | Third owner decision in one run |
| --- | --- |
| ![Personas working](docs/media/live-personas-working.png) | ![Owner decision](docs/media/live-third-owner-decision.png) |
| **Live graph inside a resumed segment** | **Stopped with exact evidence** |
| ![Live continuation graph](docs/media/live-graph-continuation.png) | ![Teuta blocked](docs/media/live-teuta-blocked.png) |

## Core ideas

- **Stations vs. workers.** A station (spec, build, test, review, visual…)
  defines the contract: inputs, outputs, gates and budget. The worker filling
  it (a model or a deterministic fake) can be swapped without changing the line.
- **Quality gates.** Each hand-off is checked against typed domain contracts
  before the next station may start. Failures stop the line with a recorded
  reason instead of drifting silently.
- **Evidence ledger and replay.** Calls, prompts, outputs, costs and verdicts
  are hashed into an append-only ledger, so a run can be replayed and its
  provenance reconciled.
- **Recipes.** Successful line configurations are stored as recipes and reused
  for warm starts on new work.
- **Minimum Capable Worker (MCW) experiments.** Paired and hold-out experiments
  measure the cheapest worker that still passes a station's gates, under an
  explicit cost ceiling.
- **Governed recovery.** Bounded same-run recovery and correction loops, with
  build budgets and runtime freshness checks.

## VIRTUTES Workbench

A local, dependency-free web Workbench shows the factory as a live task graph.
The crew is presented as personas: **Zojz** and **Ora** (orchestration),
**Agron**, **Teuta**, **Andinus**, **Bindus**, **Redon**, **Bato**,
**Monunius**, **Medaurus**, **Gentius** and **ZANA** (visual station). It works
on desktop and mobile, with budget controls, per-task telemetry and an
"open app" result view.

## Proven results

- A **Mini CRM** built end to end by the factory line.
- A **multi-station parallel live run** across a dependency DAG.
- **974 tests**, passing on Windows and Linux without extra settings.
- **Zero runtime dependencies**: Python standard library only (Python 3.11+).

## Getting started

```bash
pip install -e .[dev]
pytest
```

Start the Workbench (offline by default, using deterministic fake workers):

```bash
python -m redi_lab.workbench.server --port 8765
# then open http://127.0.0.1:8765/
```

### Live provider calls

Nothing contacts a model provider unless you arm it explicitly. Live runs need:

- your own key in `REDI_OPENAI_API_KEY`
- `REDI_LIVE_PROVIDER=1`
- the `--live` flag on the relevant script, plus its cost ceiling

No keys are included in this repository.

## Repository layout

| Path | Contents |
| --- | --- |
| `redi_lab/` | Domain contracts, factory line, gates, evidence ledger, replay, recipes, experiments, Workbench |
| `tests/` | Unit and integration tests (offline) |
| `scripts/` | Validation and experiment runners |
| `docs/` | Architecture, research charter, threat model, data policy, station designs |
| `experiments/` | Experiment bindings and fixtures |

## License

Source-available for reading and evaluation only. See [LICENSE](LICENSE).
