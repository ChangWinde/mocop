# ADR-0036: Independent training freshness guard

## Status

Accepted.

## Context

A platform migration caused authenticated reads to return redirects while old
training rows remained available. A separate browser discovery failure hid new
GPU specifications. A live HTTP server alone did not establish that its training
worker, list, browser observations, or metrics were advancing.

## Candidates

An in-process check alone reuses the existing process and is easy to ship, but
cannot report a dead or blocked monitor to an operator who has closed the page.
An independent local scheduled check can read the authenticated cache without
remote queries, reserve bounded recovery attempts, and continue running if an
upgrade removes the installed training extensions. It requires a separate
private policy, state file, and operating-system schedule.

## Decision

Use a shared pure health projection plus an opt-in independent local guard.
The Mac deployment runs a standalone copy of `training_guard.py`,
`training_health.py`, and `platform_origin.py` every minute under launchd,
using an interpreter outside the package environment. It reads only the
configured loopback API with the private dashboard capability and never uses
the attended-dashboard marker. The collector retains ownership of upstream
reads, credential handling, backoff, and request budgets.

Policy controls freshness thresholds, confirmation checks, notification and
recovery cooldowns, browser reopening, and monitor restart limits. Default
policy disables actions. Action reservations are written atomically before
side effects, persist across invocations, and are protected by a process lock.
No remote job, server, SSH setting, proxy, or token is modified.

Only an unreachable local process or a collector with a stale attempt heartbeat
can recommend monitor restart. Authentication, migration, HTTP denial, missing
extensions, and rate limiting require an alarm rather than repeated restarts.
A closed platform tab may be reopened in the explicit default Chrome user
profile; missing login or Apple Events permission is not bypassed. Recovery
never discovers a new origin or forwards credentials through redirects.

## Consequences

The dashboard shows health and the independent check time. The guard records
incidents, local notification attempts and bounded recovery actions even when
no dashboard is open. macOS acceptance of a notification command does not prove
that notification permissions allowed a visible alert. Mac sleep, shutdown,
loss of the interpreter, and manual login or cross-origin migration still need
operator attention. Existing polling and explicit unknown-data semantics remain
necessary; the guard does not manufacture card counts or utilization.
