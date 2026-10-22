---
name: verify
description: Verify a code change in the running app - run the test suite, start the app, and exercise the changed behavior; drive every visual or interactive change in a headless browser. Use this whenever the user asks to prüfen, testen, verifizieren, im Browser checken, or kontrollieren ob es funktioniert, after implementing any change before calling it done, and before every commit.
---

Announce once when this skill loads: `Skill aktiv: verify`.

Why: status codes and API tests miss CSS/JS-only bugs - a style rule overriding another, an event handler never attached, an unwanted page reload. Rule: `ai-rules.md`, "Verifying UI and Interactive Behavior".

## Launch recipe

<!-- Maintained by the AI: fill after the first successful run, correct only when a step failed or was missing. Commands that actually worked, nothing else. -->
- Start: `.env` must exist (from `.env.example`). `docker compose up -d --build`, then poll `docker compose ps --format '{{.Service}}={{.Health}}'` until `api=healthy` and `worker=healthy` (about 10 s); `migrate` shows `Exited (0)`.
- App URL: `http://localhost:8000` - `GET /health/live`, `GET /health/ready` (200 with `{"status":"ok","database":"ok","schema":"current"}`). Logs: `docker compose logs --no-log-prefix api worker migrate`, one JSON object per line.
- Test suite: inside the dev container `python -m pytest` (integration tests need its `db`). From the host without VS Code: `npx -y @devcontainers/cli@latest up --workspace-folder .`, then `npx -y @devcontainers/cli@latest exec --workspace-folder . python -m pytest`. Never `pkill -f <pattern>` inside `exec bash -c '...'`: the pattern also matches that shell and kills it.
- Stop and cleanup: `docker compose down -v` (stack and its `db-data` volume); `docker compose -p finnewsint-dev down -v` (dev container, its database and its Claude Code login volume - leave out `-v` to keep the login).

If this section is empty or a step fails, work out the steps from the README, compose files, and devcontainer config, then record exactly what worked.

## Scope by change type

- Docs or tests only: skip.
- Backend only: test suite, then exercise the changed endpoint or flow against the running app.
- Visual or interactive: additionally drive it in a headless browser.

## Browser verification

1. Use the project's existing browser test tool. If none exists, propose Playwright as a dev dependency with rationale and wait for approval (`ai-rules.md`, "Dependencies and Libraries").
2. Exercise the change as a user would: click, type, submit. Check that the expected state changed (visible/hidden, enabled/disabled, text) and that no unwanted page reload happened.
3. Check the browser console for errors.
4. Check desktop, tablet, and mobile viewport widths.
5. Anchor assertions to precise, uniquely identifying structure (id, `data-testid`, role plus accessible name). Never assert that text appears somewhere within a large region - such checks eventually match the wrong occurrence, and the resulting failure or false pass is hard to diagnose.

## Cleanup in shared or persistent environments

- Delete all data created during verification.
- Stop every server and browser process you started.

Leftovers look exactly like application bugs later.

## When a bug is found

Fix it, then search the codebase for the same faulty pattern (`ai-rules.md`, "Testing") before calling the fix done.

## Report

What was verified, how (commands, viewports), the result, and anything not verified with the reason.
