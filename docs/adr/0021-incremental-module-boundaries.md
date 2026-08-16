# ADR-0021: Incremental module boundaries and growth budgets

## Status

Accepted

## Context

Mocop deliberately ships without a browser build chain and keeps the collector,
state coordinator, and HTTP adapter in directly runnable Python modules. That
simplicity helped the project stay dependency-free, but `app.js`, `service.py`,
and `web.py` accumulated enough responsibilities that continued unchecked growth
would make reviews and regression analysis progressively harder.

The current behavior is well covered and concurrency-sensitive. A wholesale file
move would create a large equivalence burden without changing user behavior, so
the maintenance boundary must improve without weakening the zero-build deployment
model or the state store's locking invariants.

## Driving factors

- Preserve the dependency-free Python runtime and directly served static assets.
- Keep the authenticated snapshot as the canonical browser data boundary.
- Extract pure leaf behavior before moving orchestration or mutable state.
- Prevent core modules from growing while incremental extraction proceeds.
- Test extracted algorithms independently and through the existing browser path.
- Keep package installation, CSP, caching, and Python 3.10 compatibility intact.

## Candidates

### Option A: Split every large module immediately

Pros: produces smaller files in one change and makes the target layout visible
at once.

Cons: moves locking, scheduling, HTTP dispatch, DOM state, and event wiring at the
same time; creates a large semantic-equivalence surface; and makes regressions hard
to attribute despite the unchanged product behavior.

### Option B: Extract dependency-free leaves and enforce declining budgets

Pros: creates independently testable boundaries one behavior at a time, preserves
the current deployment model, and turns further growth into an executable failure.
Pure algorithms can move without copying mutable application state.

Cons: orchestration modules remain larger than ideal during the transition, and
maintainers must lower budgets after each extraction rather than treating the
first ceiling as a permanent allowance.

### Option C: Introduce a JavaScript bundler and a general Python plugin registry

Pros: offers conventional frontend imports and broad extension points.

Cons: adds a build artifact and dependency lifecycle, complicates CSP/source
debugging, and reintroduces registry indirection where Mocop currently has one
collector and one HTTP implementation. No present second implementation justifies
those extension points.

## Decision

Choose Option B. Browser leaf modules use a single frozen namespace on
`globalThis`, load before the classic application controller, and expose a small
factory whose dependencies and bounds are explicit. This keeps existing global
debug/test access and requires no compilation. The first extraction is the process
search projection and bounded Top-N ranking in `process-search.js`.

Python orchestration stays in its current modules until a concrete leaf boundary
can move without splitting one lock-owned invariant across files. Repository tests
enforce line ceilings for the existing large modules. A change that exceeds a
ceiling must extract a coherent leaf or add a superseding ADR with measured reasons;
it must not simply raise the number. Every successful extraction lowers the
corresponding ceiling in the same commit.

The snapshot JSON remains the only browser search input, and search results remain
plain references to immutable snapshot objects. No extra API, remote query, retained
snapshot copy, or third-party package is introduced.

## Impact

- Process-search normalization, matching, ranking, and result bounding have an
  independent Node contract test and remain covered by the authenticated browser
  journey.
- `app.js` becomes smaller while its DOM/event orchestration remains stable.
- Core-file, README, release-metadata, and tracked-root budgets become executable
  repository governance rather than reviewer memory.
- Future module extraction is incremental, reviewable, and required before the
  current concentration can grow.
