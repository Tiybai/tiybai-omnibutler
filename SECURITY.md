# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Contact the
maintainers privately through the repository owner's GitHub profile, with:

- a description of the issue and its impact,
- steps to reproduce (mock driver preferred - never include real tokens,
  device keys, addresses, or account information).

We aim to acknowledge reports within 7 days.

## Security model

The full model - risk levels, key handling, exposure allowlists, audit and
remote-access rules - is documented in [docs/security.md](docs/security.md).
The headlines:

- High-risk devices (locks, garage doors, gas valves) are never directly
  controllable by agents; human confirmation is required.
- Secrets live in the user's environment / local secret store only.
- Never expose the bridge to the public internet; remote access only via
  Cloudflare Access or WireGuard.

## Supported versions

Only the latest release line receives security fixes while the project is
pre-1.0.
