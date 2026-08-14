# ADR-0001: Repository layout

## Status

Superseded by [ADR-0019](0019-repository-and-documentation-governance.md)

## Context

The repository root mixed runtime code with architecture, governance, security, release, configuration, and deployment files. This made the first-level layout harder to scan and kept a static systemd example beside a tested unit generator that already owns service installation.

## Driving factors

- Keep the root limited to project entry points, package code, tests, and build metadata.
- Preserve direct source execution and the dependency-free development workflow.
- Put GitHub community health files where GitHub discovers them.
- Avoid duplicate deployment definitions while preserving standard build metadata.

## Candidates

### Option A: Consolidate support files and keep the flat package

Pros: removes six root entries, preserves imports and test commands, and follows GitHub community file conventions.

Cons: source-tree tests can still import the checkout directly instead of an installed package.

### Option B: Consolidate support files and adopt a `src/` layout

Pros: prevents accidental imports from the repository root and strengthens packaging isolation.

Cons: replaces `mocop/` with `src/` without reducing root entries, requires an installation step for normal test execution, and expands a documentation-focused refactor into runtime packaging work.

## Decision

Choose Option A. Move architecture and release material to `docs/`, community files to `.github/`, and the publication-safe configuration to `examples/`. Remove the static unit because `mocop service install` generates and tests the authoritative unit. Keep `MANIFEST.in` as standard build metadata so a source distribution contains both READMEs, project policy, engineering documentation, examples, and tests. Runtime package data remains declared explicitly in `pyproject.toml`.

## Impact

- Root navigation becomes smaller without changing Python import paths.
- Documentation links and the example-config test use their new paths.
- GitHub continues to discover contribution, conduct, and security policies.
- Source distributions retain the public project material; installed runtime assets remain limited by `pyproject.toml` package-data declarations.
