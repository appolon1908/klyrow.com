# Dependency updates (Dependabot)

Config: `.github/dependabot.yml`. Runs weekly (Monday 06:00 America/La_Paz), max 5 open PRs
per ecosystem, commit prefix `chore(deps)`, minor+patch updates grouped into one PR per ecosystem.
Major bumps arrive as individual PRs.

## Coverage

| Ecosystem        | Directories                                  | Manifest                          |
| ---------------- | -------------------------------------------- | --------------------------------- |
| `pip`            | `/apps/gateway`, `/`                         | `requirements.txt`, `requirements-dev.txt` |
| `npm`            | `/apps/web`, `/tools/contracts`              | `pnpm-lock.yaml`, `package-lock.json` |
| `docker`         | `/apps/gateway`, `/apps/web`                 | `Dockerfile` base images          |
| `github-actions` | `/`                                          | `.github/workflows/*.yml`         |

`/docker` and `/apps/postal-provisioner` have no Dockerfile or Gemfile today; add entries when they do.

## Reviewing pinned bumps

- **GitHub Actions** are pinned to full commit SHAs with a `# vX.Y.Z` comment. Dependabot bumps both;
  confirm the SHA belongs to the tagged release in the upstream repo before approving.
- **Docker base images** are pinned by digest. Dependabot bumps the digest; CI rebuilds the image and
  verifies the signature (cosign) and scans it (Trivy). Do not merge a digest bump whose CI image job is red.

## Gateway pip closure rule

`apps/gateway/requirements.txt` pins the full transitive closure and is installed with `pip install --no-deps`.
For every Dependabot PR against it: install into a clean venv, run `pip check`, and add or bump any
transitive pins the new version requires. A PR that leaves the closure incomplete fails the CI image build.
