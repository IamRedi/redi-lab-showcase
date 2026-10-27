# Professional Product Factory v1: first live acceptance

**Not accepted.** The single authorized build `wb-ef4a10903f78` stopped during
ProjectSpec validation. No coding station ran; all six final gates were NOT_RUN.
No app was packaged and no real-product browser acceptance was possible.

Execution commit: `083761068a006fc5e1063f1b1af537eaf18f2b2e`.
OpenAI / `gpt-5.6-terra`: one completed call, finish reason `stop`, 960 input
tokens (0 cached), 3,243 output tokens, 4,203 total tokens, USD 0.0408360.
Build elapsed time: 28.794 seconds. No paid retry occurred.

## Exact first failure and cause

`ValueError: unsupported ProjectSpec version` in specification validation.
The retained JSON response omitted `schema_version`. The factory requested it
in prose but omitted it from its explicit required-key list. The provider adapter
requested generic JSON-object mode, with no enforced ProjectSpec schema. Valid
fake fixtures always supplied the version, hiding this producer/consumer mismatch.

The primary classification is **factory specification-contract defect**, manifested
as a provider-generated specification omission. The provider transport completed
normally; this was not an API outage, generated-module error, assembly failure,
or final-gate failure. The retained run's coarse `specification_issue` category
is preserved; this report provides the more precise forensic classification.

Offline replay of the exact retained response reproduces the exception. A
metadata-only counterfactual passed the old validator but exposed further
inconsistencies: money examples were described as cents while the runtime accepts
major units, global deletion bans contradicted promised conditional deletion,
and `restart_required=false` contradicted persistence acceptance. Thus merely
inserting the missing version would have concealed other contract problems.

## Forward-only generic repair

The repair in this report's commit introduces one closed strict provider schema
shared by prompt construction, required-key declaration, SDK response format and
local validation. It includes the required version enum and all nested shapes.
Schema bytes are included in pre-call budget reservations. Unsupported strict
output is terminal; there is no JSON-mode fallback or automatic retry.

An explicit wire representation handles nullable optional fields and transition
rows without changing the canonical station APIs. Raw output is retained before
validation; deterministic decoding and its input hash are recorded separately.
No missing metadata is filled and no existing evidence is rewritten.

Money examples/defaults are decimal major-unit strings; persistence flags must
require restart verification; unconditional deletion prohibitions require a
source-grounded reason and are distinguished from normal foreign-key restrictions.
These controls improve the contract but cannot prove arbitrary natural-language
requirements are consistent. Independent review and real acceptance remain required.

Focused offline verification: 15 tests passed, covering exact failed-response
replay, strict SDK request shape, no fallback, semantic contradictions, three
product families, pre-completion Chromium checks, frozen gates and runnable
fake-provider delivery. Full validation results are retained separately in
`build/professional-v1/forensics/validation/`.

## Evidence and operational state

The authorized two-value transfer from PID 23292 to PID 20964 completed in memory.
PID 23292 stopped; one listener on 8765 was verified. Source/runtime identity
matched the execution commit before the provider call; only presence booleans
were recorded. Credential values were not displayed, logged or written to files.

Retained live evidence is under `build/workbench-builds/wb-ef4a10903f78/`.
The original input/output, events and final evidence remain unchanged. A byte-exact
response fixture supports repeatable offline regression. Forensic reports and
hash inventories are under `build/professional-v1/forensics/`.

The original 852-test suite and REDI-01 through REDI-10 passed before live
submission. The retained owner-ready Mini CRM remains separate and unmodified.
Offline browser checks are not substituted for real PitlaneOne acceptance.

There is no final PitlaneOne app path, launch command or OPEN APP URL. Customer,
vehicle, work-order, mobile and restart browser acceptance are NOT_RUN. The
required acceptance milestone remains unproven. Any further live build needs new
authorization after review of this evidence; none is initiated by the repair.
