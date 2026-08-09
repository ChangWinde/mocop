# Security policy

## Supported versions

Security fixes are applied to the latest released minor version. Older versions should be upgraded before a report is evaluated.

## Reporting a vulnerability

Use the published repository's private vulnerability-reporting feature. If that feature is unavailable, open a minimal public issue asking a maintainer to establish a private contact; do not include exploit details, credentials, hostnames, addresses or telemetry in that issue.

Include the affected version, attacker prerequisites, a minimal reproduction, impact and any known workaround. Maintainers aim to acknowledge reports within seven days and will coordinate disclosure after a fix is available.

Do not probe systems you do not own or have explicit permission to test. Never attach a real SSH configuration, private key, inventory file or unredacted monitor response.

The implementation threat model and deployment boundary are documented in [docs/SECURITY.md](../docs/SECURITY.md).
