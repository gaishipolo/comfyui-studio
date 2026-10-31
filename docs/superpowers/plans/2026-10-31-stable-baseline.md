# Stable Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce an installable, resource-safe, fully verified baseline from the current P0 and media worktree changes.

**Architecture:** Preserve the single-process FastAPI and React architecture. Add explicit lifecycle tracking, worker-thread boundaries for blocking operations, persistent FileStore thumbnails, temporary-file ZIP creation, and isolated regression execution.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy async, SQLite, Pillow, FileStore local/MinIO, React 18, TypeScript, Vite, Playwright CLI.

## Global Constraints

- Preserve user isolation: read denial is 404, write denial is 403, and shared assets are read-only to teammates.
- Preserve database-first deletion: file cleanup failures never restore deleted records.
- Preserve batch limits exactly: 50 files and 1 GiB.
- Preserve upload limits exactly: image 20 MiB, audio 50 MiB, video 200 MiB.
- Keep the server single-process; do not add cross-process infrastructure.
- All file access goes through `app/filestore.py`; no direct `STORAGE_DIR` access in business code.
- Every production behavior change requires a test that is observed failing before implementation.
- Run service-writing tests only in a disposable worktree with isolated DB, storage, plugins, and workflows.
- Real generation covers text-to-image only; video coverage uses a local MP4 fixture.
- Do not run `e2e_restart_recovery.py` or terminate an existing port-8000 process.
- Do not modify or discard unrelated changes in the original `main` worktree.

---

### Task 1: Make Provider Retirement And Shutdown Reliable

**Files:**
- Modify: `backend/app/engines/base.py`
- Modify: `backend/app/engines/factory.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/verify_provider_lifecycle.py`

**Interfaces:**
- Produces: `_close_provider(provider: GenerationProvider) -> Awaitable[None]`
- Produces: `_retire_tasks: set[asyncio.Task[None]]`
- Preserves: synchronous `register_provider`, `unregister_provider`, `set_engine_config`, and `get_provider`
- Preserves: `close_all_providers() -> None`

- [ ] **Step 1: Add failing lifecycle tests**

Extend the in-process H1 section to assert that a retirement task remains in
`factory._retire_tasks` while `close()` is blocked, disappears after release,
and is awaited by `close_all_providers()`. Add a Provider whose `close()` never
returns and assert shutdown completes within the module's finite timeout.

- [ ] **Step 2: Run the lifecycle test and verify RED**

Run from `backend`:

```powershell
& "C:\Users\PC\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" tests\verify_provider_lifecycle.py
```

Expected: the new strong-reference or timeout assertion fails against the
current fire-and-forget implementation. HTTP-dependent sections may be skipped
through a focused in-process entry point added to the script.

- [ ] **Step 3: Implement bounded Provider closing**

Use one constant, `_CLOSE_TIMEOUT_SECONDS = 10.0`. Wrap every `provider.close()`
with `asyncio.wait_for`. Add each retirement task to `_retire_tasks`, remove it
with a done callback, and log failures. `close_all_providers()` concurrently
closes current instances and awaits a snapshot of retirement tasks.

Wrap the application lifespan body in `try/finally` so
`close_all_providers()` runs after normal shutdown and startup/runtime errors.

- [ ] **Step 4: Run focused lifecycle tests and verify GREEN**

Run the focused H1 tests twice to catch leaked global registry state. Expected:
all H1 assertions pass and no un-retrieved task exception is printed.

- [ ] **Step 5: Commit Task 1**

```powershell
git add backend/app/engines/base.py backend/app/engines/factory.py backend/app/main.py backend/tests/verify_provider_lifecycle.py
git commit -m "fix: close retired providers reliably"
```

---

### Task 2: Enforce Async IO And A Single Safe Error Boundary

**Files:**
- Modify: `backend/app/safe_detail.py`
- Modify: `backend/app/api/health.py`
- Modify: `backend/app/api/uploads.py`
- Modify: `backend/app/services/task_service.py`
- Create: `backend/tests/verify_error_boundary.py`
- Modify: `backend/tests/verify_queue_and_auth.py`
- Modify: `backend/tests/verify_filestore.py`
- Modify: `backend/tests/verify_from_asset_upload.py`

**Interfaces:**
- Produces: `safe_detail(exc: BaseException) -> str`
- Preserves: `upload_file`, `upload_from_asset`, and task status response shapes
- Preserves: `spawn_task(task_id: str, resume: bool = False) -> bool`

- [ ] **Step 1: Add failing sanitization and event-loop tests**

Add cases proving the browser-visible string contains none of:

```text
http://10.0.0.5/private?api_key=TOPSECRET
Authorization: Bearer abc.def.ghi
token=TOPSECRET
password=hunter2
```

Add a task failure case that checks `Task.error_msg` is sanitized while a
captured server log includes the original exception. Add behavior tests that
replace workflow reads, FileStore writes, and thumbnail work with a blocking
call while an asyncio ticker continues to advance.

- [ ] **Step 2: Run the focused tests and verify RED**

Expected: standalone Bearer/header/secret cases leak, task errors retain raw
text, and the old source guards fail after `asyncio.to_thread` refactoring.

- [ ] **Step 3: Implement the safe boundary and behavior-based guards**

Sanitize URLs first, then Authorization/Bearer values and case-insensitive
`api_key`, `apikey`, `token`, `secret`, and `password` assignments. Keep the
exception type and 200-character cap.

Log full task exceptions with task ID and Provider context, but store only
`safe_detail(exc)` in `Task.error_msg` for execute and resume paths. Catch and
log unexpected `_run_and_cleanup` failures so fire-and-forget exceptions are not
reported as un-retrieved.

Replace brittle exact-call source assertions with behavior checks or assertions
that accept an awaited `asyncio.to_thread` call.

- [ ] **Step 4: Run the affected offline suites and verify GREEN**

```powershell
& "C:\Users\PC\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" tests\verify_filestore.py
& "C:\Users\PC\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" tests\verify_queue_and_auth.py
```

Expected: both scripts exit 0 without an un-retrieved task warning.

- [ ] **Step 5: Commit Task 2**

```powershell
git add backend/app/safe_detail.py backend/app/api/health.py backend/app/api/uploads.py backend/app/services/task_service.py backend/tests/verify_error_boundary.py backend/tests/verify_queue_and_auth.py backend/tests/verify_filestore.py backend/tests/verify_from_asset_upload.py
git commit -m "fix: sanitize provider failures consistently"
```

---

### Task 3: Build Batch Archives In Temporary Files

**Files:**
- Modify: `backend/app/api/assets.py`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/components/AssetGallery.tsx`
- Create: `backend/tests/verify_batch_archive.py`

**Interfaces:**
- Produces: `_build_batch_archive(assets: Sequence[Asset]) -> Path`
- Produces: `_remove_temp_archive(path: Path) -> None`
- Preserves: `POST /api/assets/batch-download` request and ZIP response

- [ ] **Step 1: Add failing archive tests**

Cover these behaviors independently: 51 IDs fail before DB/file work; metadata
over 1 GiB fails before archive creation; streamed actual bytes crossing 1 GiB
remove the partial temp file; duplicate and traversal-like names become safe,
unique basenames; a successful response points to a temp file that is removed by
its background callback.

- [ ] **Step 2: Run archive tests and verify RED**

Expected: current code returns an in-memory `StreamingResponse` and cannot prove
temporary-file cleanup.

- [ ] **Step 3: Implement streamed temporary ZIP creation**

Create the ZIP in `asyncio.to_thread`. For each asset, call `open_asset`, copy
`handle.iter_bytes()` into `ZipFile.open(arcname, "w")`, close the handle in
`finally`, and increment the real-byte counter before writing each chunk. Remove
the temp path on every `BaseException` path. Return `FileResponse` with a
Starlette `BackgroundTask` cleanup callback.

Update `batchDownload` to parse FastAPI JSON `detail` on non-2xx responses.
Retain the existing AssetGallery busy state so duplicate clicks are disabled.

- [ ] **Step 4: Verify backend and frontend GREEN**

Run focused P0 archive tests, then:

```powershell
npm run build
```

Expected: archive tests pass and TypeScript/Vite exits 0.

- [ ] **Step 5: Commit Task 3**

```powershell
git add backend/app/api/assets.py frontend/src/api/client.ts frontend/src/components/AssetGallery.tsx backend/tests/verify_batch_archive.py
git commit -m "fix: stream batch downloads through temporary zip files"
```

---

### Task 4: Persist Thumbnails And Clean Derived Files

**Files:**
- Modify: `backend/app/thumbs.py`
- Modify: `backend/app/api/tasks.py`
- Modify: `backend/app/api/assets.py`
- Modify: `backend/app/services/task_service.py`
- Modify: `backend/requirements.txt`
- Create: `backend/tests/verify_media_reliability.py`

**Interfaces:**
- Produces: `make_image_thumb(data: bytes, max_edge: int = 400) -> bytes | None`
- Preserves: `make_video_thumb(data: bytes, suffix: str = ".mp4") -> bytes | None`
- Preserves: `thumb_key(task_id: str, file_name: str) -> str`
- Produces: one in-flight generation task per thumbnail storage key

- [ ] **Step 1: Write failing image and cleanup tests**

Create in-memory PNG fixtures for RGB, RGBA, and EXIF-rotated images. Assert the
result is decodable JPEG, has no edge over 400 pixels, and preserves orientation.
Assert invalid bytes return `None`.

Add API/service cases proving: a generated image thumbnail is saved under
`thumb_key`; two concurrent requests generate once; a missing video thumbnail
does not read the source video; asset deletion, task deletion, and retry each
attempt source and derived-key cleanup.

- [ ] **Step 2: Run the new script and verify RED**

Expected: current `Image.open(raw)` or `img.save(bytearray())` path returns
`None`, persistence assertions fail, and derived files survive deletion/retry.

- [ ] **Step 3: Implement persistent thumbnails**

Use `io.BytesIO`, `ImageOps.exif_transpose`, `Image.thumbnail`, alpha compositing
onto white, RGB conversion, and optimized JPEG output. Remove the in-memory LRU.

The thumbnail endpoint authenticates first, serves an existing derived object,
and for image MIME only creates one shared generation task that reads, converts,
and saves through FileStore in a worker thread. Video without a derived object
returns 404 immediately. Add ETag from `Asset.id` and `Cache-Control: private,
no-cache`.

Centralize best-effort derived cleanup and consume its result in all three
delete/retry paths. Add `Pillow>=10.0.0` to requirements in the same task.

- [ ] **Step 4: Run media tests and verify GREEN**

Run `verify_media_reliability.py`, `verify_filestore.py`, and
`verify_delete_assets_tasks.py` against isolated storage/service fixtures.
Expected: all exit 0 and no `thumb_*` fixture remains.

- [ ] **Step 5: Commit Task 4**

```powershell
git add backend/app/thumbs.py backend/app/api/tasks.py backend/app/api/assets.py backend/app/services/task_service.py backend/requirements.txt backend/tests/verify_media_reliability.py
git commit -m "fix: persist and clean media thumbnails"
```

---

### Task 5: Make Asset Forwarding And Authenticated Media Atomic

**Files:**
- Modify: `backend/app/api/uploads.py`
- Modify: `backend/tests/verify_from_asset_size_gate.py`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/components/AuthMedia.tsx`
- Modify: `frontend/src/components/DynamicForm.tsx`
- Modify: `docs/ui-test-checklist-2026-10-31.md`

**Interfaces:**
- Extends: `fetchAssetBlob(path, options?: { silent?: boolean; signal?: AbortSignal })`
- Produces: `AssetFetchError` with numeric `status`
- Preserves: `POST /api/upload/from-asset` response shape

- [ ] **Step 1: Add failing size and state-transition tests**

Extend the backend size test with an Asset whose DB size is small/zero while the
FileStore handle or returned bytes exceed the limit. Assert no Provider upload
occurs.

Add Playwright scenarios that start with an existing single value and preview,
force Provider upload failure, and assert both remain unchanged. For a multiple
field, assert the failed item appears in neither values nor previews. Assert
thumbnail 404 falls back once, raw 404 says the file is missing, server failure
says loading failed, and changing/unmounting media aborts the request.

- [ ] **Step 2: Verify RED**

Run the backend size script in an isolated service and the focused Playwright
scenarios. Expected: stale/zero metadata bypasses the backend guard, single-value
failure keeps a stale engine value with a cleared preview, and multi-value
failure can leave a ghost preview.

- [ ] **Step 3: Implement actual-size checks and atomic UI updates**

Use `open_asset(asset.file_path).size` before reading, close the handle, then
retain the post-read `len(data)` check. Direct and asset uploads only publish
new React value/preview state after `uploadInput` or `uploadFromAsset` succeeds.

Add AbortSignal support and a typed status error. `AuthMedia` falls back only on
thumbnail 404, never starts a second request after 401, aborts on cleanup, and
renders distinct missing/load-failed states. Update the checklist with these
exact scenarios.

- [ ] **Step 4: Verify GREEN**

Run the size-gate script, frontend build, and focused Playwright checks. Expected:
all pass with no browser console errors or lingering network requests.

- [ ] **Step 5: Commit Task 5**

```powershell
git add backend/app/api/uploads.py backend/tests/verify_from_asset_size_gate.py frontend/src/api/client.ts frontend/src/components/AuthMedia.tsx frontend/src/components/DynamicForm.tsx docs/ui-test-checklist-2026-10-31.md
git commit -m "fix: keep asset forwarding state consistent"
```

---

### Task 6: Align Clean Installation, Configuration, And Test Contracts

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/.env.example`
- Modify: `README.md`
- Modify: `docs/provider-dev-guide.md`
- Modify: `backend/tests/verify_p0_fixes.py`
- Modify: affected `backend/tests/verify_*.py` scripts
- Modify: `docs/code-review-2026-10-30.md`

**Interfaces:**
- Documents: Provider `close()` lifecycle contract
- Documents: isolated versus service versus real-Comfy test prerequisites

- [ ] **Step 1: Add a clean-install import check**

In a disposable virtual environment install only `backend/requirements.txt`,
then run:

```powershell
python -c "import app.main, app.auth, app.thumbs, app.filestore"
```

Expected RED before dependency edits: one or more of `jose`, `passlib`, bcrypt,
Pillow, or MinIO is unavailable in a genuinely clean environment.

- [ ] **Step 2: Complete dependency and configuration declarations**

Add these compatible floors/constraint:

```text
python-jose>=3.5.0
passlib>=1.7.4
bcrypt>=3.2,<4
Pillow>=10.0.0
minio>=7.2.0
```

Synchronize `.env.example` with every setting in `app/config.py`, remove the
unsupported PostgreSQL claim, and state that relative paths require launching
from `backend`.

- [ ] **Step 3: Repair documentation and test contracts**

Remove the nonexistent `FieldSpec.advanced` documentation, document Provider
`close()`, correct verify-script counts and prerequisites, and distinguish
historical results from fresh evidence. Replace remaining brittle exact-source
guards and make scripts aggregate failures into a nonzero exit code with
`finally` cleanup where this baseline touches them.

- [ ] **Step 4: Verify clean installation and docs-related suites GREEN**

Recreate the disposable virtual environment from scratch, install requirements,
run the import command, then run schema, Provider-loader, and configuration
verification scripts. Expected: every command exits 0.

- [ ] **Step 5: Commit Task 6**

```powershell
git add backend/requirements.txt backend/.env.example README.md docs/provider-dev-guide.md docs/code-review-2026-10-30.md backend/tests
git commit -m "docs: align setup and regression contracts"
```

---

### Task 7: Run Isolated Full Regression And Record Evidence

**Files:**
- Modify only if evidence is inaccurate: `docs/ui-test-checklist-2026-10-31.md`
- Modify only if evidence is inaccurate: `docs/code-review-2026-10-30.md`

**Interfaces:**
- Uses: all prior task behavior
- Produces: a clean, reviewable branch with current verification evidence

- [ ] **Step 1: Create the disposable verification worktree**

Use `superpowers:using-git-worktrees` at execution time. Configure a unique
SQLite DB, local storage, fixed regression secret, `admin123`, copied workflows,
test-only plugin configuration, and a non-production port. Do not copy `.env`,
real DB, storage, or API keys.

- [ ] **Step 2: Run static and build gates**

Run `git diff --check`, Python import/compile checks, `npm ci`, and
`npm run build`. Expected: all commands exit 0 with no errors.

- [ ] **Step 3: Run all 32 verify scripts serially**

Run pure/offline scripts first, then temporary-resource scripts, then scripts
requiring the isolated FastAPI service. Capture each command, exit code, and
PASS/FAIL count. Do not run tests against the original DB or storage.

- [ ] **Step 4: Run UI verification**

Use Playwright CLI in one open-interact-close task. Cover admin/member access,
upload limits, 51-file archive rejection and normal ZIP, thumbnail generation,
missing versus failed media, atomic upload rollback, task notification, and the
FastAPI-served production build.

- [ ] **Step 5: Run media integration evidence**

Submit one real ComfyUI text-to-image task using
`backend/workflows/image_flux2_klein_text_to_image.json` with mappings
`prompt -> 76.inputs.value`, `width -> 103.inputs.value`,
`height -> 106.inputs.value`, and output node `108`. Include a UUID in the
prompt, verify task/asset/thumb/download/delete, and clean the task afterward.

Use a tiny local MP4 fixture to run generated-file save, ffmpeg thumbnail,
thumbnail fetch, delete cleanup, and concurrent health/task requests. Do not
submit a real video task.

- [ ] **Step 6: Audit cleanup and record actual evidence**

Verify the isolated DB has no test users/templates/tasks/assets, storage has no
source or `thumb_*` leftovers, plugins/workflows match their initial snapshots,
and ComfyUI's queue is empty. Update documents only with results actually seen
in this run and list the two explicit exclusions.

- [ ] **Step 7: Commit evidence changes if needed**

```powershell
git add docs/code-review-2026-10-30.md docs/ui-test-checklist-2026-10-31.md
git commit -m "docs: record stable baseline verification"
```

- [ ] **Step 8: Request final code review**

Review the complete branch against this plan, fix all Critical/Important
findings in one follow-up wave, rerun affected tests, and perform one scoped
re-review before branch completion.
