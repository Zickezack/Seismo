---
name: settings-seite
description: Build or extend settings - Plattform-Einstellungen (global, admin only) and Benutzer-Einstellungen (per user) - with separate storage and permissions, a quick-navigation row, and collapsible sections. Use this whenever the user mentions Einstellungen, Settings, Admin-Bereich, Konfiguration, Optionen, Profil, an admin page, or adds any configurable option, even a single one.
---

Announce once when this skill loads: `Skill aktiv: settings-seite`.

Rules: `requirements.md`, sections "Settings" and "Basic UI Requirements" (items 4-7); `architecture.md`, "Optional feature backlog" (admin settings area, user management) and the "law of the land" rule. This skill is the implementation procedure.

## 1. Assign the scope

- For every setting decide: Plattform (applies to the whole instance, admin only) or Benutzer (applies to one user, edited only by that user). Ask when unclear - a wrong scope means a data migration later.
- If user management does not exist yet, build only Plattform-Einstellungen. Record any user-scoped setting as a backlog item in `userstories.md` instead of attaching it to the admin account.

## 2. Storage and access

- Platform settings: one record per instance. User settings: one record per user, keyed by user id. Document both in `datamodel.md` via skill `datenmodell-aendern`.
- Read every setting with a default, so a missing value never breaks a page.
- Separate routes per scope. Platform routes require admin. User routes act only on the current user's record - take the user id from the session, never from the request.
- Use the same auth/permission convention as all other routes.

## 3. Layout

- The two scopes are separate areas (separate pages or routes), never mixed within one section.
- Within an area: single column of sections, each section collapsible and expanded by default.
- Quick-navigation row near the top, one link per section, only once the area has enough sections to need it - not for one or two (`requirements.md`, item 4: "several"). A quick-nav click scrolls to the section and expands it if collapsed.
- Within a section: short fields two per row, wide fields (text area, file picker, submit button) spanning the full row.
- Collapse with a native mechanism (`<details>` or the `hidden` attribute). No style rule may override `hidden`.
- Small edits save in place without reload (`requirements.md`, "Destructive and Bulk Actions", item 3).

## 4. Import and export

- Every import/export states its scope. Platform export contains no user data unless explicitly selected.
- Never export secrets or credentials.
- Import validates the whole file before writing anything; an invalid file changes nothing.

## 5. Tests

- Permissions: a non-admin can neither read nor write platform settings; user A cannot change user B's settings.
- Defaults apply when a setting is missing.
- Import of an invalid file changes nothing.
- Browser behavior (quick-nav scroll and expand, collapse, responsive layout): skill `verify`.
