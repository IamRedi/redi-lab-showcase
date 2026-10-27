# ZANA visual station v1

ZANA is the VIRTUTES responsibility for generated creative assets. Teuta owns the page and component structure; Gentius owns assembly and release. A visual worker is injected behind ZANA and receives only a versioned VisualTask. No model or vendor is part of the station contract.

## Product and task contracts

The existing ProjectSpec v1 and its historical wire schema remain accepted. ProjectSpec v2 adds a required `visual_requirements` list. An empty list schedules no visual task. Each explicit requirement records its ID, purpose, asset type, placement (`view:hero|section|background|thumbnail`), aspect ratio, minimum dimensions, style, subject, content and forbidden constraints, text permission, transparency, variant count, PNG format, blocking status and layout dependency. Validation rejects unknown views, unsupported formats, excessive dimensions and oversized task context. Consequential uncertainty remains in `uncertainties` and uses the owner decision flow.

Ora emits one `zana_visual_task_v1` per requirement. The task includes the logical run, requirement, bounded direction, consumers and checks. A `visual_first` requirement makes Teuta depend on ZANA. A `parallel` requirement lets Teuta build layout while ZANA generates. Gentius waits for both. The visual task is never passed to the text-code provider; `VisualWorker.generate(task, variant_index, failure_evidence)` is the replaceable boundary.

## Evidence and integration

Each attempted PNG is retained under `visual/<task>/attempt-NNN/` before validation, including failed bytes and their hash. A passing `visual_asset_manifest_v1` records the worker profile, provider/model references, safe parameters, prompt-context hash, file path/type, dimensions, ratio, size, content hash, time, cost, provider call ID when available, independent review and consumers. Variant IDs are distinct. The final integration receipt records the selected asset's exact package path and `actually_used` result; earlier attempt manifests remain intact.

Deterministic gates check PNG structure, pixel stream, dimensions, ratio within 2%, minimum size, transparency when required, local retention and exact hash. An independent reviewer must accept the visual. Teuta binds placement to asset ID in its literal DESIGN contract. Gentius copies selected retained bytes into the package and writes a versioned visual handoff. The trusted renderer serves only governed local `/assets/<id>.png` paths. Chromium checks that each required image loaded at its declared placement with the expected dimensions. A generated image absent from the final page does not satisfy its requirement. Medaurus maps the browser observation to the visual requirement and to applicable acceptance prose.

## Workbench, accounting and recovery

The canonical crew has 12 personas. ZANA is WAITING without a visual task; WORKING and VERIFYING require real task events; VERIFIED requires completed gates and retained bytes. Failed or blocked work is shown from task and owner-action evidence. The active count is derived from states. The existing 11 roles keep their former mappings.

Visual telemetry records worker invocations, provider calls, duration, cost, retries, accepted and rejected variants, retained assets and failures. Visual cost is included in total product cost. Typed failures distinguish provider unavailability/restriction, budget, invalid output, decode, dimensions, requirement mismatch, retention, integration and independent review rejection. The existing bounded recovery policy keeps all failed attempts. A same-worker correction gets only the task and precise failure evidence; no visual worker is selected silently. Optional visual failures are retained and shown as failed station work while the product can continue.

## Offline acceptance and provider selection

`tests/unit/test_zana_visual.py` builds a small Field Notes product with a generated hero image and secondary section image. It injects deterministic PNG bytes and an independent offline review callback, then verifies local retention, package hashes, Teuta binding, Chromium image loading and Medaurus approval. The fake worker makes zero real visual-provider calls and incurs zero visual-provider spend. Its abstract fixture images establish contract behavior, not creative quality.

No owner-authorized real visual-provider adapter was found in the repository. Real visual execution remains blocked until the owner selects a provider and authorizes its adapter, budget and independent visual review policy. This milestone does not run the final website benchmark.
