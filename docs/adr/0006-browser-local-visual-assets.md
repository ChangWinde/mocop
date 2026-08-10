# ADR-0006: Browser-local visual assets

## Status

Accepted

## Context

Mocop's built-in color themes do not provide enough visual variety for operators who keep the dashboard open throughout the day. Operators also want to use a personal image as the dashboard background. Presentation remains a per-browser concern: an image selected by one viewer must not become service data, a cluster-wide setting, or a file-serving endpoint.

## Driving factors

- Offer meaningfully different visual systems rather than palette-only variants.
- Keep personal images on the device that selected them.
- Bound browser decode and storage cost for untrusted local files.
- Preserve the dependency-free, same-origin dashboard and its restrictive CSP.
- Degrade safely when browser storage is unavailable.

## Candidates

### Option A: Upload visual assets to the Mocop service

Pros: assets follow the service and can be shared by every viewer.

Cons: adds multipart parsing, storage lifecycle, authorization and content-serving surfaces to a telemetry service; leaks personal imagery to other viewers; and makes the operator responsible for disk quotas and cleanup.

### Option B: Store a data URL in `localStorage`

Pros: simple synchronous implementation using the existing preference record.

Cons: base64 expands the asset, blocks the main thread during serialization, quickly exceeds small synchronous storage quotas, and couples structured preferences to a large opaque payload.

### Option C: Store a validated raster `Blob` in browser IndexedDB

Pros: keeps the file browser-local, avoids base64 expansion, separates binary and structured preferences, and provides asynchronous storage. The service needs no upload or file route.

Cons: IndexedDB can be unavailable or quota-constrained, so the UI needs an explicit session-only fallback.

## Decision

Choose Option C. Accept only PNG, JPEG, WebP and AVIF files up to 8 MiB. Verify the declared type against its container signature, decode the selected file before persistence, and reject images wider or taller than 8,192 pixels or larger than 32 megapixels. SVG and animated formats are intentionally excluded. Store the validated `Blob` under one fixed key in a dedicated IndexedDB object store; create only a browser-owned object URL for rendering, and revoke replaced URLs.

Theme and background-visibility values stay in the versioned, allowlisted `localStorage` record. The image never crosses an HTTP boundary. The CSP permits `blob:` only for images. If IndexedDB persistence fails, Mocop may render the validated image for the current session while clearly reporting that it was not saved.

Built-in themes may change geometry, surface opacity, blur, shadow, typography and background texture in addition to color. These differences remain CSS-only and never alter telemetry semantics, accessibility labels or data density preferences.

## Impact

- Personal images do not enter Mocop configuration, logs, telemetry, backups or server storage.
- Decode work and retained browser storage have explicit byte, dimension and pixel bounds.
- Resetting structured display preferences does not silently delete the separately managed image; removal remains an explicit action.
- A browser without IndexedDB still renders telemetry and can use built-in themes.
- `img-src` adds `blob:` while scripts, connections, objects, frames and base URLs retain their existing restrictions.
