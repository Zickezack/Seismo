# AI Rules and Prompt Guidance

This document defines how to work with AI in this project. It combines project development standards with guidance for prompting, reviewing, and integrating AI-generated contributions.

## Purpose

- Make AI usage predictable, safe, and effective.
- Keep AI output aligned with project goals, architecture, and conventions.
- Ensure generated code is readable, maintainable, and properly tested.
- Preserve human ownership and review responsibility.
- Capture conventions for prompting, reviewing, and iterating.

## General Principles

- Treat AI output as a draft, not as final code.
- Always review, test, and adapt generated suggestions before integrating them.
- Prefer simple, explicit solutions over clever or magical abstractions.
- Keep the codebase consistent with its existing style and architecture.
- Do not rely on AI for architectural decisions without human validation.
- Do not change unrelated files unless explicitly requested or clearly necessary.
- Track important assumptions and unresolved questions as comments or issues.

## Skills (Recurring Procedures)

- Recurring multi-step procedures live in `.claude/skills/<name>/SKILL.md`, one folder per skill (Agent Skills open standard, https://agentskills.io).
- Division of responsibility: the rule files (`architecture.md`, `requirements.md`, `datamodel.md`, this file) define what must hold. Skills define how to carry out a task and reference the rule files instead of copying them. If a skill and a rule file disagree, the rule file wins and the skill must be fixed.
- Whenever a skill is used, state it at the start of the reply: `Skill aktiv: <name>`.
- Tools without skill support: before a task matching one of the skills below, read the corresponding `SKILL.md` and follow it.
- Do not create, rename, or remove skills without human approval. When a skill is added, renamed, or removed, update the table below in the same change. The table is an index only; each skill's `SKILL.md` is authoritative.
- Manual-only skills are not visible to Claude Code's automatic skill matching. If a request matches a manual-only skill, do not start the task without it: name the matching command (e.g. `/neues-feature <story>`) and wait for the user to invoke it.

| Skill | Use for | Invocation |
|-------|---------|------------|
| `neues-feature` | Implementing a user story end to end | Manual only: `/neues-feature <story>` |
| `listen-ui` | List views, sorting, bulk actions, delete confirmation, inline rename | Automatic or `/listen-ui` |
| `settings-seite` | Plattform- and Benutzer-Einstellungen, settings layout | Automatic or `/settings-seite` |
| `seed-import` | Seed, starter, and demo data | Automatic or `/seed-import` |
| `verify` | Verifying changes in the running app, UI in a headless browser; runs before each commit | Automatic or `/verify` |
| `datenmodell-aendern` | Any change to tables, collections, fields, or entities | Automatic or `/datenmodell-aendern` |
| `deploy-setup` | CI/CD pipeline and Portainer deployment setup | Manual only: `/deploy-setup <deviations>` |

## Coding Style

- Use clear, descriptive names for variables, functions, and types.
- Favor consistency with the existing codebase over personal preference.
- Add comments only when they explain why something is done or clarify a non-obvious decision.
- Keep functions small and focused.
- Avoid deep nesting and long parameter lists.
- Prefer explicit behavior over complex abstractions.
- Keep generated code readable and idiomatic.

## Testing

- Every feature should include appropriate tests before it is considered complete.
- Use tests to capture expected behavior, error cases, and important edge cases.
- Prefer small, targeted unit tests.
- Add integration tests for critical flows and interactions between components.
- Ask the AI to generate or update tests when implementing or changing behavior.
- Do not accept generated code without verifying it with tests.
- If testing is not practical, document the reason and identify suitable manual verification steps.
- When a bug is found and fixed, check whether the same faulty pattern was independently repeated elsewhere in the codebase before considering the fix complete.

## Verifying UI and Interactive Behavior

- Status codes and API-level tests do not catch CSS/JS-only bugs. Verify every visual or interactive change by exercising it in a real or headless browser, not only by checking the backend response. Procedure: skill `verify`.

## Security and Privacy

- Never generate, commit, or hardcode secrets, keys, tokens, passwords, or credentials. Use gitignore and env variables for sensitive information.
- Do not include sensitive production data in AI prompts.
- Validate and sanitize all external input.
- Use secure defaults and avoid risky shortcuts.
- Keep privacy-sensitive data clearly identified in the data model.
- Consider authentication, authorization, data exposure, and error handling when changing security-relevant code.
- Review generated code for security and privacy implications before merging.

## Dependencies and Libraries

- Prefer stable, well-maintained dependencies.
- Avoid adding packages without a clear and justified need.
- Do not add dependencies merely to solve a small problem that can be handled with existing project capabilities.
- Document every new dependency with a short rationale.
- Review the license, maintenance status, and security implications of new dependencies where relevant.

## AI Usage Rules

- Provide relevant context before asking for code changes.
- Include the target file or module, desired behavior, and applicable constraints.
- Mention existing project conventions, patterns, and architectural requirements.
- Ask the AI to include tests and documentation when applicable.
- Request clarification of assumptions when requirements are incomplete or ambiguous.
- Ask the AI to avoid unrelated changes.
- Review all generated output carefully before committing it.
- Validate correctness, clarity, maintainability, security, and performance.
- Do not accept architectural changes without human review and approval.

## Prompt Guidance

A useful request should include:

- What the feature or change should do.
- Where it should live in the project.
- Which files or modules may be changed.
- Existing patterns or styles to follow.
- Input and output formats.
- Validation rules and edge cases.
- Testing requirements.
- Documentation requirements.
- Constraints such as backward compatibility or performance expectations.

### Example prompt structure

1. Describe the goal and expected behavior.
2. Identify the relevant file, module, or component.
3. Explain the constraints and conventions to follow.
4. Mention validation, error handling, and edge cases.
5. Ask explicitly for tests and documentation.
6. Request a short summary of the proposed changes and any assumptions.

## Example Prompts

### Example 1: Add a helper function

```text
Add a helper function in `utils/string.ts` called `normalizeTitle`.

It should trim whitespace, collapse repeated spaces into one, and convert the
string to title case. Follow the existing project style and keep the function
small.

Also create unit tests covering normal input, an empty string, and strings with
extra spaces. Do not change unrelated files.
```

### Example 2: Implement a user story with a skill

```text
/neues-feature Admin / Settings UX - bulk remove list items with one checkbox per row
```
