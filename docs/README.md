# Mocop documentation

Start with the document that matches the task. `README.md` at the repository
root is the installation and product entry point; the documents below own the
detailed contracts and operating procedures.

## Find a document

| Reader / task | Canonical document | What it owns |
|---|---|---|
| New operator | [Project README](../README.md) | Requirements, first installation, first dashboard, and essential safety boundaries |
| Chinese-speaking operator | [简体中文入门](locales/zh-CN/README.md) | Maintained Simplified Chinese onboarding and daily workflow |
| Example-driven operator | [Complete safe configuration](../examples/mocop.example.json) | Publication-safe example containing every supported configuration section |
| Configuration author | [Configuration reference](CONFIGURATION.md) | Every JSON field, default, type, relationship, and hard limit |
| Service operator | [Operations runbook](OPERATIONS.md) | Health checks, backup, upgrade, rollback, capability rotation, and uninstall retention |
| API or automation author | [HTTP API](API.md) | Versions, authentication, access tiers, schemas, errors, retry rules, and examples |
| Security reviewer | [Security model](SECURITY.md) | Assets, actors, trust boundaries, abuse cases, enforcement, and deployment requirements |
| Maintainer | [Architecture](ARCHITECTURE.md) | Components, dependency boundaries, canonical formats, state lifecycle, and repository layout |
| Performance engineer | [Performance](PERFORMANCE.md) | Hot paths, resource ceilings, benchmarks, and re-evaluation thresholds |
| Release reviewer | [Quality and resource assessment](QUALITY.md) | Current performance, robustness, stability, resource evidence, and residual boundaries |
| Release reader | [Changelog](CHANGELOG.md) | User-visible additions, changes, fixes, removals, and security notes |
| Release maintainer | [Release procedure](../.github/RELEASING.md) | Version alignment, immutable tags, artifacts, and post-release verification |
| Decision reviewer | [Architecture decision index](adr/README.md) | Accepted, superseded, and proposed structural decisions |
| Contributor | [Contributing guide](../.github/CONTRIBUTING.md) | Development gates, writing rules, commit policy, and change requirements |
| Community member | [Code of conduct](../.github/CODE_OF_CONDUCT.md) | Participation and enforcement expectations |
| Vulnerability reporter | [Security policy](../.github/SECURITY.md) | Supported versions and private reporting process |

## Canonical ownership

Avoid copying a detailed contract into several documents. Link to its owner:

- `README.md` answers “what is Mocop, can I run it, and how do I get the first
  healthy dashboard?” It stays task-oriented and does not duplicate every field
  or API schema.
- `CONFIGURATION.md` owns configuration truth. The publication-safe JSON under
  `examples/` demonstrates it but does not redefine bounds.
- `OPERATIONS.md` owns stateful procedures and rollback. Commands that can change
  service or durable state link there.
- `API.md` owns HTTP compatibility. Route, access-tier, stable-code, and schema
  changes update it in the same commit.
- `SECURITY.md`, `ARCHITECTURE.md`, and `PERFORMANCE.md` own their respective
  cross-cutting contracts. `QUALITY.md` summarizes current evidence without
  redefining those contracts. A material boundary decision also gets an ADR.
- `CHANGELOG.md` records user-visible behavior; it is not a design explanation.

## Update triggers

| Change | Required documentation |
|---|---|
| Installation, first-run, or primary UI workflow | Root README and affected localized onboarding |
| JSON key/default/range/relationship | Configuration reference, example, and schema-drift test |
| Route, field, access tier, error, or retry behavior | API reference and API contract tests |
| State path, backup, migration, service, token, or uninstall behavior | Operations runbook |
| Trust boundary, secret, permission, input, or deployment assumption | Security model and security policy when reporting changes |
| Component boundary, canonical format, or consequential alternative | Architecture document and a new/superseding ADR |
| Resource limit, hot path, benchmark, or performance claim | Performance document with a reproducible command |
| Any user-visible addition, change, fix, removal, or security correction | Unreleased changelog |
| Version or release artifact | Changelog, both onboarding READMEs, and release procedure |

## Language policy

English is canonical for engineering and API contracts. The Simplified Chinese
README is a maintained onboarding document, not an independent specification.
When an English change affects a section present in the Chinese document, update
both in the same change. Chinese links point back to the canonical English
references for exhaustive field and protocol details.

## Decision lifecycle

Architecture decisions live under `docs/adr/` and use one of `Proposed`,
`Accepted`, `Deprecated`, or `Superseded by ADR-NNNN`. Never rewrite a historical
decision to make a later choice look inevitable; add a superseding ADR and link
both directions. The [ADR index](adr/README.md) must list every numbered record.

## Path and link policy

Canonical reference paths are public URLs. Do not move them only for visual
symmetry. A move needs an explicit compatibility plan, updated inbound links,
and link tests. Use repository-relative links, fenced commands that can be copied
without private values, and fictional hosts from the documentation address range.

The tracked repository root intentionally keeps only standard project entry and
build files plus the source, tests, examples, documentation, GitHub policy, and
hook directories. Local virtual environments, caches, build products, agent
configuration, private runtime config, and solver state are ignored and are not
project structure.

## Quality gates

Run these before merging a documentation or structure change:

```bash
python3 -m unittest tests.test_docs -v
python3 -m unittest discover -s tests -t . -v
uvx --from coverage==7.15.4 coverage run --branch --source=src/mocop -m unittest discover -s tests -t . -p 'test_*.py' -q
uvx --from coverage==7.15.4 coverage report --fail-under=85
python3 -m compileall -q src/mocop tests
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
node --check src/mocop/static/app.js
node --check src/mocop/static/capacity-match.js
node --check src/mocop/static/capacity-watch.js
node --check src/mocop/static/csv-export.js
node --check src/mocop/static/dashboard-auth.js
node --check src/mocop/static/format.js
node --check src/mocop/static/process-search.js
node tests/capacity_match_test.mjs
node tests/capacity_watch_test.mjs
node tests/csv_export_test.mjs
node tests/dashboard_auth_test.mjs
node tests/process_search_test.mjs
node --experimental-websocket tests/browser_smoke.mjs
```

`tests.test_docs` verifies local links, the canonical-document portal, the ADR
inventory, live API routes/access/errors, configuration fields, authentication,
and collection protocol references. Passing it proves only those assertions;
examples and user journeys still require their owning tests.
