# Working in this repository as an agent

Mocop is built to be operated and maintained by AI agents. Agents and people
follow the same contracts; this page is the entry point to them, not a second
copy. Every rule below is enforced by a test, a hook, or CI, so reading it
first saves a failed run.

## Read first

- [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md): the canonical
  quality-gate command list, the commit-subject policy, and the writing
  standard for documentation and interface text.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): the module map, the browser
  leaf inventory, and the line-budget ratchet.
- [`docs/README.md`](docs/README.md): which document owns each fact and which
  change requires which documentation update.

## Before changing code

1. Enable the repository hooks once per clone:
   `git config core.hooksPath .githooks`.
2. Find the owner of every contract you touch in the documentation portal.
   Repository tests compare documentation with code and fail on drift:
   `docs/API.md` against `src/mocop/api_manifest.py`, `docs/CONFIGURATION.md`
   against the configuration schema, the README size, the tracked root
   layout, and the per-module line budgets in
   `tests/test_repository_governance.py`.
3. A route, query parameter, body field, or error code changes in
   `src/mocop/api_manifest.py` first. The handlers validate through that
   manifest and `GET /api/meta` publishes it, so the reference, the server,
   and the self-description cannot disagree.
4. Line budgets are a ratchet. When a change would cross one, extract a
   coherent leaf (Python module or `src/mocop/static/*.js` leaf with a Node
   test) and lower the ceiling instead of raising it.

## Before committing

Run the gate list from `CONTRIBUTING.md`: `unittest`, branch coverage at or
above 85 %, `ruff check` and `ruff format --check`, `node --check` on every
leaf, every `tests/*_test.mjs`, and the browser smoke journey. Commit
subjects are `[scope/op]: imperative summary`, at most 72 characters, with
`op` from the list in `CONTRIBUTING.md`; the `commit-msg` hook and CI both
enforce it. Record every user-visible change under `[Unreleased]` in
`docs/CHANGELOG.md`.

## Boundaries that must hold

- The runtime uses only the Python standard library and the system OpenSSH
  client; a new dependency needs measurements and an ADR.
- Remote commands are fixed and versioned in `src/mocop/remote_script.py`; no
  configuration or browser value ever becomes shell text.
- Automation never sends `X-Monitor-Request: dashboard`; it marks a live
  viewer and changes the collection cadence. Use `mocop api` or an A-tier
  `curl` with the Bearer capability instead.
- Never commit a real SSH alias, address, capability, or telemetry export;
  `examples/mocop.example.json` uses fictional names.

## Operating a deployment

- `GET /api/meta` (public) describes every route, tier, query and body
  schema, error code, and the `servers[].message` vocabulary; a `403` tells
  you where the capability lives.
- Webhook receivers implement the contract under
  [Webhook deliveries](docs/API.md#webhook-deliveries): signed JSON per
  actionable incident transition, `eventId` as the idempotency key.
- On the monitor host, `mocop api PATH` performs any public or authenticated
  GET with the listener and capability taken from the configuration.
- `mocop config check --json`, `mocop doctor --json`, and every lifecycle
  command with `--json` write one `{ok, ...}` document; exit codes are
  documented in [`docs/OPERATIONS.md`](docs/OPERATIONS.md#command-reference-and-exit-codes).
