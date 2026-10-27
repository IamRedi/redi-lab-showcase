# VIRTUTES Workbench UI v1

This presentation layer uses the canonical **VIRTUTES — Workbench UI Direction
(Desktop + Mobile)**, Google document `1g2SUQ_qf4lrpF_aEvZeD_L_VKaXpkxMBHp47TYIHT7E`.
Both visual references were inspected. The written names and evidence rules
override generated mockup text.

## Boundaries

The existing local Workbench and build/open/stop APIs remain the execution path.
No frontend framework, external font, image service or provider is required.
`presentation.py` adds a read-only `presentation` field to build API responses.
It reads retained manifests, event timestamps, measured call facts and failure
stages without editing source records or overriding execution states.

Qualification is a different run from generation. Its source link is exposed
only when `qualification_origin.json` binds the exact source evidence hash.
The source DAG is clearly labeled with its own build ID and historical verdict;
qualification metrics and crew states remain separate. Historical unknown
duration is displayed as an em dash; recorded zero calls and cost stay zero.
Legacy in-flight placeholder call counts are unavailable until facts exist.

## Components and behavior

`static/index.html` provides the semantic shell. `workbench.css` holds shared
colors, typography, panels, inputs, buttons, status badges, persona marks,
task nodes, tabs, instruments, events, dialogs and responsive surfaces.
`view-model.js` is the pure graph/state/metric projection, tested offline with
Node. `workbench.js` binds the real APIs and patches changed regions only.
The server assembles these small files into the existing no-store HTML route.

Desktop uses intake / dominant factory / context columns and a slim telemetry
bar. Mobile uses Home and Run Details with a fixed two-action navigation bar;
completed results surface OPEN APP first. Tablet uses two columns with context
under the factory. Only the graph viewport pans horizontally.

The graph uses recorded task IDs and dependency edges, with Zojz and Ora as
the orchestration hierarchy. A task's state comes from its task record; persona
summary states still come from the existing crew mapper. Worker/model metadata
does not redefine persona identity. Graph nodes are keyed and retained across
polls, and ordinary status updates preserve pan/zoom. Fit centers wide graphs
at a readable minimum scale; the List control exposes full task descriptions
and dependency states without requiring graph interpretation.

WAITING and NOT_RUN are static. Only active states animate. Verified is stable
green, failure is red, and blocked is amber. Reduced-motion disables animation.
Labels, keyboard tabs, focus outlines, a native modal with focus restoration,
status announcements and an equivalent structured task list provide the
accessibility baseline. No unsupported pause/build-stop controls are shown.

## Verification workflow

Run focused tests:

```powershell
.venv/Scripts/python.exe -X utf8 -m pytest tests/unit/test_workbench_presentation.py tests/unit/test_workbench_selector_hydration.py
```

For deterministic browser development only:

```powershell
python -m scripts.workbench_visual_fixture --port 8771
```

The isolated fixture adapter cannot call providers or launch packages. Its
parallel, failed, blocked and 29-task scenarios carry a visible fixture label.
The actual Workbench never imports fixture data. Retained real runs validate
qualification, historical failure, Mini CRM, delivery and actual OPEN APP.

UI milestone screenshots and browser observations belong under
`build/virtutes-ui-v1/`. Regression validation uses the established full unit
suite and REDI-01 through REDI-10 with provider credentials removed and
external network denied. Historical hash manifests are checked separately.

## Deliberate limits

No upload control is advertised: specifications can be pasted into the existing
request field. No fake estimates, percentages, unsupported pause/resume actions
or inferred worker activity are added. Legacy records lacking a manifest show
station evidence rather than invented task dependencies. Wide graphs require
panning or the structured list at readable zoom levels.

## Visual reference correction

The visual-match pass makes the factory the primary stage: compact unboxed
side rails, a stronger VIRTUTES emblem/wordmark, a faint technical canvas and
thin station shells. Stable SVG role glyphs distinguish personas; bronze
orchestration, integration outlines and the selected run's recorded gate strip
distinguish production layers without adding tasks or inferred dependencies.
Only existing active states animate. Canonical role text remains available in
the structured list and selected-task context.

Delivery sits inside the factory as a compact strip with OPEN APP; supporting
metadata is disclosed through Details. Mobile Home uses a concise input console,
recent selection, persona overview and live context. Run Details makes the
graph principal, with completion first when present. Fit considers available
height as well as width, with readable minimum scales and panning for larger
graphs. Gate results and qualification provenance retain their original scope.

This correction changes only static UI files and this documentation. It leaves
the server, API, runtime identity, projection adapter and factory code unchanged.
Comparison screenshots and regression evidence are in
`build/virtutes-visual-match-v1/`.

## Full-viewport desktop shell

At desktop widths above 1000px, the Workbench is a `100dvh` application grid:
a 56px header, a bounded workspace and 46px telemetry. The workspace and its
three columns use `min-height: 0`; the factory canvas receives the remaining
space instead of contributing document height. The normal desktop document,
rails and graph do not expose vertical scrollbars. Tablet/mobile retains its
scrolling composition.

Graph row pitch and station height adapt to measured canvas height without
changing task identities, states or dependencies. Compact stations keep persona,
state and task text; large graphs use drag/keyboard pan, zoom and Fit. A canvas
ResizeObserver refits only after actual size changes, preserving zoom on polls.
The gate strip and delivery strip have bounded compact layouts.

Desktop settings, delivery details, Activity/Logs/Files/Evidence and the full
task list use a secondary native dialog. Existing DOM controls and handlers move
into that surface and return on close; canvas geometry and execution APIs stay
unchanged. Focus is restored to the invoking control. Browser evidence at the
four required desktop sizes belongs in `build/virtutes-full-viewport-v1/`.
