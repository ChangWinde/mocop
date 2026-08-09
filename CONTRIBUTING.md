# Contributing

Thank you for improving mocop. Keep changes focused on fast, reliable, GPU-first cluster observability.

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

## Change requirements

- Add focused regression coverage for behavior changes and negative coverage for security boundaries.
- Keep remote commands fixed and versioned; browser and configuration values must never become shell code.
- Update `ARCHITECTURE.md` when structural boundaries change; avoid per-feature design documents.
- Never commit a real `config/mocop.json`, SSH address, username, key, inventory name or telemetry export. Use `config/mocop.example.json` with fictional aliases.
- Preserve the dependency-free runtime unless measurements and an ADR justify a new dependency.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
