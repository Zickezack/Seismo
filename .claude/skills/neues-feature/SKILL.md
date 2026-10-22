---
name: neues-feature
description: Implement a feature or user story end to end - clarify, plan, layered implementation, tests, verification, docs, and story status in userstories.md. Invoke manually with /neues-feature.
argument-hint: "[user story text or epic + story]"
disable-model-invocation: true
---

Announce once when this skill loads: `Skill aktiv: neues-feature`.

Feature to implement: $ARGUMENTS

The rules live in the project docs; this skill is only the procedure. If a doc and this skill disagree, the doc wins - point out the conflict.

## 1. Understand

1. Find the story in `userstories.md`. If it does not exist, draft it under the fitting epic in "Backlog" and get the user's confirmation before continuing.
2. Read the parts of `requirements.md`, `architecture.md` (especially "Codebase structure") and `datamodel.md` that the story touches.
3. List open questions and assumptions. Ask before implementing when a requirement is ambiguous - a wrong guess costs more than a question.

## 2. Plan

1. State the goal, the files to create or change, the data model impact, and the tests to add.
2. Any architectural change (new service, new layer, new convention, new dependency) needs explicit user approval before implementation.
3. For a new dependency, give name, rationale, license, and maintenance status (`ai-rules.md`, "Dependencies and Libraries").
4. Move the story from "Backlog" to "Planned".

## 3. Implement

Work in this order so each layer is testable on its own:

1. Data model change: follow skill `datenmodell-aendern`.
2. Repository: data access only.
3. Service: business rules, no web framework objects.
4. Route/handler: thin, using the same auth/permission convention as every other route.
5. UI: list views follow skill `listen-ui`, settings follow skill `settings-seite`, seed data follows skill `seed-import`.

Change only the files named in the plan. If something else needs changing, say so first.

## 4. Test and verify

1. Unit tests for the service rules, including error cases and edge cases.
2. Integration test for the critical flow.
3. Run the full test suite.
4. Verify in the running app: skill `verify`.

## 5. Close

1. Update `datamodel.md` if entities changed. Propose changes to `requirements.md` or `architecture.md` only when the feature introduced a new rule - never edit them silently.
2. Move the story to "Implemented".
3. Report: changed files, tests added, verification done, assumptions, open questions.
