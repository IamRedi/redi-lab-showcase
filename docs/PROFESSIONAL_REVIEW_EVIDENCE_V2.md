# Product acceptance evidence v2

`professional_product_review_evidence_v2` is a closed structured envelope.
The public `EVIDENCE_SCHEMA` and validator live in `professional/review.py`.
Each observation retains its assertion ID, status, expected and observed value,
evidence type, hash-bound reference, failure reason, and requirement/criterion
IDs. The envelope binds ProductSpec, package file hashes, source build,
acceptance execution, timestamp and browser test implementation identity.

`medaurus_requirement_review_v2` is deterministic and provider-independent.
It consumes the entire envelope. REQUIRED criteria must all PASS; absent
observations remain NOT_RUN. N/A requires a ProductSpec reference and reason.
Requirements cannot be dropped or changed in an evidence payload. Reference
tampering invalidates approval. Unknown acceptance prose is retained as an
unmapped obligation, which fails closed. This is a bounded compiler for the
current local-product contract, not a general natural-language theorem prover.

Edit/delete obligations are derived from declared requirements. Optional
operations do not become universal release obligations. Explicit restrict_delete
rules require policy-enforcement proof, not a Delete button. Stateless specs
have a justified persistence N/A. The current runtime has asynchronous saving,
so its loading and network-error states are applicable and actually tested.

The browser collector retains individual positive and negative assertions:
navigation, semantic controls, labels, keyboard eligibility, focus, primary
action contrast, control size, responsive widths, overflow, same-origin
transport, validation, loading and success feedback, records/relationships,
editing, workflow transitions, dashboard counts and spec-permitted deletion.
It submits real generated forms in local Chrome. Disposable deletion records
are separate from the graph retained for restart testing. A transport failure
is injected only into an isolated QA frame for one save, explicitly identified
as fault injection; the generated source is unchanged. Direct owner acceptance
adds actual Tab-key and visual usability verification.

`AcceptanceLaunch` uses the same verified-package process launcher as owner
delivery but a separate QA database and process receipt. Startup checks PID,
project, database and package hash. Shutdown owns the process handle. Restart
checks a new process and unchanged state. No owner OPEN APP permission is
created by internal launch. The order is package, internal acceptance, full
evidence, deterministic Medaurus, release receipt, owner OPEN APP.

Future professional builds use this same collector and Medaurus rule; the
redundant paid prose-review call is removed from the planned call count.
Provider generation budgets and all six frozen gates remain unchanged.

Retained qualification copies only manifest-listed bytes into a new build
directory. It records the original build/result/reviewer and exact identities,
reruns the original frozen gate contract, collects new acceptance evidence,
and requires separate browser acceptance before issuing a receipt. Historical
run files are never rewritten. Owner data starts empty in the new app's data
directory; QA databases stay outside it. Workbench labels this as a retained
qualification with zero provider calls, not fresh generation. Receipt and
evidence hashes are rechecked before owner launch.

## Original rejection taxonomy (wb-43cf726ac7eb)

A. Produced but omitted: positive label/focus/contrast/validation observations
were hidden behind broad PASS labels; startup and restart process receipts were
on disk but omitted from reviewer inputs.

B. Never collected: operation-level successful edit/delete UI proof, complete
submission/loading/error/confirmation state observations, and full owner
keyboard journey. The previous `notes`/`duplicates` labels could also be emitted
for inapplicable fields.

C. Incorrectly mandatory: the actual owner OPEN APP action was treated as a
pre-release requirement. Delete, edit and loading are applicable to this
particular spec; they must not be dismissed as irrelevant to obtain approval.
For other specs, prohibited deletion or stateless persistence must be evaluated
under their own declared policy.

D. Circular dependency: owner OPEN APP required Medaurus approval while the
reviewer demanded proof of that owner action. Internal acceptance launch now
provides pre-release runtime evidence; owner action follows approval.

E. Actual retained-product defect: none established by the repaired automated
journey or direct Chrome acceptance. The original failed verdict remains intact.

Limitations: bounded local Python/SQLite products; conservative prose mapping;
browser focus eligibility is not a comprehensive assistive-technology audit.
Visual quality still needs direct inspection. Deterministic Medaurus is labeled
honestly and is not presented as a new independent LLM opinion.
