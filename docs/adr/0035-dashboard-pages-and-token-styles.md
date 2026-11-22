# ADR-0035: Dashboard pages by question and token-only visual styles

## Status

Accepted. Supersedes [ADR-0009](0009-orthogonal-visual-style-and-accent.md).

## Context

The dashboard put every workflow on one page and in side dialogs: capacity
matching, owner usage, and the recent-changes list each lived in a dialog or a
panel below the summary. An operator who wanted one answer ("which GPUs can I
use", "what is running", "what needs handling", "who used the cluster") had to
know which dialog held it. The six style families of ADR-0009 also changed
layout (column counts, rail position, panel order), so every component had to
be checked in six arrangements, and 141 style-specific rules had accumulated.
The faintest text tier measured about 2.7:1 against cards, below WCAG AA.

`app.js` sat exactly at its line ceiling, so new views could not be added to it.

## Driving factors

- One page per operator question, reachable by link and by keyboard.
- Numbers the browser derives must agree with the server's own aggregates.
- Every text tier passes WCAG 2.1 AA (4.5:1) on every style.
- No framework, build step, inline style, or new runtime dependency.
- `app.js` keeps transport, lifecycle, and routing; it does not grow.

## Candidates

### Option A: Keep one page and add more dialogs

Pros: no routing. Cons: the dialogs stack, the overview keeps every panel, and
the questions stay hidden behind buttons.

### Option B: Pages addressed by the URL hash, view leaves that build nodes

Pros: `#/find`, `#/jobs`, `#/alerts`, `#/usage`, and the overview are plain
links, so the browser history, bookmarks, and a command palette reach them.
The existing `#host` fragment keeps working. Page rendering moves into
dependency-free leaves that build nodes through `app.js`'s element factory and
are tested in Node against a small fake DOM, which pays down `app.js` instead
of raising its ceiling.

Cons: every render must skip hidden pages, and a page that reads a report
(`/api/brief`, `/api/reports/utilization`) needs its own refresh bound.

### Option C: A client-side framework with a router

Pros: routing and components out of the box. Cons: a build step and a runtime
dependency for a page that needs neither, contrary to the dependency rule.

## Decision

Choose Option B.

- Five pages: overview (`#/overview`, or `#<host>` for one host), find GPUs,
  jobs, alerts, and usage. Hidden pages render nothing; report-backed panels
  fetch at most once per minute while shown.
- Visual styles are token sets over one layout: carbon (default), paper,
  precision, glass, terminal, and ledger, plus "follow the system" between
  carbon and paper. A style changes color, type, corner radius, and material,
  never the arrangement. An accent re-tints emphasis only; "follow the style"
  is the default. Text colors are opaque per style so the repository test can
  measure them exactly.
- Display preferences move to `mocop.preferences.v2`. Migrating a version 1
  record keeps view choices and resets the visual style and accent, because
  version 1 stored its defaults explicitly.
- Page logic lives in leaves: pure projections (`fleet-state.js`,
  `gpu-jobs.js`, `use-check.js`, `alert-noise.js`, `self-check.js`,
  `command-palette.js`, `preferences.js`) and views that build nodes through
  the injected factory (`usage-view.js`, `jobs-view.js`, `alerts-view.js`,
  `topology-view.js`, `palette-view.js`, `trend-charts.js`,
  `inventory-forms.js`).

## Consequences

- The capacity and owners dialogs are gone; their element ids now sit in the
  find and usage pages, so existing selectors and the capacity watch keep
  working.
- A GPU is "compute" exactly when the server counts it in `stats.busyGpus`;
  below that, it is "occupied" only when a process holds at least 256 MiB.
- The settings self-check compares the browser's numbers with `stats` and
  `GET /api/capacity` and measures text contrast, so a drifting definition is
  visible to the operator, not only to the test suite.
- `app.js` lost about 230 lines net while gaining five pages.
