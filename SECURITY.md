# Security Policy

## Supported version

Security fixes are applied to the current `1.x` release line.

## Reporting

Do not open a public issue for a vulnerability that could expose local files,
credentials, or arbitrary code execution. Use GitHub's private vulnerability reporting
for this repository when available.

## Intended boundary

Liner is a single-user local application. It is not designed for LAN or Internet
exposure. Do not change the bind address from `127.0.0.1` or place it behind a reverse
proxy without adding authentication and conducting a new threat-model review.

The project never needs administrator privileges. Reports involving operation only
after explicitly exposing the application remotely are outside the supported model,
but defense-in-depth improvements are still welcome.
