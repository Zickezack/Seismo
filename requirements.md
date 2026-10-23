# Requirements

This document captures the initial goals, user needs, and acceptance criteria for the project.

## Goals
The platform turns global news into a persistent event and knowledge model and derives verifiable hypotheses about economic developments and financial markets. It should answer:
1. Which events are currently economically or geopolitically relevant?
2. Which events are developing, escalating or losing importance?
3. Which companies, sectors, commodities and supply chains are affected?
4. Which positive and negative effects are plausible?
5. Which time horizon is relevant for a possible effect: days, weeks, months or years?
6. Which sources support or contradict a hypothesis?
7. How often have comparable historical events led to similar market outcomes?
8. How good were past assessments compared with benchmarks?

The platform first provides research support, not autonomous trading decisions.

## Non-goals of the First Version
1. No autonomous stock trading.
2. No claim that news reliably predicts price gains.
3. No complete global source coverage from the start.
4. No mandatory graph database or distributed infrastructure.
5. No permanent full-text archiving without checking the data rights.
6. No uncontrolled processing of every article by a large LLM.

## Definition of Done for the Overall Approach
1. The platform is a viable research base when it continuously extracts events from sources, merges them correctly over time, preserves source evidence, models company and sector relationships traceably, and reproducibly evaluates hypotheses stored in advance against market data that became available later.

## Stages
Each stage has a goal and success criteria. Resources per stage: `architecture.md`, "Stages and hardware sizing".

### Stage 1 - Functional Prototype
Goal: a complete end-to-end process with a limited set of sources.
1. New articles are ingested reliably and deduplicated.
2. The system extracts structured events from articles.
3. Several articles can be assigned to one shared event.
4. An event shows its development, sources and affected entities.
5. The system produces first traceable impact assumptions for companies or sectors.

### Stage 2 - Continuous Operation
Goal: a reliable daily news and event process.
1. Restarts and temporary source outages cause no data loss.
2. Repeated articles are recognised.
3. Events are updated continuously.
4. Alerts and watchlists work.
5. Raw data is retained and deleted automatically according to rules.

### Stage 3 - Historical Research
Goal: analyse events over months and years and evaluate their market effects empirically.
1. Events can be linked to market movements in the matching time window.
2. Forecasts and actual outcomes are stored separately.
3. Backtests take into account which data was available at the time.
4. Comparable historical events can be searched systematically.
5. Results are checked against suitable benchmarks.

### Stage 4 - Scaled Analysis
Goal: process millions of articles, many languages and long historical periods.
1. Backfills and live processing can run in parallel.
2. Resource-intensive historical analyses do not block live operation.
3. Data, model and analysis versions are reproducible.
4. Capacity planning is based on measured throughput and storage values.

## Data Sources and Data Rights
1. Start with open or lawfully usable sources. Check the licence terms before using live, intraday or historical stock prices or fundamental data.
2. Respect each source's terms of use and text licensing. The full text of every article is fetched from its URL; checking each source's terms is the admin's responsibility.
3. Full texts are stored only temporarily and deleted according to the source's retention period after successful processing (`datamodel.md`, "Model-wide rules").
4. Full texts are fetched only where the publisher does not reserve text and data mining - by a machine-readable reservation (TDM Reservation Protocol) or because the admin lists its domain, its terms forbidding it; otherwise only the article's metadata is kept.

## Acceptance Criteria per Epic
Grouped by the epics in `userstories.md`. A story is complete only when the criteria of its epic that it touches hold.

### Projektfundament und Laufzeitumgebung
1. The system can be started reproducibly on the AI-Station.
2. A restart does not leave jobs in an inconsistent state.
3. Database backups can be restored as a test.
4. Resource usage and errors are visible.

### Quellenadapter und Ingestion
1. A new source adapter can be added without changing the event model.
2. Failed fetches are logged and retried.
3. Publication time and actual fetch time are stored separately.
4. Repeated fetches do not create duplicate articles.

### Normalisierung, Deduplizierung und Clustering
1. Identical articles are recognised reliably.
2. Similar articles can be linked to each other without blindly merging different events.
3. Every article stays traceable to its source.
4. Wrongly merged articles can be corrected.

### Strukturierte Ereignisextraktion
1. LLM outputs conform to a defined schema.
2. Missing information is marked as unknown, not invented.
3. Material facts carry source evidence.
4. Model version and extraction time are stored.

### Ereignisidentität und zeitliche Entwicklung
1. Several reports about the same event result in one consistent event record.
2. Changes to an event's state are traceable over time.
3. Contradicting reports are not silently merged into a single fact.
4. Merges and splits stay auditable.

### Entitäten und Wissensmodell
1. Different names of the same company are merged as far as possible.
2. Uncertain identity matches are flagged.
3. Relationships have a source, a validity period and a quality flag.
4. Historical changes to company relationships are kept.

### Verdichtung und Daten-Lifecycle
1. Articles are released for deletion only after successful processing.
2. Events stay understandable and verifiable after their raw texts are deleted.
3. Available source references and permissible evidence are kept.
4. Storage usage is monitored.
5. Licence and retention rules can be configured per source.

### Live-Kurse und Kursdatenspeicher
1. Every displayed price shows its timestamp, source, exchange, currency and whether it is real-time or delayed.
2. Prices of tracked Wertpapiere are collected continuously; collection gaps are recorded as gaps, never interpolated.
3. Full-resolution price points are kept for at least the configured retention period, then compressed into coarser intervals, never deleted without a replacement.
4. Prices are stored unadjusted, as received, with the time they became available; split and dividend adjustments are kept separately.
5. The storage usage of the price data is visible.

### Markt- und Wirkungsmodell
1. Every impact assumption names its mechanism, affected entities, time horizon and evidence.
2. Counterarguments and alternative scenarios are recorded.
3. The system distinguishes the direction of an effect from its probability.
4. Insufficient data leads to low confidence or an abstention.

### Historische Markt- und Ereignisdaten
1. An analysis can reconstruct which information was available on a historical reference date.
2. Later information never flows into earlier forecasts.
3. Results can be compared against a suitable market or sector benchmark.
4. Data gaps and adjustments are documented.

### Prognoseprotokoll und Evaluation
1. Every forecast is logged immutably before its outcome is known.
2. Backtests are reproducible.
3. Baselines and costs are documented.
4. A model is never rated successful on a good in-sample result alone.
5. Paper trading is mandatory before any productive trading.

### Dashboard, Recherche und Alerts
1. Every displayed hypothesis can be traced back to the available sources.
2. New events and material changes can be filtered.
3. Alerts are configurable and can be throttled.
4. Uncertainty is visible and not obscured by a single score.

### Skalierung und Betriebsqualität
1. Historical processing does not affect live ingestion uncontrolled.
2. Workers can be scaled independently.
3. Throughput and bottlenecks are measurable.
4. Capacity expansions are decided on measured values.

## Basic UI Requirements / Styleguide
1. Language is in german (Use terminology of datamodel.md)
2. UI must be responsive to render on big screen, tablet and mobile phone
3. All UI elements should be identical across all pages (e.g. buttons, input fields, tables, etc.)
4. A page with several independent content sections (e.g. an admin/settings page) should show a single row of in-page navigation links near the top, one per section. Clicking a link scrolls to that section and expands it if it is currently collapsed.
5. Each such section should be independently collapsible, expanded by default, so a long multi-section page stays scannable without forcing a user to scroll past everything to reach the part they need.
6. Content sections stack vertically (single column) - never place two separate, independently-titled sections side by side in a multi-column grid; it reads as two different pages glued together. Multi-column layout is only for closely related fields within one form or section (e.g. two short fields sharing a row), never for separate sections.
7. Within a form, pair short fields (a name and a role, two numeric values) two to a row instead of stacking every field in its own row, while keeping a wide field (a text area, a file picker, a submit button) spanning the full row.

## Destructive and Bulk Actions
1. A delete action must show a confirmation dialog with a short mandatory wait (a few seconds) before the confirm button becomes clickable, so an accidental click can't cause data loss.
2. When a list allows acting on more than one item (delete, export, ...), use one checkbox per row plus bulk-action buttons that act on the selected rows, instead of a separate action button per row. Bulk actions are atomic: if any selected item can't be processed (e.g. it's still referenced elsewhere), none of the selected items are processed, and the message names which ones blocked it.
3. Prefer saving small, frequent edits (e.g. renaming an item in a list) through an in-place request that does not reload the page, with a lightweight inline confirmation (a brief "Saved" message near the field). Reserve full-page reloads for actions that materially change what's on the page.

## Lists and Ordering
1. Lists of named items (categories, tags, uploaded assets, users) should be sorted by display name, case-insensitively, rather than by creation order - creation order is rarely what a user is looking for once a list grows past a handful of items.

## Settings
1. Settings are split into two scopes, shown as separate areas: Plattform-Einstellungen (global for the instance, admin only) and Benutzer-Einstellungen (per user; each user sees and edits only their own).
2. Every setting belongs to exactly one scope. The two scopes are stored separately and protected by separate permission checks.
3. Benutzer-Einstellungen exist only once user management exists (see `architecture.md`, "Optional feature backlog"). Until then, only Plattform-Einstellungen are built.
4. Import and export of settings state which scope they cover. Secrets and credentials are never exported.
