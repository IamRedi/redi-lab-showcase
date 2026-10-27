# Runnable normal-app delivery

Normal-app Workbench builds are APP READY only after the six frozen gates pass,
their evidence is retained, and the packaged product passes HTTP startup,
workflow, and restart/persistence checks. Packaging has separate evidence;
it does not rewrite `live_run_evidence.json` or the gate workspace.

Each successful build delivers `build/workbench-builds/wb-<id>/app/`:

- Verified generated modules, copied byte-for-byte from `gates/` and checked
  against the retained artifact hashes.
- `run_app.py`, a standalone standard-library HTTP transport for the product's
  declared factory, renderer, state readers and POST routes.
- `app.json`, its runtime and acceptance descriptor; `package.json`, the
  delivery inventory bound to the original evidence.
- `README.md` and Windows `start.cmd`.
- `data/app.sqlite3`, initialized empty on first owner launch and reused across
  launches. Override with `--database FULL_PATH` if needed.

No Python dependencies beyond Python 3.11+ are required for Mini CRM. From the
app directory: `python -B run_app.py --open-browser`. The process prints its
loopback URL; `--port 8766` selects a fixed port, otherwise a free port is used.
Close a manual launch with Ctrl+C. `start.cmd` uses the Windows Python launcher.

Workbench's Result shows the absolute project path, exact PowerShell launch
command, current URL and OPEN APP / STOP APP. OPEN APP starts the final project
in a dedicated credential-free process and reuses an already-running matching
instance. STOP APP stops a process launched by that Workbench instance and
preserves its database. If Workbench itself was restarted, an existing product
can still be reopened; its original Workbench process owns the stop handle.
Changed or extra delivered files invalidate APP READY and prevent launch.

Run Workbench with `python -B -m redi_lab.workbench.server --port 8765`.
Saved results are available via the Saved build selector or `/?build=<id>`.
The old credentialed server is not reloaded by editing source; use a fresh
process for the new API. A separate port may be used without touching it.

To package a previously verified retained build without providers:

```text
python -B -m redi_lab.workbench.product_packaging build/workbench-builds/wb-<id>
```

This refuses missing/failed gates, changed source, existing unverified project
directories and failed prior delivery receipts. It never regenerates or
automatically retries a build. Failed delivery artifacts and logs remain for
diagnosis. An intentional repair can archive that failed delivery separately
before packaging again, leaving original gate evidence intact.

The packager and standalone host accept a product descriptor. A future normal
web product registers its verified files, module, factory/render entrypoints,
state readers, typed routes, and isolated acceptance workflow. Mini CRM's
descriptor supplies its current station contracts. Neither the generic host
nor packaging needs to substitute fake business logic. Its smoke-test records
live in a separate `packaging-smoke-*/smoke.sqlite3`, outside owner data.
