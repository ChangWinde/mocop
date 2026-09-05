# ADR-0019: Repository and documentation governance

## Status

Accepted

> **Update:** the root-level `mocop/` package location decided here was moved to
> `src/mocop/` by [ADR-0025](0025-src-package-layout.md); the documentation
> governance, root allowlist, and `MANIFEST.in` decisions are unchanged. The
> root allowlist later gained `AGENTS.md`, the ecosystem entry point AI coding
> agents read by convention; it points at the governed documents instead of
> duplicating them, and the portal lists it as one audience row.

## Context

ADR-0001 removed obsolete root-level support files, but the repository still
needs an explicit answer to two different concerns: what belongs at the project
root, and how readers discover and maintain its growing documentation set.
Generated build output, local agent configuration, and an untracked dependency
solver lock also make a working checkout look less organized than the tracked
tree. Meanwhile, moving stable documents only for visual symmetry would break
published links and enlarge the migration surface.

## Driving factors

- Keep the root limited to ecosystem entry points, source, tests, examples, and
  governance directories.
- Preserve direct source execution, standard Python packaging, GitHub discovery,
  and existing public documentation URLs.
- Give operators, automation authors, contributors, and maintainers one indexed
  documentation entry point with explicit ownership and update triggers.
- Keep English canonical while maintaining a clearly located Simplified Chinese
  onboarding document.
- Prevent local tools, caches, and generated dependency state from becoming
  accidental release artifacts.
- Enforce the documentation graph and ADR inventory with executable tests.

## Candidates

### Option A: Move runtime code to `src/` and split every document by category

Pros: creates a conventional installed-package boundary and a visually deep
documentation hierarchy.

Cons: does not reduce the number of top-level directories, requires installation
for normal source tests, changes all published documentation URLs, and mixes a
packaging migration with information architecture work.

### Option B: Preserve ecosystem roots and add governed navigation

Pros: keeps supported imports, commands, packaging metadata, and public document
URLs stable; removes the localized README from the root; adds a documentation
portal, ADR index, translation policy, update triggers, and drift tests; and
classifies local-only artifacts without moving user state.

Cons: the canonical reference documents remain directly under `docs/` rather
than being grouped into deeper physical categories.

### Option C: Delete `MANIFEST.in` and rely only on wheel package data

Pros: removes one more root file.

Cons: source distributions would no longer intentionally carry project policy,
engineering documentation, examples, and tests. Installing those documents as
runtime data would solve a different problem and pollute the installed package.

## Decision

Choose Option B. The root keeps `README.md`, `LICENSE`, `pyproject.toml`,
`MANIFEST.in`, `.gitignore`, and `.gitattributes` as standard entry/build files.
The Simplified Chinese README moves to `docs/locales/zh-CN/README.md`. Stable
canonical references remain at their existing `docs/*.md` URLs and are indexed
by `docs/README.md`; ADRs are indexed separately by `docs/adr/README.md`.

The documentation portal owns the audience map, canonical-document registry,
language policy, update triggers, link policy, and quality gates. Tests require
every canonical document and ADR to be indexed and every local Markdown/HTML
link to resolve. English remains the source of truth; behavior changes update
the Chinese onboarding document in the same change when its covered sections
are affected.

Root-local `.claude/`, `.mcp.json`, and `uv.lock` are ignored. Mocop has no
runtime dependencies, CI pins its development tools, and the build backend is
pinned in `pyproject.toml`; introduce a committed dependency lock only through a
future dependency-policy decision. Existing user files are not moved or deleted.

## Impact

- The tracked root loses one language-specific entry while retaining every
  standard project/build entry point.
- Published API, operations, security, performance, configuration, architecture,
  and changelog URLs remain stable.
- Source distributions still include both languages and all governed documents
  through the recursive `docs` manifest rule.
- Contributors can find a document by audience and know which behavior change
  requires it to be updated.
- Documentation and ADR index drift become test failures rather than review-only
  conventions.
