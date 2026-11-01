# Native Batch Download Design

## Goal

Prevent batch ZIP downloads from buffering the entire archive in the browser
renderer while preserving JWT-protected archive preparation and support for all
major browsers.

## Root Cause

The backend already builds the ZIP in a worker thread and streams it through a
`FileResponse`. The frontend defeats that streaming by awaiting `resp.blob()`,
which retains the complete archive in the page process before creating an object
URL. A 54,440,504-byte archive was created at 10:47:12 but its response never
completed before shutdown, leaving the temporary file and no completed access
log entry.

## API Flow

The existing authenticated binary `POST /api/assets/batch-download` remains for
API clients that consume response streams correctly.

The web UI uses two new endpoints:

1. `POST /api/assets/batch-download/prepare`
   - Requires the existing Bearer JWT.
   - Applies the existing 50-file, 1-GiB, permission, safe-name, and missing-file
     rules.
   - Builds the temporary ZIP off the event loop.
   - Creates a one-time download ticket and returns a fixed-origin URL.
2. `GET /api/assets/batch-download/file/{ticket_id}`
   - Does not require an Authorization header, because browser navigation cannot
     attach one.
   - Requires the matching secret in an HttpOnly cookie.
   - Atomically consumes the ticket and streams the ZIP with `FileResponse`.

The frontend calls prepare, creates a temporary anchor for the returned URL, and
lets the browser download manager consume the response. It never calls
`Response.blob()` or `URL.createObjectURL()` for batch ZIPs.

## Ticket Security

Each ticket has:

- a random public ID used in the URL;
- a separate 256-bit random secret stored in a dynamically named cookie;
- the archive path, requesting user ID, creation time, and expiry in the
  single-process in-memory registry.

The cookie is `HttpOnly`, `SameSite=Strict`, scoped to that ticket's exact GET
path, expires after five minutes, and uses `Secure` when the prepare request is
HTTPS. The public ID is safe to appear in access logs because it is unusable
without the cookie secret. Secret comparison is constant-time.

Invalid secrets do not consume tickets. A valid GET atomically removes the
ticket before serving so it cannot be replayed.

## Cleanup

- Successful response, send failure, or client disconnect removes the ZIP.
- An unused ticket owns a strongly referenced expiry task that removes the ZIP
  after five minutes.
- Application shutdown cancels expiry tasks and removes every pending ZIP.
- Application startup removes only stale files matching the application's
  `comfyui-assets-*.zip` prefix from the system temp directory. Recent files are
  left for active sibling processes; files older than the ticket lifetime plus a
  safety margin are removed.
- Cleanup is idempotent.

The original binary endpoint continues using the same response cleanup. Forced
process termination cannot execute `finally`, so startup stale-file cleanup is
the recovery path.

## Frontend Behavior

The download button remains busy while preparation runs. Preparation errors are
shown with the existing backend `detail` message. After the anchor click, the UI
returns to idle while the browser download manager handles transfer progress.
No ZIP bytes are retained by React.

The implementation removes the obsolete Blob-returning batch client contract;
other authenticated media downloads continue using Blob because they are
individual files and outside this change.

## Testing

Backend tests cover:

- ticket ID/secret separation and cookie flags;
- authorization and existing archive limits during preparation;
- one-time consumption, invalid secret, and expiry;
- successful, disconnected, expired, shutdown, and startup-stale cleanup;
- concurrent tickets from separate tabs;
- no secret in the returned URL.

Frontend tests cover:

- prepare errors remain visible;
- the returned URL is clicked through an anchor;
- no `blob()` or object URL call occurs;
- busy state resets after prepare success/failure;
- existing selection and batch limit behavior remain unchanged.

The regression test uses a large synthetic streamed response to prove browser
heap buffering is no longer part of the UI flow without allocating a large Blob.
