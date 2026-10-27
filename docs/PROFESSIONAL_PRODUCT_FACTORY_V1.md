# Professional Product Factory v1

This additive experimental backend converts an owner request into a versioned,
persisted ProjectSpec before producing code. It does not promote a research
recipe or change the pharmacy, single-call v6, or earlier normal-app paths.

## Specification and planning

`professional_project_spec_v1` records the product, users, journeys, requirements,
views/navigation, typed entities/references, validation, executable rules,
workflows, persistence, privacy, responsive behavior, all application states,
accessibility, acceptance criteria, assumptions, uncertainties and provenance.
Original source text and hashes remain separate from the provider's interpretation.
Additional extracted document sources can enter the same source interface; a PDF
extraction/upload UI is not included in this milestone.

The provider boundary uses the separate `professional_project_spec_wire_v1`
strict JSON schema. It is the common source for the required keys, prompt,
provider response format and local structural validator. The canonical station
representation remains ProjectSpec v1. The only decoding operations are removing
explicit optional nulls and indexing transition rows by their source state;
missing metadata or invalid values are never silently repaired. Money examples
use decimal major-unit strings; SQLite alone converts these to cents. Persistent
specifications must require restart persistence. An unconditional deletion ban
requires a source-grounded reason, distinct from automatic foreign-key protection.

Ora derives a DAG from the validated requirements. SQLite and workflow producers
run independently when needed. Domain, experience and assembly follow their
actual dependencies. Each task carries ownership, complete transitive producer
handoffs, versioned interfaces, checks, expected behavior and worker profile.
Absent capabilities do not cause decorative persona activity.

## Product framework and generated artifacts

The standard-library runtime is a deliberately bounded reusable framework.
Providers generate schema, workflow, domain configuration, product-specific UI
composition/copy and assembly modules. They consume explicit framework APIs;
they do not invent transport or persistence APIs. The runtime is pinned and copied
into each delivered project, which needs no factory or provider installation.
No acceptance-product names or fixture sources are used by the live backend.

Teuta v2 uses escaped component rendering and literal design configuration.
Raw CSS/JavaScript is never interpreted as Python formatting. Components supply
responsive navigation, tables/cards, labeled forms, visible focus, loading,
validation, empty/success/error states and destructive confirmation. Views derive
from the spec: a calculator, feed and business workflow have different structures.
No demonstration records or fabricated metrics ship in the owner database.

## Verification and delivery

Independent subprocess checks exercise generated producer interfaces, schema,
relationships, numeric validation, duplicates, transitions, notes, deletion,
derived counts and restart persistence. Actual installed Chromium renders empty
and populated products at 1280, 768 and 390 CSS pixels before Teuta completes.
DOM checks cover hierarchy, navigation, labels, control sizes, keyboard focus,
contrast, horizontal overflow, transport, invalid form feedback and raw errors.
The test retains reports and rendered DOM; it does not compare screenshots.

The existing six frozen gates remain unchanged. A separate final project copies
their verified bytes. Real HTTP create/update/note/workflow requests, navigation
and a stop/restart persistence check run against an isolated database. The same
launch helper powers Workbench OPEN APP. Medaurus receives actual spec, source
provenance, integrity, browser, frozen gate and packaging evidence before approval.
Any required failure withholds APP READY. Owner data starts empty.

Provider calls use the existing SDK/pricing boundary, pre-call cost reservations,
runtime freshness and no retries. Calls, source, ownership, events and checks are
retained per build. Fake SDK tests are explicitly labeled injected/offline.

## Boundaries

V1 supports local single-operator products, acyclic references, bounded arithmetic,
count metrics and declared rule types. External integrations require a provisioned
adapter and fail closed. This is not a general arbitrary-code security sandbox,
multi-user authentication system, or universal application generator. Browser
automation checks observable behavior; owner acceptance additionally judges actual
usability and operates the final product. One successful benchmark cannot establish
a global model ranking or broad production readiness.

## Preregistered real acceptance

After all offline suites pass, one PitlaneOne Lite build is authorized at a USD 2
ceiling through the actual Workbench professional backend, with no automatic paid
retry. The exact owner prompt and criteria are retained in
`build/professional-v1/acceptance_manifest.json` before submission. Acceptance
requires the unchanged gates, new product gates, runnable package, owner browser
customer/vehicle/work-order lifecycle, prices/notes, dashboard, invalid input,
reload, restart persistence and mobile usability. Failure is retained and diagnosed
offline; passing fake tests alone does not satisfy this milestone.
