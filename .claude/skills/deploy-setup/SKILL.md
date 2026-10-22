---
name: deploy-setup
description: Set up the CI/CD pipeline and Portainer deployment - tests before image build, image push, main branch to live, optional dev branch to staging, persistent volumes, and secrets. Invoke manually with /deploy-setup; arguments describe deviations from the standard setup.
argument-hint: "[Abweichungen, z.B. 'ohne Staging']"
disable-model-invocation: true
---

Announce once when this skill loads: `Skill aktiv: deploy-setup`.

Deviations for this project: $ARGUMENTS

Deviations given here take precedence over the standard steps below. If a deviation conflicts with a rule in `architecture.md`, point out the conflict and ask before proceeding.

Rules: `architecture.md`, sections "Structure", "Deployment and Persistence", "Seed Data". This skill sets up the pipeline; it never deploys anything itself.

## 1. Inventory

- Repo host and CI system, container registry, Portainer setup (one stack per environment, how a stack picks up new images). Ask for anything not visible in the repo; never guess hostnames, registries, or webhook URLs.
- List every compose service and what it must persist.

## 2. Pipeline

1. Every push and pull request: run the test suite. A failing suite stops the pipeline; no image is built.
2. `main`, after green tests: build, tag with the commit SHA plus `main`, push, trigger the Portainer update for live.
3. `dev` (optional), after green tests: same, tagged `dev`, to staging.
4. Other branches: tests only.

## 3. Compose for deployment

- The app container is stateless. Everything that must survive a redeploy, including uploaded files, goes to the database or an explicitly declared persistent volume; list each one.
- Health checks for every service.
- No seed step and no implicit data creation on start.
- Database migrations: decide with the user when Alembic runs - as an explicit one-shot job per deploy, or on container start. Running on start is only safe with a single app replica; concurrent replicas race.

## 4. Secrets

- No secrets in the repo, compose files, or images.
- CI secrets for registry and webhook; runtime values (admin credentials, database password) via the Portainer stack environment.
- `.env` is gitignored; commit an `.env.example` with variable names and placeholder values only.

## 5. Document

Record the actual setup (environments, branches, registry, image tags, rollback procedure) in a "Deployment" subsection of `architecture.md` - facts only, no secrets.

## 6. Verify

- A deliberately failing test on a throwaway branch blocks the image build.
- Data survives a redeploy: create a record on staging, redeploy, the record is still there.
- Rollback works: redeploy the previous SHA tag.
