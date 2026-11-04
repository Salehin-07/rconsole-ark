# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private reporting:
[Report a vulnerability](https://github.com/Salehin-07/rconsole-ark/security/advisories/new).

Include the Rconsole version (`rconsole version`), how you deployed it, what you did and what you expected.

## Supported versions

Only the latest release receives fixes.

## Threat model

Rconsole is built for one owner and a few trusted people. Its Linux systems are PRoot environments, not hardened
sandboxes. Read the full [security model](docs/security.md) for what is and is not isolated.
