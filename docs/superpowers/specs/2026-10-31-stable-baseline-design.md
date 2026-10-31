# Stable Baseline Design

## Goal

Turn the current uncommitted P0 fixes and media changes into an installable,
fully verified baseline before adding product features.

## Scope

This work keeps the current single-process FastAPI, SQLite, React, and Provider
architecture. It does not add Celery, PostgreSQL, client-side routing, approval
workflows, JWT revocation, or polling redesign.

The baseline includes six change areas:

1. Reliable Provider retirement and shutdown.
2. Non-blocking IO and consistent sanitization of user-visible Provider errors.
3. Bounded batch downloads built through temporary ZIP files.
4. Size-safe asset forwarding with atomic form value and preview updates.
5. Persistent image thumbnails and complete derived-file cleanup.
6. Dependency, configuration, test, and documentation alignment.

## Provider Lifecycle

Replacing, disabling, deleting, or reconfiguring a Provider retires the old
instance. Retirement tasks remain strongly referenced until they finish. Every
`close()` call has a finite timeout; close failures are logged and never undo the
registry operation. Application shutdown runs in `finally` and waits for both
live Providers and outstanding retirement tasks.

## IO And Error Boundaries

Workflow reads, generated-file writes, thumbnail generation, archive creation,
and large filestore reads must not block the event loop.

Every Provider exception visible to a browser is sanitized through one helper.
The helper removes URLs, Authorization values, Bearer tokens, and common secret
parameters, then truncates the result. Full exceptions are retained only in
server logs. This applies to health checks, uploads, task execution, and resumed
tasks.

HTTP semantics remain stable:

- invalid input and size limits: 400;
- inaccessible or missing assets: 404;
- upstream Provider failures: 502;
- a member's `fresh=true` health request silently uses the cache;
- read authorization remains 404 and write authorization remains 403.

## Upload And Asset Forwarding

Direct uploads stop reading immediately after the applicable image, audio, or
video limit is crossed. Asset forwarding checks the FileStore object's current
size before loading bytes and checks `len(data)` again before calling the
Provider. Database `file_size` remains an early hint, not the authority.

The frontend updates a field only after the Provider upload succeeds. A failed
single-value replacement preserves the prior value and preview. A failed item in
a multi-value field adds neither a value nor a preview, so both arrays remain in
the same order.

## Batch Downloads

Batch download keeps the existing limits of 50 files and 1 GiB. It performs the
metadata precheck, then writes an archive to a temporary file in a worker thread.
Each FileStore object is copied in chunks through `ObjectHandle.iter_bytes()`;
the actual cumulative byte count is checked while writing. ZIP entry names are
basename-only and duplicate names receive a task prefix.

Success returns `FileResponse`. Completion, disconnect, limit errors, and other
exceptions all remove the temporary file. The frontend displays FastAPI's
`detail` instead of replacing it with a generic status-only message.

## Thumbnails And Derived Files

Images use lazy persistent thumbnails. The first authorized thumbnail request:

1. checks for the derived object;
2. deduplicates concurrent generation for the same storage key;
3. reads the source through FileStore in a worker thread;
4. applies EXIF orientation, converts to RGB, fits within 400 by 400, and writes
   an optimized JPEG through FileStore;
5. serves the derived object.

Video thumbnails continue to be created when outputs are saved. A missing video
thumbnail returns 404 without reading the video as an image. Audio has no
thumbnail. Thumbnail responses use the Asset ID as an ETag and require browser
revalidation so a retry that creates a new Asset cannot show stale content.

Asset deletion, task deletion, and task retry attempt to remove both the source
and `thumb_key(task_id, file_name)`. Database deletion remains the primary
operation: file cleanup failures are reported and logged but do not restore
database records.

## Authenticated Media

`fetchAssetBlob` accepts an `AbortSignal` and exposes HTTP status on failures.
`AuthMedia` aborts requests on unmount or source changes. A missing thumbnail may
fall back to the original exactly once; authentication failure reloads the login
flow without a second request; missing files and server/network failures render
different states.

## Verification

All behavior changes use red-green tests. Existing source-string guards that
break on harmless refactors are replaced by behavior checks or tolerant
structural assertions.

Full regression runs in a disposable linked worktree with its own SQLite DB,
storage, plugins, workflows, credentials, and frontend build. It runs all 32
`verify_*.py` scripts serially, Playwright UI checks, one real ComfyUI text-to-
image task, and a local small MP4 fixture for write/ffmpeg/responsiveness paths.
The restart-recovery E2E that terminates port 8000 and real video generation are
explicitly excluded.

## Delivery

Commits are split by responsibility:

1. Provider lifecycle.
2. Async IO, sanitization, and upload safety.
3. Temporary ZIP downloads and download UX.
4. Thumbnail, media, and asset-forwarding reliability.
5. Dependencies, regression infrastructure, and documentation.

Full-regression fixes use follow-up commits rather than rewriting reviewed
history.
