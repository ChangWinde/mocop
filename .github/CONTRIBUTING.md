# Contributing

Thank you for improving Mocop. Keep changes focused on fast, reliable, GPU-first cluster observability.

## Development setup

Python 3.10 or newer and OpenSSH are required. The runtime has no third-party Python dependencies.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mocop tests
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
node --check mocop/static/app.js
node --experimental-websocket tests/browser_smoke.mjs
```

Enable the repository-owned Git hooks once per clone:

```bash
git config core.hooksPath .githooks
```

## Change requirements

- Add focused regression coverage for behavior changes and negative coverage for security boundaries.
- Keep remote commands fixed and versioned; browser and configuration values must never become shell code.
- Keep `README.md` and `README.zh-CN.md` structurally and factually synchronized; English is canonical.
- Update `docs/ARCHITECTURE.md` when structural boundaries change; avoid per-feature design documents.
- Never commit a real `config/mocop.json`, SSH address, username, key, inventory name or telemetry export. Use `examples/mocop.example.json` with fictional aliases.
- Preserve the dependency-free runtime unless measurements and an ADR justify a new dependency.

## Commits

Each commit must represent one independently reviewable intent and leave the repository verifiable. Use the Forge subject format:

```text
[scope/op]: imperative summary
```

Scopes use lowercase path-like names such as `repo`, `core`, `config`, `probe`, `service`, `web`, `docs`, `security`, `ci`, or `tests`. The final segment must be one of `add`, `cleanup`, `document`, `fix`, `harden`, `promote`, `refactor`, `remove`, `test`, or `validate`.

```text
[repo/refactor]: simplify public project layout
[docs/readme/document]: explain cluster configuration
[tests/fix]: stabilize browser smoke test
```

The complete subject must be at most 72 characters, and the imperative summary must not end with a period. Use a body when the rationale or trade-offs are not evident from the diff. The local `commit-msg` hook checks new commits, and CI validates the complete reachable history. Git-generated merge and revert subjects are accepted only when their commit metadata proves their origin.

Do not mix generated output, unrelated formatting, or local configuration into a commit. Before committing, inspect staged paths and run the checks relevant to the change.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
