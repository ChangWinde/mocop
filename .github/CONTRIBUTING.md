# Contributing

Thank you for improving Mocop. Keep changes focused on fast, reliable, GPU-first cluster observability.

## Development setup

Python 3.10 or newer, Node.js, a Chrome or Chromium binary for the browser smoke
test, and OpenSSH are required. The runtime has no third-party Python
dependencies.

This is the canonical quality-gate list; `.github/workflows/ci.yml` runs the
same commands and the READMEs and documentation portal link here instead of
repeating it.

```bash
python3 -m unittest discover -s tests -t . -v
uvx --from coverage==7.15.4 coverage run --branch --source=src/mocop -m unittest discover -s tests -t . -p 'test_*.py' -q
uvx --from coverage==7.15.4 coverage report --fail-under=85
python3 -m compileall -q src/mocop tests
uvx --from ruff==0.12.11 ruff check .
uvx --from ruff==0.12.11 ruff format --check .
for script in src/mocop/static/*.js; do node --check "$script"; done
for test in tests/*_test.mjs; do node "$test"; done
node --experimental-websocket tests/browser_smoke.mjs
```

Enable the repository-owned Git hooks once per clone:

```bash
git config core.hooksPath .githooks
```

## Change requirements

- Add focused regression coverage for behavior changes and negative coverage for security boundaries.
- Keep remote commands fixed and versioned; browser and configuration values must never become shell code.
- Keep `README.md` and `docs/locales/zh-CN/README.md` factually synchronized for the onboarding sections they share; English is canonical.
- Use the [documentation portal](../docs/README.md) to identify the canonical owner and update trigger for each contract.
- Update `docs/ARCHITECTURE.md` when structural boundaries change; record consequential alternatives in an ADR and add it to `docs/adr/README.md`.
- Never commit a real `config/mocop.json`, SSH address, username, key, inventory name or telemetry export. Use `examples/mocop.example.json` with fictional aliases.
- Preserve the dependency-free runtime unless measurements and an ADR justify a new dependency.

## Writing standard

Documentation and interface text must tell the reader what the feature does, what action to take, and what happens next.

- Put the conclusion or required action first. Use one main claim per sentence.
- Name the concrete component, state change, storage location, limit, and failure behavior when they matter.
- Separate current behavior from recommendations and known limitations.
- Define a necessary technical term at first use; remove terminology that does not help the reader act.
- Remove promotional adjectives and abstract claims such as “smart,” “seamless,” or “more robust” unless a measured behavior immediately explains them.
- Keep essential instructions in the README and link implementation detail to the focused architecture, security, or performance document.
- Write Chinese copy naturally rather than translating English sentence structure word for word.

Prefer “Mocop retries a failed host with a delay of up to 60 seconds” over “Mocop provides an intelligent and robust retry experience.” A reader should not need to infer the subject, action, result, or boundary.

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

The complete subject must be at most 72 characters, and the imperative summary must not end with a period. Use a body when the rationale or trade-offs are not evident from the diff. The local `commit-msg` hook checks new commits, and CI validates the complete reachable history. Git-generated merge and revert subjects are accepted only when their commit metadata proves their origin. A commit that reached the protected branch before its rule was enforced is listed by full SHA with a reason in `HISTORICAL_EXEMPTIONS` inside `.github/scripts/check_commit_message.py`; a repository test pins that list, so adding to it is a reviewed change, never a silent one.

Do not mix generated output, unrelated formatting, or local configuration into a commit. Before committing, inspect staged paths and run the checks relevant to the change.

Maintainers cut immutable releases only from a verified default-branch commit;
follow [RELEASING.md](RELEASING.md) for the version, Changelog, annotated-tag,
artifact, and post-release verification contract.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
