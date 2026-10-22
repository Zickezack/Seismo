# Data Model

This document describes the project's key entities, relationships, and data constraints. Update it as the domain becomes clearer.

## Purpose
- Capture the core domain model for the application.
- Define entities, attributes, and relationships.
- Identify validation, privacy, and persistence requirements.

## Conventions
- Each entity entry lists: German UI term (used in the UI, see `requirements.md`), technical name, store (SQL table or MongoDB collection), fields, relationships, and privacy classification.
- Mark privacy-sensitive fields with `[privacy]`.
- Persistent means stored in the database volume. Nothing in the app container filesystem is persistent (`architecture.md`, "Deployment and Persistence").
- Changes to this file follow skill `datenmodell-aendern`.

## Domain terms

German UI terms (`requirements.md`, "Basic UI Requirements / Styleguide") and their technical names. All are planned for PostgreSQL. A term gets its full entry under "Entities" when the story that introduces it is implemented.

| UI term | Technical name | Meaning | Privacy |
|---------|----------------|---------|---------|
| Quelle | `source` | A news source with its adapter type (RSS, GDELT, ...), fetch rules, terms-of-use note and raw-text retention period. | none |
| Artikel | `article` | One fetched report. Its metadata (source, URL, publication time, fetch time, language) is kept permanently. | none |
| Rohtext | `raw_text` | An article's full text in the temporary raw-data layer. | none |
| Extraktion | `extraction` | One LLM run on an article, with model version, schema version, extraction time and validation result. | none |
| Ereigniskandidat | `event_candidate` | An event as extracted from a single article, before it is assigned to an event. | none |
| Ereignis | `event` | The durable main object with a stable ID, collecting all reports about one real-world event. | none |
| Ereigniszustand | `event_state` | One timestamped version of an event's state (start, update, escalation, closure, resumption). | none |
| Aussage | `statement` | One extracted statement, labelled Tatsache, Behauptung, Quellenprognose or Interpretation. | none |
| Beleg | `evidence` | The source reference and text quote supporting a statement. | none |
| Entität | `entity` | A company, subsidiary, person, country, authority, product, commodity or sector. | [privacy] for persons |
| Namensform | `entity_alias` | A name variant or identifier of an entity. | [privacy] for persons |
| Beziehung | `entity_relationship` | A relationship between entities: supplier, customer, competitor, regulator, commodity dependency. | none |
| Wertpapier | `instrument` | A tradable security (stock, ETF, index) with symbol, ISIN, exchange and currency, optionally linked to an Entität. Its tracked flag (verfolgt) decides whether its prices are collected continuously. | none |
| Kurspunkt | `price_point` | One price of a Wertpapier at a point in time or over an interval (open, high, low, close, volume), with UTC timestamp, resolution, source and a real-time or delayed flag. | none |
| Wirkungsannahme | `impact_hypothesis` | An assumed effect of an event on companies, sectors or assets. | none |
| Prognose | `forecast` | A forecast made by the platform, with creation time, data state, model version and target metric. | none |
| Ergebnis | `forecast_outcome` | The actual outcome of a forecast. | none |
| Watchlist | `watchlist` | Entities, sectors or event types an analyst follows. | none |
| Alert | `alert_rule` | A rule that notifies about new events or material changes. | none |
| Job | `job` | A unit of work in the PostgreSQL job queue, with status, lease and retry count. | none |

## Model-wide rules

These follow from the architecture principles (`architecture.md`) and apply to every entity.

1. The event, not the article, is the durable main object. Articles are evidence for events.
2. Every material statement is traceable to its evidence (source, article, quote), its timestamps and its extraction (model version, schema version, extraction time).
3. Events, relationships and impact hypotheses are versioned: a change adds a new version and never overwrites an earlier state unnoticed. Merges and splits of events are recorded in an audit trail.
4. An article's publication time and fetch time are stored separately, in UTC.
5. Missing information is stored as unknown, never filled with a guess.
6. Every statement has a kind: Tatsache, Behauptung, Quellenprognose or Interpretation. Quellenprognose (a forecast made in a source) is kept apart from Prognose (a forecast made by the platform). Contradicting statements are stored side by side, never merged into one fact.
7. Confidence is stored separately for extraction, source quality and inference, never combined into a single score.
8. A raw text is deleted according to its source's retention period, and only after processing and validation have succeeded. Metadata, source references and permissible evidence quotes are kept, so events stay verifiable.
9. Uncertain matches between a name form and an entity are flagged, never merged silently.
10. Relationships carry a source, a validity period and a quality flag; historical changes are kept.
11. Point in time: every analysis input records when it became available, so an analysis for a reference date only uses data that was available on that date.
12. A forecast cannot be changed once it is logged; its outcome is stored in a separate record.
13. Persons are personal data. Until user management exists, only the admin can read and export them. Logs reference entities and articles by ID only, never by name or text.
14. Kurspunkte are stored unadjusted, as received, in UTC, with the time they became available; split and dividend adjustments are stored separately. Full-resolution Kurspunkte of tracked Wertpapiere are kept for the configured retention period and then compressed into coarser intervals, never deleted without a replacement. Collection gaps stay gaps and are never interpolated.

## Entities

No entity is implemented yet. Each term from "Domain terms" gets its entry here, with fields, when the story that introduces it is implemented.

<!-- Template - copy once per entity:

### <German UI term> (`technical_name`)
- Store: SQL table `name` | MongoDB collection `name`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id    |      | yes      |         |       |

- Relationships:
- Privacy: none | contains [privacy] fields
-->

## Diagram
