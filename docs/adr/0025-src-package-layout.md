# ADR-0025: src/ package layout

## Status

Accepted

## Context

ADR-0001 and ADR-0019 kept the `mocop` package at the repository root so tests
and tools could import the checkout without an installation step. That layout
has a known cost: any process whose working directory is the repository root
imports the checkout instead of the installed release, packaging bugs such as a
missing `package-data` declaration stay invisible during development, and the
root mixes the runtime package with governance directories.

The dependency-free direct execution property that motivated the root layout
does not actually require a root package. Test discovery already imports the
`tests` package first, and that package can extend `sys.path` deterministically.

## Driving factors

- Keep unittest discovery working from a clean checkout without an
  installation step or an environment variable.
- Make an installed release the only importable `mocop` outside the test suite,
  so packaging mistakes fail in CI instead of on an operator's machine.
- Reduce top-level entries that are neither ecosystem entry points nor
  governance directories.
- Preserve public documentation URLs and the GitHub community file locations
  governed by ADR-0019.

## Candidates

### Option A: Keep the root package

Pros: no migration and no path changes anywhere.

Cons: retains checkout shadowing, hides package-data mistakes, and keeps the
runtime package competing with governance directories at the root.

### Option B: Move the package to `src/` and bootstrap `sys.path` in `tests/`

Pros: standard packaging isolation; the checkout can no longer shadow an
installed release; direct test execution keeps working because importing the
`tests` package prepends `src` exactly once.

Cons: discovery must name the repository root as the top-level directory
(`-t .`) so the `tests` package is imported; file paths in CI, contributor
commands, and governance tests change once; `python3 -m mocop` from the
repository root now requires an installed package (the documented workflows
already install one).

### Option C: Move the package and require editable installation for tests

Pros: closest to how installed releases run.

Cons: breaks the documented dependency-free workflow and adds a setup step to
every quick check.

## Decision

Choose Option B. `git mv mocop src/mocop` preserves history. `pyproject.toml`
declares `package-dir = {"" = "src"}` with unchanged package names and package
data. `tests/__init__.py` inserts the absolute `src` path at the front of
`sys.path` when the test package is imported. Discovery names the repository
root as the top-level directory (`python3 -m unittest discover -s tests -t .`),
which imports the `tests` package first; the browser fixture
(`python3 -m tests.browser_fixture`) and coverage runs get the same bootstrap,
so a checkout still runs without installation.

Repository governance now pins `src` as the tracked root entry and prefixes the
core module line budgets with `src/`. CI, contributor, and documentation
commands reference `src/mocop` paths. `MANIFEST.in` and wheel contents are
unchanged because package discovery follows `package-dir`.

## Impact

- The tracked root drops the runtime package directory in favor of `src`.
- An accidental `import mocop` from the repository root fails instead of
  silently using unpackaged code.
- Contributor commands change once: `compileall`, `node --check`, and
  `coverage --source` gain the `src/` prefix, and unittest discovery adds
  `-t .`.
- ADR-0019's documentation governance is unaffected; its root-layout choice of
  a root-level package is superseded by this decision.
