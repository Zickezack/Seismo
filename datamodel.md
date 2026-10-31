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
| Quelle | `source` | A news source with its adapter type (RSS, GDELT, ...), fetch rules, terms-of-use note, raw-text retention period and Abrufstatus (last success, last error, failures in a row). | none |
| Artikel | `article` | One fetched report. Its metadata (source, URL, publication time, fetch time, language) is kept permanently. | none |
| Rohtext | `raw_text` | An article's full text in the temporary raw-data layer. | none |
| Verknüpfung | `article_link` | Two articles with similar texts - a near-duplicate, a syndicated or lightly edited copy - or a pair the admin separated. UI: Ähnliche Artikel. | none |
| Extraktion | `extraction` | One LLM run on an article, with model version, schema version, extraction time and validation result. | none |
| Ereigniskandidat | `event_candidate` | An event as extracted from a single article, before it is assigned to an event. | none |
| Ereignis | `event` | The durable main object with a stable ID, collecting all reports about one real-world event. | none |
| Ereigniszustand | `event_state` | One timestamped version of an event's state (start, update, escalation, closure, resumption). | none |
| Korrektur | `event_correction` | A merge or split of events by the analyst, with the reports it moved and the events they left: the audit trail of rule 3. | none |
| Aussage | `statement` | One extracted statement, labelled Tatsache, Behauptung, Quellenprognose or Interpretation. | none |
| Beleg | `evidence` | The source reference and text quote supporting a statement. | none |
| Entität | `entity` | A company, subsidiary, person, country, authority, organisation, product, commodity or sector. | [privacy] for persons |
| Namensform | `entity_alias` | A name variant or identifier of an entity, flagged while uncertain. | [privacy] for persons |
| Nennung | `entity_mention` | An entity named in a report, as an actor or an impact's target, by one of its name forms. | none |
| Entitätenerkennung | `entity_recognition` | The LLM run that recognised the entities an extraction's reports name. | none |
| Beziehung | `entity_relationship` | A relationship between entities: supplier, customer, competitor, regulator, commodity dependency. | none |
| Angabe | `relationship_record` | One statement about a relationship: a report's that it holds or ended, with its Beleg, or the analyst's validity and review, with its source. | none |
| Wertpapier | `instrument` | A tradable security (stock, ETF, index) with symbol, ISIN, exchange and currency, optionally linked to an Entität. Its tracked flag (verfolgt) decides whether its prices are collected continuously. | none |
| Kurspunkt | `price_point` | One price of a Wertpapier at a point in time or over an interval (open, high, low, close, volume), with UTC timestamp, resolution, source and a real-time or delayed flag. | none |
| Wirkungsannahme | `impact_hypothesis` | An assumed effect of an event on companies, sectors or assets. | none |
| Prognose | `forecast` | A forecast made by the platform, with creation time, data state, model version and target metric. | none |
| Ergebnis | `forecast_outcome` | The actual outcome of a forecast. | none |
| Watchlist | `watchlist` | Entities, sectors or event types an analyst follows. | none |
| Alert | `alert_rule` | A rule that notifies about new events or material changes. | none |
| Job | `job` | A unit of work in the PostgreSQL job queue, with status, lease and retry count. | none |
| Plattform-Einstellungen | `platform_settings` | Settings that apply to the whole instance, edited by the admin (`requirements.md`, "Settings"). | none |
| Backfill | `backfill` | A past period of a Quelle fetched after the fact, page by page, always behind live work. | none |
| Neuverarbeitung | `reprocessing` | The runs of one step - event extraction or entity recognition - made with an older schema version or another model, run again for the articles the admin chooses, always behind live work. | none |
| Speichermessung | `storage_measurement` | What the data took on one day: the database and its tables, the raw texts, the backups and the free space. | none |
| Domain | `fetch_host` | The fetch state of one host: its robots.txt, its request slots and a pause it asked for. Not shown in the UI. | none |
| Seed-Protokoll | `seed_log` | Which items of a seed source (e.g. the starter catalogue of Quellen) were imported once (`architecture.md`, "Seed Data"). Not shown in the UI. | none |

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
13. Persons are personal data. Only persons in a public or business role become entities; a private individual a report names stays in its actors only. Until user management exists, only the admin can read and export them. Logs reference entities and articles by ID only, never by name or text.
14. Kurspunkte are stored unadjusted, as received, in UTC, with the time they became available; split and dividend adjustments are stored separately. Full-resolution Kurspunkte of tracked Wertpapiere are kept for the configured retention period and then compressed into coarser intervals, never deleted without a replacement. Collection gaps stay gaps and are never interpolated.

## Entities

Each term from "Domain terms" gets its entry here, with fields, when the story that introduces it is implemented.

### Job (`job`)
- Store: SQL table `job`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | kind | varchar(100) | yes | | Job type, e.g. `testjob`; a worker claims only the kinds it has a handler for. UI: Art. |
  | payload | jsonb | yes | `{}` | Input of the job: IDs only, never names or texts (rule 13). |
  | dedupe_key | varchar(200) | no | | While a job with this key waits or runs, no second one is queued (partial unique index `uq_job_dedupe_key_active`). |
  | status | varchar(20) | yes | `queued` | `queued`, `running`, `succeeded`, `failed` (check constraint). UI: Wartend, Läuft, Erledigt, Fehlgeschlagen. |
  | attempts | integer | yes | 0 | Counted when a worker claims the job; not counted when a stopping worker hands it back. Reset to 0 when the admin requeues a failed job. |
  | max_attempts | integer | yes | 5 | After the last attempt the job fails instead of being retried. Volltext abrufen: 31, about 24 hours with the growing delay. |
  | priority | smallint | yes | 0 | A worker claims due jobs with a lower number first: 0 live work, 1 backfill (its pages and the full texts of its articles). Index `ix_job_status_priority_run_after`. UI: "(Backfill)" after the kind. |
  | run_after | timestamptz | yes | now() | Earliest time of the next attempt; failed attempts wait 30 s, doubling up to 1 h. |
  | lease_token | uuid | no | | New on every claim. A worker writes to the job only while its token is still current (fencing). |
  | lease_owner | varchar(200) | no | | Worker holding the lease: container hostname and process id. |
  | lease_expires_at | timestamptz | no | | Renewed while the job runs. An expired lease puts the job back into the queue, or fails it after the last attempt. |
  | last_error | text | no | | Error type and message of the last failed attempt, at most 2000 characters. Shown to the admin, never logged. |
  | created_at | timestamptz | yes | now() | |
  | updated_at | timestamptz | yes | now() | Last change of status or attempt; a lease renewal does not count. |
  | started_at | timestamptz | no | | Start of the latest attempt. |
  | finished_at | timestamptz | no | | Set when the job succeeded or failed; the retention period counts from here. |

- Relationships: none yet; job kinds reference their records by ID in `payload`.
- Rules: a job's result is written in the same transaction that marks it succeeded, and only while its lease is current, so a retried job has no duplicate effect. All times come from the database clock. The worker deletes succeeded and failed jobs once an hour after their retention period (Plattform-Einstellungen); waiting and running jobs are never deleted.
- Privacy: none

### Plattform-Einstellungen (`platform_settings`)
- Store: SQL table `platform_settings`, exactly one row (`id` = 1, check constraint)
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | smallint | yes | 1 | Always 1. |
  | llm_max_parallel_calls | integer | yes | 1 | Most LLM calls running at once across all workers, 1 to 32. Enforced with PostgreSQL advisory locks, one per slot; read on every slot request, so a change applies without a restart. |
  | succeeded_jobs_retention_days | integer | yes | 7 | Days a succeeded job is kept after `finished_at`, 1 to 3650. |
  | failed_jobs_retention_days | integer | yes | 30 | Days a failed job is kept after `finished_at`, 1 to 3650. |
  | backup_interval_hours | integer | yes | 24 | Hours from the newest backup file to the next automatic backup, 1 to 720. |
  | backups_to_keep | integer | yes | 14 | Backup files kept; after each new backup the oldest beyond this number are deleted, 1 to 365. |
  | article_max_age_days | integer | yes | 7 | Items published longer ago than this are not taken by the live fetch; they belong to the backfill. Items without a date always come. 1 to 365. |
  | domain_min_interval_seconds | integer | yes | 10 | Minimum time between two requests to the same host, feeds and full texts alike; a longer Crawl-delay in the host's robots.txt wins. 1 to 600. |
  | tdm_blocked_domains | text | no | the starter list | Domains whose terms forbid text and data mining, one per line, sorted, at most 500; each covers its subdomains. A full text from them is not fetched (Vorbehalten). Empty: none. Starter list (NULL): the publishers found in October 2026 (`tdm_policy.STARTER_BLOCKED_DOMAINS`). UI: Domains ohne Volltext. |
  | extraction_languages | varchar(200) | yes | `de,en,es,fr,it,nl,pt` | Languages the event extraction takes (ISO 639-1 codes, comma-separated, sorted): the language recognised in the text, else the one stated. UI: Ereignisextraktion - Sprachen. |
  | extraction_min_text_chars | integer | yes | 500 | Shorter cleaned texts are not extracted; check constraint >= 0. UI: Mindestlänge des Texts (Zeichen). |
  | extraction_max_age_days | integer | yes | 3 | Older articles (publication, else fetch time) are not extracted; check constraint >= 1. UI: Höchstalter (Tage). |
  | extractions_per_hour | integer | yes | 30 | At most this many extraction jobs are queued per hour, newest articles first; 0 holds the extraction; check constraint >= 0. UI: Extraktionen pro Stunde (höchstens). |
  | reprocessings_per_hour | integer | yes | 30 | At most this many jobs of a Neuverarbeitung are queued per hour - extractions and recognitions alike, an article's own counted as well -, apart from the extraction's budget; 0 holds the reprocessing; check constraint >= 0. UI: Verarbeitung - Neuverarbeitungen pro Stunde (höchstens). |
  | source_quality_domains | text | no | the catalogue's suggestions | "domain level" per line, level hoch, mittel or niedrig, sorted by domain; a domain covers its subdomains, the most specific one wins. NULL: the suggestions the code starts with (`platform_settings_service.STARTER_SOURCE_QUALITY`: official bodies hoch, established media mittel, press release portals niedrig - the admin's decision of 2026-10-24). UI: Ereignisextraktion - Quellenqualität nach Domain. |
  | updated_at | timestamptz | yes | now() | |

- Relationships: none
- Rules: every setting is read with its default, so a missing row or column never breaks a page. Admin only. Benutzer-Einstellungen are stored separately once user management exists.
- Privacy: none

### Quelle (`source`)
- Store: SQL table `source`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | name | varchar(200) | yes | | Unique regardless of case (unique index `uq_source_name` on `lower(name)`). Sorted with the ICU collation `de-x-icu`. UI: Name. |
  | adapter_type | varchar(20) | yes | | Adapter that fetches the source: `rss`, `gdelt` (GDELT DOC 2.0 API), `reliefweb` (ReliefWeb API v2, reports), `eia` (EIA Open Data API v2, data releases). Checked in the service, not by a constraint, so a new adapter needs no migration. UI: Typ. |
  | url | varchar(2000) | yes | | Feed or API address, `http` or `https` with a host. Left empty for `gdelt` and `reliefweb`, the API address is filled in; for `eia` the API URL of one data set from EIA's API browser. Never with an API key: a parameter `api_key`, `apikey`, `api-key` or `access_token` is refused, the key comes from the environment. Not unique: one endpoint can serve several sources, e.g. every GDELT query. |
  | query | text | no | | The search of an adapter that accepts one, in its syntax, at most 1000 characters; required for `gdelt`, optional for `reliefweb` (empty: every new report), empty for `rss`. UI: Suchanfrage. |
  | is_active | boolean | yes | true | Only active sources are fetched. UI: Aktiv / Inaktiv. |
  | extract_events | boolean | yes | true | Its articles pass the event extraction's pre-filter. UI: Ereignisse extrahieren. |
  | quality | varchar(10) | no | | `high`, `medium`, `low`; check constraint. The quality of its articles' quotes where no domain of the list in the Plattform-Einstellungen matches; empty: unknown. UI: Quellenqualität - Hoch, Mittel, Niedrig, Unbekannt. |
  | fetch_interval_minutes | integer | yes | 60 | Time between two fetches, 5 to 1440. UI: Abrufintervall. |
  | language | varchar(10) | no | | ISO 639-1 code; empty for a source in several or unknown languages (rule 5). The language of each article is stored separately. UI: Sprache. |
  | region | varchar(20) | no | | What the source reports on: `africa`, `asia`, `europe`, `middle_east`, `north_america`, `south_america`, `oceania`, `worldwide` (UI: Afrika, Asien, Europa, Naher Osten, Nordamerika, Südamerika, Ozeanien, Weltweit; the list sorts in this order). Checked in the service, not by a constraint. Required by the form; empty only for sources from before regions existed (UI: Ohne Region). UI: Region. |
  | terms_note | text | no | | What the source's terms allow, with a link, at most 2000 characters. Checking them is the admin's job (`requirements.md`, "Data Sources and Data Rights"). UI: Nutzungsbedingungen. |
  | raw_text_retention_days | integer | yes | 30 | Days a raw text is kept after its processing has succeeded, 0 to 365; 0 deletes it right after (rule 8). UI: Rohtext aufbewahren. |
  | min_request_interval_seconds | integer | no | | Minimum time between two requests to the same host for this source, feed and full texts alike, 1 to 600; empty: `domain_min_interval_seconds`. Only raises it, never lowers it. UI: Mindestabstand. |
  | next_fetch_at | timestamptz | no | | When the worker queues the next fetch; empty or past means due. Moves one interval ahead when a fetch is queued. |
  | last_fetched_at | timestamptz | no | | Last successful fetch. UI: Letzter Erfolg. |
  | last_error | text | no | | Message of the last failed fetch, at most 2000 characters, e.g. `HTTP 503 (Retry-After 120 s)`. Kept after a success. Shown to the admin; logged only when it is the adapter's own message. UI: Letzter Fehler. |
  | last_error_at | timestamptz | no | | When the last fetch failed. |
  | consecutive_failures | integer | yes | 0 | Failed fetch attempts since the last success, each retry counted; a success sets it to 0. A fetch waiting for its request slot is no failure. UI: Fehler in Folge. |
  | created_at | timestamptz | yes | now() | |
  | updated_at | timestamptz | yes | now() | Last change through the form or a bulk action. |

- Relationships: has many Artikel (`article.source_id`).
- Rules: bulk actions (Jetzt abrufen, Blockierte Volltexte abrufen, Aktivieren, Deaktivieren, Region setzen, Exportieren, Löschen) are all or nothing; an export carries the form fields only and its import skips a name in use (`transfer_service`, README.md, "Configuration export and import"); the list filters by region (or Ohne Region) and sorts by name or by region, then name; an inactive source blocks Jetzt abrufen. Abrufstatus: OK (no failure since the last success), n Fehler in Folge, or Noch nicht abgerufen; it covers the feed fetch only, a full text's outcome is kept at its Artikel. Deleting a source keeps its articles (they keep a copy of its name and URL). Seeded sources come only from the starter catalogue import (`app/seeds/sources.json`, Seed-Protokoll), active, with their region. "Regionen übernehmen" (Plattform-Einstellungen, Starterdaten) gives a source without a region the one of its catalogue item, matched by URL and query; it never overwrites a region.
- Privacy: none

### Artikel (`article`)
- Store: SQL table `article`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | source_id | bigint | no | | Foreign key to `source`, `ON DELETE SET NULL`: empty once the source is deleted. |
  | source_name | varchar(200) | yes | | Name of the source at fetch time; shown once the source is deleted, otherwise its current name. |
  | source_url | varchar(2000) | yes | | Feed URL of the source at fetch time. |
  | item_key | varchar(1000) | yes | | The item's id in its source (RSS guid, Atom id, `reliefweb:<report id>`, `eia:<route>:<period>`), else its link; above 1000 bytes `sha256:<hex>` of it. Unique with `source_id` (`uq_article_source_id`), so a repeated fetch stores nothing twice. |
  | url | varchar(2000) | yes | | Link of the article, `http` or `https`. |
  | title | text | no | | As the source states it, at most 1000 characters; after normalization the page's own (og:title, else `<title>`) where the source states none or is GDELT, whose titles come tokenized (`7 : 32 a . m . EDT`). |
  | published_at | timestamptz | no | | Publication time as the source states it, in UTC (RSS pubDate, Atom published, else the item's only date: RSS 1.0 dc:date, Atom updated); empty when it states none (rules 4 and 5). GDELT states none: when GDELT first saw the article (seendate, 15-minute steps), the admin's decision of 2026-10-23. ReliefWeb: when it posted the report (`date.created`, to the second; `date.original`, the original publisher's day, is not used). EIA states none: empty. A time more than a day ahead counts as none, the feed's and the page's alike. UI: Veröffentlicht. |
  | fetched_at | timestamptz | yes | now() | When it was stored. UI: Abgerufen. Index `ix_article_fetched_at`. |
  | language | varchar(10) | no | | ISO 639 code as the source states it for the item or the feed (GDELT: its English language name, translated), else the language of the Quelle; empty if neither has one. |
  | full_text_status | varchar(20) | yes | `pending` | `pending`, `stored`, `disallowed` (robots.txt), `unsupported` (content type), `failed`, `blocked` (the host refused it, HTTP 401 or 403, often a bot protection), `reserved` (the publisher reserves text and data mining: its domain is on the list, or TDMRep says so - nothing of the page is kept), `skipped` (source inactive or deleted, stored before full texts existed, or a duplicate by its link: the page is the original's); check constraint. Set once, from `pending`; only the admin's "Blockierte Volltexte abrufen" puts `blocked` back to `pending`, and migration `3201aa52f946` put the `failed` ones whose host's robots.txt or tdmrep.json was above its limit back, fetched again behind live work. UI: Volltext - Ausstehend, Gespeichert, Gesperrt, Nicht unterstützt, Fehlgeschlagen, Blockiert, Vorbehalten, Übersprungen. |
  | full_text_error | text | no | | Why the full text is not stored, e.g. `HTTP 404`; when blocked, also whether a contact was in the User-Agent. Shown to the admin, never logged. |
  | backfill_id | bigint | no | | The Backfill that found it, foreign key `ON DELETE SET NULL`; empty for an article of the live fetch, also when a backfill finds it again. |
  | canonical_url | varchar(2000) | no | | The link without tracking parameters (`utm_*`, `fbclid`, `traffic_source`, ...), fragment and default port, scheme and host in lower case (`app/services/urls.py`); after normalization the page's own `<link rel="canonical">` in that form. The full text is fetched from it; `url` keeps the link as the source gave it. |
  | published_at_source | varchar(10) | no | | Where `published_at` came from: `feed`, `gdelt` (seendate) or `page` (the page's own full timestamp with time zone: `article:published_time`, JSON-LD `datePublished`); check constraint. Empty without a time. |
  | detected_language | varchar(10) | no | | ISO 639-1 code recognised in the cleaned text (py3langid, among GDELT's languages), only from 200 characters on and at confidence 0.6; else empty (rule 5). UI: Sprache shows it, else `language`. |
  | normalization_status | varchar(20) | yes | `pending` | `pending`, `done`, `failed` (a raw text no reader understands); check constraint. Partial index `ix_article_normalization_pending`. UI: Volltext - "Text n Zeichen", "kein Text erkannt", "Text nicht lesbar". |
  | normalization_error | text | no | | Why the text could not be read. Shown to the admin, never logged. |
  | normalized_at | timestamptz | no | | |
  | content_hash | varchar(64) | no | | SHA-256 of the cleaned text, from 200 characters on; else empty. Kept with the metadata, so a copy is recognised after its original's raw text is deleted. Index `ix_article_content_hash`. |
  | duplicate_of_id | bigint | no | | The original this article duplicates (`duplicate_service`), foreign key to `article`, `ON DELETE SET NULL`; empty for an original. Never a duplicate itself. UI: "Duplikat (gleiche URL) von" / "Duplikat (gleicher Text) von" and the original's source, linked to it; filter "Duplikate ausblenden". |
  | duplicate_reason | varchar(10) | no | | How it was recognised: `url` (the same `canonical_url`) or `text` (the same `content_hash` on another host); check constraint. Kept when its group gets an original published earlier. |
  | text_signature | integer[] | no | | MinHash of the cleaned text (`similarity_service`): 128 values over its word 5-grams - character 5-grams for a script without spaces between words - from 200 characters on; else empty. Kept with the metadata, so a text is compared after its raw text is deleted. |
  | text_bands | bigint[] | no | | The signature's 32 bands of 4 values, hashed: an article sharing one is a candidate for a similar text. GIN index `ix_article_text_bands` (`&&`). |

- Relationships: belongs to a Quelle while it exists; has at most one Rohtext; may be the duplicate of one original Artikel, which has any number of them; an original has any number of Verknüpfungen.
- Rules: metadata is kept permanently. A source adapter (`app/sources/`) returns items in one shape; only items within the maximum age (Plattform-Einstellungen) and with a web link become articles. Once its full text is settled, the worker normalizes an article (job "Artikel normalisieren", `normalization_service`): canonical URL, the page's title where the source states none or only GDELT's tokenized one, the page's publication time where the feed states none or only GDELT's seendate, detected language, cleaned text. A page without a main text (an app shell, an error page) gives none of its metadata: it may concern another page - the EC press corner's shell names its home page as canonical. Identical articles (story 2, `duplicate_service`) are kept - every source that found one stays traceable - and point to their original: the same `canonical_url`, checked at ingestion (a duplicate by its link gets no full text) and after normalization (the page's own canonical link), or the same `content_hash` on another host - the same text on one host under another canonical URL is a page that is no report (an error page, a consent wall), never a duplicate. The original is the article published first (the admin's decision of 2026-10-24), on equal times the one stored first; an unknown time never comes before a known one (rule 5). An article found later but published earlier becomes the original, and the group follows it; the order is settled when an article joins, a time that changes afterwards does not reorder it. Later steps work on originals only. Similar originals (story 3) are linked in pairs, never merged: see Verknüpfung.
- Privacy: none

### Rohtext (`raw_text`)
- Store: SQL table `raw_text`, one row per article
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | article_id | bigint | yes | | Primary key; foreign key to `article`, `ON DELETE CASCADE`. |
  | content | bytea | yes | | The answer as received, not decoded: HTML, XHTML, plain text or PDF, at most 10 MB. PostgreSQL compresses it (TOAST). For a source that delivers the text itself (EIA: written by the adapter from the release's numbers, one line per series with its change against the previous period) that text as UTF-8, `text/plain`, stored with the article - no page is fetched. |
  | content_type | varchar(200) | no | | The Content-Type header, with its charset. |
  | url | varchar(2000) | yes | | Where it was fetched from, after redirects; for a delivered text the article's link. |
  | fetched_at | timestamptz | yes | now() | |
  | plain_text | text | no | | The main text, cleaned: without navigation, ads and comments (trafilatura for HTML, pypdf for PDF); empty when none was found. Full text as well: it goes with the raw text (rule 8). |

- Relationships: belongs to one Artikel.
- Rules: the temporary raw-data layer: deleted after its source's retention period once processing has succeeded (rule 8, epic Verdichtung und Daten-Lifecycle); nothing deletes it yet. The normalisation turns it into plain text. A Neuverarbeitung's new extraction reads its plain text: an article without one cannot be extracted again. The Datenbestand page shows what the raw texts take, per Quelle too.
- Privacy: none

### Verknüpfung (`article_link`)
- Store: SQL table `article_link`, one row per pair
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | article_id | bigint | yes | | The pair's lower article id; primary key with `other_id`; foreign key to `article`, `ON DELETE CASCADE`. Check constraint `article_id < other_id`. |
  | other_id | bigint | yes | | The pair's higher article id; foreign key to `article`, `ON DELETE CASCADE`. Index `ix_article_link_other_id`. |
  | similarity | double precision | no | | The estimated share of word 5-grams the two texts have in common (Jaccard, from their signatures); empty for a pair separated without a link ("Kein Duplikat"). UI: Ähnlichkeit, in percent. |
  | created_at | timestamptz | yes | now() | |
  | separated_at | timestamptz | no | | Set by the admin ("Verknüpfung aufheben", "Kein Duplikat"): the two are unrelated, never linked again, and neither becomes the other's duplicate. |

- Relationships: pairs two Artikel.
- Rules: made at normalization (`similarity_service`), between originals only - a duplicate's links are its original's: two texts from 60 % estimated similarity (the admin's decision of 2026-10-24), published (else fetched) within 7 days of each other, never two identical texts on one host (an error page, see Artikel). Every link is a measured pair: A linked to B and B to C does not link A and C. When an original becomes a duplicate, its pairs go to the new original, a separation winning over a link. The article page (UI: Artikel, `/articles/<id>`) lists an original's similar articles first published first, marks the first, and separates selected ones; it also separates a duplicate from its original ("Kein Duplikat": the duplicate becomes an original of its own, normalized again, its full text fetched when it was skipped). Kept with the articles; pruning belongs to the epic Verdichtung und Daten-Lifecycle.
- Privacy: none

### Extraktion (`extraction`)
- Store: SQL table `extraction`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | article_id | bigint | yes | | Foreign key to `article`, `ON DELETE CASCADE`. Index. |
  | model | varchar(200) | yes | | The model as Ollama names it, e.g. `qwen3.5:9b`. |
  | schema_version | varchar(20) | yes | | `event_schema.SCHEMA_VERSION`: the fields, their meaning and the prompt the model answered to. |
  | extracted_at | timestamptz | yes | now() | |
  | duration_ms | integer | yes | | The model call, waiting for an LLM slot included. |
  | input_chars | integer | yes | | Characters of the cleaned text the model saw. |
  | truncated | boolean | yes | | The text was cut to fit the model's context (`OLLAMA_NUM_CTX`). UI: Text gekürzt. |
  | statements_dropped | integer | yes | 0 | Statements whose quote is not in that text: not stored, nor an event left without one. |
  | validation_result | varchar(20) | yes | `valid` | `valid`, `invalid` (outside the schema), `incomplete` (cut before its end); check constraint. Every model run is stored, an unusable one at once in its own transaction. UI: Ausserhalb des Schemas, Abgeschnitten. |
  | validation_errors | text | no | | Of an unusable run: what was wrong, one line per error - where and what, never the value (`event_schema.error_summary`). Shown to the admin; logged is only their number. |
  | raw_answer | text | no | | Of an unusable run: the model's answer, at most 20000 characters, for diagnosis. May quote the article: shown to the admin, never logged. |
  | repaired | boolean | yes | false | The answer to the follow-up question, asked with the errors of the first answer (not the answer itself, which would not fit the context). A valid one lowers every statement's extraction level by one. UI: gültig erst auf Nachfrage. |
  | replaced_by_id | bigint | no | | The extraction of its article that replaced it (Neuverarbeitung): empty for the article's current reading. Foreign key to `extraction`, `ON DELETE SET NULL`. Index. |
  | reprocessing_id | bigint | no | | The Neuverarbeitung that made it; empty for the live extraction and an article's own reprocessing. Foreign key to `reprocessing`, `ON DELETE SET NULL`. Index. |

- Relationships: belongs to one Artikel; has any number of Ereigniskandidaten.
- Rules: made by the job "Ereignisse extrahieren" (`extraction_service`) for an article that passes the pre-filter: an original (never a duplicate, never a later copy of a linked article) of a source that extracts, in a listed language, with enough text, not too old - at most a number per hour, newest first (Plattform-Einstellungen). One current reading per article: its valid run no reprocessing replaced - a Neuverarbeitung's new extraction replaces it (`replaced_by_id`), the old one is kept with its reports, statements and quotes; a run with an unusable answer is stored as well, and the job asks the model once more right away, told what was wrong; a second unusable answer leaves the job to its retry. An article whose extraction job failed comes back only when the admin requeues the job. Every text the model writes is English, every quote verbatim. Kept with the metadata.
- Privacy: none

### Ereigniskandidat (`event_candidate`)
- Store: SQL table `event_candidate`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | extraction_id | bigint | yes | | Foreign key to `extraction`, `ON DELETE CASCADE`. Index. |
  | article_id | bigint | yes | | Foreign key to `article`, `ON DELETE CASCADE`. Index. |
  | position | smallint | yes | | Its place in the model's answer, the most important first. |
  | event_type | varchar(40) | yes | | `armed_conflict`, `terrorism`, `civil_unrest`, `natural_disaster`, `accident`, `cyber_incident`, `infrastructure_outage`, `supply_chain_disruption`, `energy_supply`, `commodity_market`, `monetary_policy`, `fiscal_policy`, `trade_policy` (tariffs, sanctions, export controls), `regulation`, `election_politics`, `diplomacy`, `corporate`, `labor`, `health`, `legal`, `economic_data`, `other`; check constraint. UI: German labels (`extraction_service.EVENT_TYPE_LABELS`). |
  | status | varchar(20) | yes | | `start`, `update`, `escalation`, `closure`, `resumption` (as the states of an Ereignis), `unknown`; check constraint. UI: Beginn, Fortsetzung, Eskalation, Abschluss, Wiederaufnahme, Status unbekannt. |
  | title | text | yes | | English, at most 200 characters. |
  | summary | text | yes | | English, at most 1000 characters. |
  | country | varchar(2) | no | | ISO 3166-1 alpha-2, where the article states it (rule 5). |
  | place | text | no | | As the article names it. |
  | location_quote | text | no | | The passage that names place and country (schema 2); without one found, both are empty - unknown, never guessed. |
  | time_note | text | no | | When the event happens, as the article states it, in a few English words, e.g. "on Friday morning", "effective Nov. 7". No date of the model's own: tried in development, it filled in the publication day for 24 of 27 candidates (rule 5); a date is derived later, in code, with the epic Ereignisidentität und zeitliche Entwicklung. |
  | time_quote | text | no | | The passage that states the time (schema 2); without one found, `time_note` is empty. |
  | inference_confidence | varchar(10) | no | | `high`, `medium`, `low`: how sure type, status and impacts follow from the article, as the model rates it (rule 7); check constraint; empty before schema 2. UI: Schlussfolgerung hoch, mittel, niedrig, unbekannt. |
  | actors | jsonb | yes | `[]` | `[{name, role, quote}]`: who acts or is directly concerned; an actor without a quote found in the text is left out (schema 2). The entities they name: Nennung. |
  | impacts | jsonb | yes | `[]` | `[{target, target_kind, direction, horizon, description, quote}]`: only effects the article itself names, an impact without a quote found left out (schema 2); `target_kind` company, sector, commodity, country, market, other; `direction` positive, negative, mixed, unclear; `horizon` days, weeks, months, years, unknown. Not a Wirkungsannahme of the platform. |
  | event_id | bigint | no | | Its Ereignis; empty until assigned; changed only by a Korrektur. Foreign key to `event`, `ON DELETE SET NULL`. Index. |
  | assignment_method | varchar(10) | no | | How it was assigned: `new` (it founded its event), `score` (similar enough by its words), `llm` (in between, the LLM judged it the same incident); check constraint. A Korrektur that moves the report leaves it, with score and time, as it was; the event page then shows the correction (UI: Zusammenführung, Aufteilung) with this one as "zuerst". UI: Neues Ereignis, Ähnlichkeit, LLM. |
  | assignment_score | double precision | no | | Its similarity to the event when assigned: TF-IDF cosine of the English words of title, summary, place and actors with the event's most similar report, 0 to 1, three decimals. Empty for one that founded its event. |
  | assigned_at | timestamptz | no | | |

- Relationships: belongs to one Extraktion and its Artikel; has one or more Aussagen; belongs to one Ereignis once assigned.
- Rules: stored only with at least one Aussage that has a Beleg. Assigned to an Ereignis by the job "Ereignis zuordnen" (see Ereignis); moved to another only by a Korrektur. A report of a replaced reading (its extraction's `replaced_by_id`) is never assigned; once every report of its article's current reading has its event, it leaves its own (`reprocessing_service.retire_replaced`, a Korrektur Neuverarbeitung of each event it leaves) - until then its successor finds that event through it. It keeps how it was assigned, its statements and its mentions; it is no longer listed with an entity.
- Privacy: none (actors may name persons, private individuals too: shown to the admin only, never logged - rule 13)

### Ereignis (`event`)
- Store: SQL table `event`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | The stable event ID (rule 1). UI: Ereignis-ID. |
  | title | text | yes | | Of the report that founded it, English; kept as later reports come in - renamed only when a Neuverarbeitung replaces the report it is named after. |
  | event_type | varchar(40) | yes | | Of the founding report; the values of Ereigniskandidat, check constraint. |
  | country | varchar(2) | no | | Of the founding report; empty where it states none (rule 5). |
  | first_reported_at | timestamptz | yes | | The publication (else fetch) time of its earliest report. |
  | last_reported_at | timestamptz | yes | | The same of its latest report. Index. |
  | created_at | timestamptz | yes | now() | |
  | founding_candidate_id | bigint | no | | The report it is named after: title, type and country are this report's - the one that founded it, or the earliest of those a split made it of. Never split off. Replaced by a Neuverarbeitung: then its article's new report in the event, else the event's earliest report, gives it title, type and country. Foreign key to `event_candidate`, `ON DELETE SET NULL`. Index. Filled for the events before by migration `0a882e94c29f`: the report with the event's title. UI: Nach diesem Bericht ist das Ereignis benannt. |
  | merged_into_id | bigint | no | | Set by a merge, never cleared: the event its reports went to; or by a Neuverarbeitung that left it without reports: the event its article's new reading's first report went to. Foreign key to `event`, `ON DELETE SET NULL`. UI: Zusammengeführt in. |

- Relationships: has one or more Ereigniskandidaten, its reports - none once merged; has Ereigniszustände, its versions; is named after one of its reports; takes part in Korrekturen; a merged one points to the event it was merged into.
- Rules: one incident with its direct continuation - an attack and its death toll, a quake and its aftershocks - never a topic (the admin's decision of 2026-10-24). The job "Ereignis zuordnen" (`event_service`) assigns every Ereigniskandidat without one, the earliest report first, so the first report founds the event: compared with the events reported from 7 days before to 7 days after it - from 0.5 similarity it joins the most similar, below 0.2 it founds a new one, in between the LLM chooses among the up to 3 most similar or none. Never an event that has a report of the same article - but for its replaced reading's (see Ereigniskandidat). The assignment runs under an advisory lock, after comparing again: two workers never found two events for one incident. The list (UI: Ereignisse, `/events`) shows the 100 most recently reported; the event page (`/events/<id>`) its reports, the earliest first, each with its article and how it came to the event. Its state is its latest Ereigniszustand not withdrawn (UI: Zustand, in the list and on the page); the page shows the versions (UI: Verlauf) and what each report does to the state in effect at its time (UI: Zum Zustand - Zustandswechsel, bestätigt, widerspricht with the state it contradicts). The analyst corrects the grouping on the page (`event_correction_service`), each correction recorded as a Korrektur (rule 3, the admin's decision of 2026-10-30): merging the selected of its similar events into it (UI: Ähnliche Ereignisse - those reported from 7 days before its first to 7 days after its last report, by the similarity of the assignment, an event scoring its report most similar to any of this one's, at most 10; UI: In dieses Ereignis zusammenführen), and splitting off selected reports into one new event, named after the earliest of them (UI: Als neues Ereignis abspalten); the report it is named after stays. A merged event keeps its id and page, without reports, its versions as they were; it is no longer listed, counted, compared or corrected. Both all or nothing, with an optional reason, under the assignment's advisory lock.
- Privacy: none

### Ereigniszustand (`event_state`)
- Store: SQL table `event_state`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | event_id | bigint | yes | | Foreign key to `event`, `ON DELETE CASCADE`. Index with `valid_from`. |
  | status | varchar(20) | yes | | `start`, `update`, `escalation`, `closure`, `resumption` (never `unknown`); check constraint. UI: Beginn, Fortsetzung, Eskalation, Abschluss, Wiederaufnahme. |
  | valid_from | timestamptz | yes | | The time of the report that stated it: its article's publication, else its fetch. UI: Seit. |
  | candidate_id | bigint | no | | The report that stated it - its Beleg is that report's statements and quotes; foreign key to `event_candidate`, `ON DELETE SET NULL`: the version stays. Index. UI: Laut Bericht. |
  | recorded_at | timestamptz | yes | now() | When Seismo learned it (rule 11): a report found later may state an earlier version. UI: Erfasst. |
  | added_by_id | bigint | no | | The Korrektur that appended it; empty when its report's assignment did. Foreign key to `event_correction`. UI: durch Zusammenführung, durch Aufteilung, durch Neuverarbeitung. |
  | withdrawn_by_id | bigint | no | | The Korrektur after which it no longer follows from the event's reports; set once, never cleared. Foreign key to `event_correction`. A withdrawn version stays to be seen and is never in effect. UI: struck through, zurückgezogen durch ... |

- Relationships: belongs to one Ereignis; stated by one Ereigniskandidat; may be appended or withdrawn by a Korrektur.
- Rules: appended in the transaction that assigns its report (`event_service.state_effect`), never changed (rule 3) - but for being withdrawn once. After a Korrektur every event it concerns gets the history its reports make when weighed one by one in the order they were assigned, by the same rule (`event_correction_service.replay`), as if grouped so from the start: a version they no longer make is withdrawn by the correction, one they make anew appended by it, recorded then; a version whose report is gone stays as it is. A merged event's versions stay as they were. Migration `0a882e94c29f` deletes the withdrawn versions on downgrade: the code before would take them for in effect. A report is weighed against the version in effect at its own time - the latest from before it, of equal times the one recorded last: the earliest known status founds the state; while ongoing (start, update, escalation, resumption) an update confirms it, an escalation or a closure is a new version, a start or a resumption contradicts it; while closed a closure confirms it, a resumption is a new version, anything else contradicts it; the same status always confirms; unknown has no effect (the admin's decision of 2026-10-29). A contradicting report adds nothing and keeps its own statements beside the state (rule 6). A version whose status the version before it (by time, not withdrawn) already has - a report found later stated the state earlier - changed nothing: it stays and is shown as a confirmation, its report as confirming, the state counting from the earlier one (`event_service.mark_repeats`, the admin's decision of 2026-10-31). The reports assigned before were replayed by migration `5d0c7e1a9b42` in the order they were assigned, `recorded_at` their assignment time. A date from an article's time note ("on Friday") is not derived; the version shows it beside its time.
- Privacy: none

### Korrektur (`event_correction`)
- Store: SQL table `event_correction`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | kind | varchar(10) | yes | | `merge`, `split`, `reprocess` (the reports of a replaced reading leaving the event); check constraint. UI: Zusammenführung, Aufteilung, Neuverarbeitung. |
  | event_id | bigint | yes | | The event the reports went to: of a merge the one that remains, of a split the new one; of a reprocessing the one they left, for none. Foreign key to `event`, `ON DELETE CASCADE`. Index. |
  | moves | jsonb | yes | | `[{event_id, candidate_ids}]`: the events the reports left, each with its reports moved - of a merge every event merged, of a split the event split, of a reprocessing the event itself. Ids only, so the record stays should a report go. |
  | reason | text | no | | The analyst's reason, at most 1000 characters, trimmed; empty when none was given. Never logged. UI: Begründung. |
  | created_at | timestamptz | yes | now() | UI: Zeit. |

- Relationships: concerns the event it names and the events in its moves; appends and withdraws Ereigniszustände.
- Rules: the audit trail of rule 3, written in the transaction of the merge, split or reprocessing it records, never changed or deleted. Shown on the page of every event it concerns, as that event sees it (UI: Korrekturen - Aufgenommen:, Aufgegangen in, Abgespalten als, Abgespalten aus, Ersetzt durch die neue Extraktion ihres Artikels). Logged by ids, kind and number of reports only (rule 13). Migration `fb4c52ab16d6` deletes the reprocessing's on downgrade, with the versions they withdrew and the replaced readings: as if the articles had only ever been read the new way.
- Privacy: none

### Aussage (`statement`)
- Store: SQL table `statement`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | candidate_id | bigint | yes | | Foreign key to `event_candidate`, `ON DELETE CASCADE`. Index. |
  | position | smallint | yes | | |
  | kind | varchar(20) | yes | | `fact`, `claim`, `source_forecast`, `interpretation` (rule 6); check constraint. UI: Tatsache, Behauptung, Quellenprognose, Interpretation. |
  | text | text | yes | | English, one sentence. |
  | extraction_confidence | varchar(10) | no | | `high`, `medium`, `low`: how faithfully the statement renders its quote, as the model rates it, one lower for a cut text or a repaired answer (rule 7); check constraint; empty before schema 2. UI: Extraktion hoch, mittel, niedrig, unbekannt. |

- Relationships: belongs to one Ereigniskandidat; has one Beleg (from the article it was extracted from).
- Privacy: none

### Beleg (`evidence`)
- Store: SQL table `evidence`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | statement_id | bigint | yes | | Foreign key to `statement`, `ON DELETE CASCADE`. Index. |
  | article_id | bigint | yes | | The article the quote is from; foreign key to `article`, `ON DELETE CASCADE`. Index. |
  | quote | text | yes | | The passage as it stands in the article's cleaned text - found there despite spaces, typographic quotes and dashes the model wrote differently, at least 10 characters. |
  | source_quality | varchar(10) | no | | `high`, `medium`, `low`: the quality of the source the quote is from, as it stood at extraction - the most specific domain of the list in the Plattform-Einstellungen, else the article's Quelle; empty: unknown (rule 7); check constraint. UI: Quelle hoch, mittel, niedrig, unbekannt. |

- Relationships: belongs to one Aussage and one Artikel.
- Rules: a permissible evidence quote, kept after the raw text is deleted (rule 8).
- Privacy: none

### Entität (`entity`)
- Store: SQL table `entity`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | UI: Entität (the number). |
  | kind | varchar(20) | yes | | `company`, `subsidiary`, `person` (in a public or business role only), `country`, `authority` (a named body of one state: government, ministry, central bank, regulator, court, police, armed forces), `organization` (an international organisation, party, union, NGO, movement or armed group - the admin's decision of 2026-10-31), `product`, `commodity`, `sector`; check constraint. UI: Unternehmen, Tochterunternehmen, Person, Land, Behörde, Organisation, Produkt, Rohstoff, Branche. |
  | name | text | yes | | The usual English name it was first recognised with, without legal form and title; of a starter list item its German name. Sorted with the ICU collation `de-x-icu`. [privacy] for persons. |
  | parent_id | bigint | no | | Of a subsidiary the company that owns it, of a product the company that makes it, as the LLM knows it; founded as a company when not known yet. Never replaced by a recognition. Foreign key to `entity`, `ON DELETE SET NULL`. Index. UI: Mutterunternehmen, Hersteller. |
  | merged_into_id | bigint | no | | Set by a merge, never cleared: the entity its name forms went to. Foreign key to `entity`, `ON DELETE SET NULL`. UI: Zusammengeführt in. |
  | created_at | timestamptz | yes | now() | |

- Relationships: has Namensformen, through them Nennungen; may belong to a parent Entität and have subsidiaries and products; a merged one points to the entity it was merged into.
- Rules: recognised by the job "Entitäten erkennen" (see Entitätenerkennung), or imported with the starter list of sectors and commodities (`app/seeds/entities.json`, Seed-Protokoll `entities`) - sectors and commodities come only from that list. The list (UI: Entitäten, `/entities`) shows the 200 first by name, not merged ones, with their name forms, uncertain ones, reports and events; filters by kind, uncertain name forms and a part of a name form. The entity page (`/entities/<id>`) shows name forms, reports and similar entities - those of its kind (a company's subsidiaries too) with a name form of trigram similarity from 0.3, at most 10. The analyst merges the selected similar ones into it (UI: In diese Entität zusammenführen): their name forms, identifiers, subsidiaries and products move to it, all or nothing, of its kind only; it takes the first parent among them should it have none; a merged entity keeps its id and page, without name forms, and is no longer listed or compared; its relationships go along (see Beziehung). Never deleted.
- Privacy: [privacy] for persons: name.

### Namensform (`entity_alias`)
- Store: SQL table `entity_alias`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | entity_id | bigint | yes | | Foreign key to `entity`, `ON DELETE CASCADE`. Index. |
  | scheme | varchar(20) | yes | | `name`; an identifier: `iso3166` (a country's ISO 3166-1 alpha-2 code), `isin`, `lei`, `ticker` (EXCHANGE:SYMBOL), `catalogue` (the key of a starter list item); check constraint. UI: Name, ISO-Ländercode, ISIN, LEI, Ticker, Starterliste. |
  | value | text | yes | | As recognised: a name as the report or the LLM gives it, an identifier in its one spelling. [privacy] for persons. UI: Namensform. |
  | key | text | yes | | What it is looked up by: a name without accents, case, punctuation, a leading "the" and trailing legal forms (`entity_names.name_key`: "Boeing Co." and "The Boeing Company" are `boeing`); an identifier in upper case without spaces. Index with `scheme`; unique for identifiers (`uq_entity_alias_identifier`, `scheme <> 'name'`): an identifier names one entity only. Trigram GIN index on names (`ix_entity_alias_key_trgm`, pg_trgm). |
  | match | varchar(20) | yes | | How it came: `founding` (recognised first with it), `name` (together with a name form the entity has), `identifier` (together with an identifier it has), `llm` (a similar name the LLM judged the same), `seed` (starter list), `split` (split off by the analyst); check constraint. UI: Zuordnung. |
  | similarity | double precision | no | | Of an uncertain one: its trigram similarity to the entity's most similar name form when it came, three decimals. |
  | uncertain | boolean | yes | false | Flagged (rule 9): the LLM only judged a similar name the same, or it was recognised together with an uncertain name form. Cleared only by the analyst's confirmation. UI: unsicher. |
  | confirmed_at | timestamptz | no | | When the analyst confirmed it (UI: Bestätigen). |
  | created_at | timestamptz | yes | now() | UI: Erfasst. |

- Relationships: belongs to one Entität; has Nennungen.
- Rules: added only by a recognition, the starter list import or a correction, all under one advisory lock (`entity_repository.lock_entities`, namespace 7304). A name form carries its reports: moved, they name the entity it went to. The analyst confirms selected uncertain ones, or splits selected ones off into one new entity of the same kind, certain, named after the earliest name form among them (UI: Als neue Entität abtrennen) - all or nothing; at least one name form goes and one stays, a starter list key never goes. Never deleted.
- Privacy: [privacy] for persons: value.

### Nennung (`entity_mention`)
- Store: SQL table `entity_mention`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | candidate_id | bigint | yes | | The report; foreign key to `event_candidate`, `ON DELETE CASCADE`. Index. Unique with `source` and `position`. |
  | alias_id | bigint | yes | | The name form it was recognised by - its entity, and whether it is uncertain; foreign key to `entity_alias`, `ON DELETE CASCADE`. Index. |
  | source | varchar(10) | yes | | `actor` or `impact` (the impact's target); check constraint. UI: Rolle - Akteur, Betroffen. |
  | position | smallint | yes | | Its place in the report's `actors` or `impacts`: role, impact and quote are read from there. |

- Relationships: belongs to one Ereigniskandidat and one Namensform.
- Rules: written with its recognition; a recognition run again (Neuverarbeitung) replaces the extraction's. Of a replaced reading's reports they stay, no longer listed or counted with the entity. A name the reports of one extraction give more than once - by the same key - is one entity at every place. The event page (UI: Entitäten) lists the entities its reports name, the most often named first, with the name forms used and whether one is uncertain; the entity page its reports, the latest 100.
- Privacy: none (it names a person by reference only)

### Entitätenerkennung (`entity_recognition`)
- Store: SQL table `entity_recognition`, one row per extraction
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | extraction_id | bigint | yes | | Primary key; foreign key to `extraction`, `ON DELETE CASCADE`. |
  | model | varchar(200) | no | | The model as Ollama names it; empty when the reports name nobody and no call was made. |
  | schema_version | varchar(20) | yes | | `entity_schema.SCHEMA_VERSION`: the answer's fields, their meaning and the prompt. |
  | recognised_at | timestamptz | yes | now() | |
  | duration_ms | integer | no | | The model call, waiting for an LLM slot included. |
  | names | integer | yes | 0 | The distinct names (by key) of the reports' actors and impact targets the model was asked about, at most 50. |
  | linked | integer | yes | 0 | Of them, those that became a Nennung; the others the model judged no entity, or a sector or commodity not on the list. |
  | relationships | integer | yes | 0 | The Angaben of relationships it recorded (schema 2); 0 for the runs before. |
  | reprocessing_id | bigint | no | | The Neuverarbeitung of this run; empty for the live recognition and an article's own reprocessing. Foreign key to `reprocessing`, `ON DELETE SET NULL`. Index. |

- Relationships: belongs to one Extraktion.
- Rules: the job "Entitäten erkennen" (`entity_service`, one of the LLM jobs) runs for every current reading (an extraction no reprocessing replaced) with reports and without a recognition, a replaced one's never, the earliest article first, queued in the worker's LLM sweep. The model is shown the reports, each name with its role or impact and quote, the known entities with a similar name form (pg_trgm similarity from 0.3, at most 3 per name, 40 in all) and the imported sectors and commodities; it answers each name's kind or none, a named individual's public or business position first (a person without one, or answered as a private individual, is no entity - rule 13), its usual English name, a short form the report uses, the known entity it is when it is sure, a subsidiary's parent or a product's maker, a country's code, a listed item's key and identifiers. An identifier counts only valid (ISIN, LEI by their check digits; a country code assigned, UK as GB) and stated in the name's quote (rule 5). Resolved in the effect's transaction under the advisory lock: an identifier or a name form of an entity of the kind (a company's subsidiary alike) - certain; else the known entity the LLM judged it - uncertain; else a new entity. An entity with a similar name that came since the LLM was asked defers the job (`JobDeferred`): it asks anew. Schema 2 shows the model the reports' statements, numbered with their quotes, and asks for the relationships they state between two names, each citing its statement; one between two names that became entities of kinds that can stand in it is an Angabe (see Beziehung). The runs before have none until a Neuverarbeitung runs them again (job "Entitäten neu erkennen"): a run again takes the row's place - one row per extraction, the latest run's -, replaces the extraction's mentions and marks the Angaben of the run before replaced, in one transaction under the entities' lock. An answer cut or outside the schema is retried with the job. Logged by ids and counts only (rule 13).
- Privacy: none

### Beziehung (`entity_relationship`)
- Store: SQL table `entity_relationship`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | UI: Beziehung (the number). |
  | kind | varchar(20) | yes | | `supplies` (subject supplies object: UI from the subject's side Kunde, from the object's Lieferant), `competes` (UI: Wettbewerber), `regulates` (UI: Reguliert, Regulator), `depends_on` (on a commodity; UI: Abhängig von, Abhängig davon); check constraint. |
  | subject_id | bigint | yes | | Foreign key to `entity`, `ON DELETE CASCADE`. Of a competition the lower id. |
  | object_id | bigint | yes | | Foreign key to `entity`, `ON DELETE CASCADE`. Index. Never the subject (check constraint). |
  | created_at | timestamptz | yes | now() | |

- Relationships: between two Entitäten; has Angaben.
- Rules: unique by kind, subject and object (`uq_entity_relationship_kind`): a supplier and its customer are one relationship, seen from either side. Its sides by kind (`relationship_service.SIDES`): supplies - a company, subsidiary or country to a company, subsidiary, country, authority, organisation or sector; competes - companies, subsidiaries or products; regulates - an authority or organisation of a company, subsidiary, product or sector; depends_on - a company, subsidiary, country or sector on a commodity. Its state is not stored: it follows from its Angaben in the order they were recorded (`relationship_service.replay`), so every earlier state and the state at any past moment stay to be rebuilt (rules 3, 10, 11). The analyst's review is its quality flag (UI: Prüfung - Ungeprüft, Bestätigt, Bestritten; the admin's decision of 2026-10-31); it is in effect while it holds and is not disputed. An Angabe whose reading was replaced no longer counts. The entity page lists an entity's relationships with an Angabe that counts, those in effect first (UI: Beziehungen - Rolle, Entität, Gültig, Prüfung, Berichte), confirms or disputes the selected ones all or nothing, their dates as they are, and takes the analyst's own (UI: Beziehung erfassen - the other's role, the other entity by name, number or the list, validity, a required source; confirmed). The relationship page (`/relationships/<id>`) shows its Angaben with the state after each (UI: Verlauf) and takes the analyst's Angabe of its validity and review (UI: Stand ändern). A merge of entities moves the Angaben of the merged one's relationships to the same relationships of the entity it went into; one between the two stays where it was. Never deleted.
- Privacy: none

### Angabe (`relationship_record`)
- Store: SQL table `relationship_record`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | relationship_id | bigint | yes | | Foreign key to `entity_relationship`, `ON DELETE CASCADE`. Index. |
  | origin | varchar(10) | yes | | `report` or `analyst`; check constraint. UI: Bericht, Analyst. |
  | candidate_id | bigint | no | | Of a report's: the report. Foreign key to `event_candidate`, `ON DELETE SET NULL`: the Angabe stays. Index. |
  | statement_id | bigint | no | | Of a report's: the statement that states it; its Beleg is the evidence - quote and source quality. Foreign key to `statement`, `ON DELETE SET NULL`. |
  | subject_alias_id, object_alias_id | bigint | no | | Of a report's: the name forms it names the subject and the object by. Foreign keys to `entity_alias`, `ON DELETE SET NULL`. Index each. |
  | reported_on | date | no | | Of a report's: its day - its article's publication, else fetch, in UTC. |
  | ended | boolean | yes | false | Of a report's: it says the relationship ended (a contract cancelled, a supplier dropped). UI: beendet, else besteht. |
  | valid_from, valid_to | date | no | | Of the analyst's: the validity it sets; empty `valid_to`: it holds, empty `valid_from`: start unknown. UI: Gültig ab, bis. |
  | review | varchar(20) | no | | Of the analyst's: `unreviewed`, `confirmed`, `disputed`; check constraint. UI: Ungeprüft, Bestätigt, Bestritten. |
  | note | text | no | | Of the analyst's: the source or reason, at most 1000 characters; required when the analyst enters a relationship. Never logged. UI: Quelle. |
  | recorded_at | timestamptz | yes | now() | When Seismo learned it (rule 11): the order of the replay. UI: Erfasst. |
  | replaced_at | timestamptz | no | | Of a report's: when the reading it came from was replaced - its article extracted again, or its extraction's entities recognised again (Neuverarbeitung). From then on it no longer counts; it stays in the history, so the state at an earlier moment can still be rebuilt (rule 11). UI: ersetzt. |

- Relationships: belongs to one Beziehung; a report's to one Ereigniskandidat, one Aussage and two Namensformen.
- Rules: appended, never changed (rule 3) - but for being marked replaced once (`replaced_at`, skipped by the replay), and moved by a correction of entities: a merge (see Beziehung), and a split of name forms, which moves the reports' Angaben that name an entity by a name form split off to the same relationship of the entity they now make; the analyst's stay with the entity they were made for. The replay (the admin's decision of 2026-10-31): the first Angabe founds the state - a report's that it holds from its day, that it ended until its day; later a report's that it holds opens it again after an end before its day, or moves its start earlier; that it ended closes it while it is open and from its start on; anything else changes nothing and confirms. The analyst's sets dates and review as given; a report never changes the review. Written by the recognition in its transaction, by the analyst under the entities' advisory lock.
- Privacy: none

### Backfill (`backfill`)
- Store: SQL table `backfill`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | source_id | bigint | yes | | Foreign key to `source`, `ON DELETE CASCADE`: a backfill goes with its source, its articles stay. |
  | period_start | timestamptz | yes | | Start of the first day, UTC. Not before the adapter's reach (GDELT: 3 months). UI: Zeitraum. |
  | period_end | timestamptz | yes | | End of the last day, UTC, at most the moment it was started; after `period_start` (check constraint). |
  | cursor | timestamptz | yes | | Everything from here to `period_end` is fetched; the next page ends here. Starts at `period_end`, moves back to `period_start`. UI: Fortschritt. |
  | status | varchar(20) | yes | `running` | `running`, `done`, `failed`, `cancelled` (check constraint). UI: Läuft, Abgeschlossen, Fehlgeschlagen, Abgebrochen. |
  | articles_found | integer | yes | 0 | Articles of all pages, also those known before. |
  | articles_new | integer | yes | 0 | Articles stored by it. UI: Artikel - "n neu von m". |
  | last_error | text | no | | Why it failed or ended, e.g. `Quelle inaktiv`. Shown to the admin, never logged. |
  | created_at | timestamptz | yes | now() | UI: Gestartet. |
  | finished_at | timestamptz | no | | |

- Relationships: belongs to a Quelle; has many Artikel (`article.backfill_id`).
- Rules: started by the admin for an active source, over whole days up to today. One job per page (`Backfill abrufen`, priority 1): the newest page of what is left; a full page continues one second past its oldest item, a page that is not full ends it (a feed: one request). Its jobs and the full texts of its new articles wait behind every live job and take only free request slots. Cancelling (all or nothing, running ones only) stops further pages; queued full texts still come. A deactivated source cancels it, a source error fails it.
- Privacy: none

### Neuverarbeitung (`reprocessing`)
- Store: SQL table `reprocessing`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | |
  | step | varchar(20) | yes | | `extraction` (Ereignisextraktion: the article extracted again; its reports' assignment and recognition follow) or `recognition` (Entitätenerkennung of an extraction); check constraint. UI: Schritt. |
  | schema_version | varchar(20) | yes | | The schema version the runs it replaces were made with. UI: Schema. |
  | model | varchar(200) | yes | | The model they were made with. UI: Modell. |
  | source_id | bigint | no | | Only the articles of this Quelle. Foreign key to `source`, `ON DELETE SET NULL`: a Quelle deleted since leaves it empty, and its articles, without theirs, match no more. |
  | source_name | varchar(200) | no | | A copy of its name; set exactly when it is limited to a Quelle. UI: Quelle. |
  | period_start, period_end | date | no | | Only the articles of these days - publication, else fetch, UTC; empty: no limit. Check constraint `period_start <= period_end`. UI: Von, Bis. |
  | status | varchar(20) | yes | `running` | `running`, `done` (each run it selects was queued once and none of its jobs waits or runs), `cancelled` (by the admin; jobs queued before still run); check constraint. UI: Läuft, Abgeschlossen, Abgebrochen. |
  | total | integer | yes | | The runs it selected when it started. UI: Fortschritt - "n von total neu". |
  | created_at | timestamptz | yes | now() | UI: Gestartet. |
  | finished_at | timestamptz | no | | UI: Beendet. |

- Relationships: may be limited to a Quelle; has the Extraktionen and Entitätenerkennungen it made (`reprocessing_id`).
- Rules: started by the admin on the Datenbestand page (`/data`, UI: Neu verarbeiten), one at a time, from a Stand: the current runs of a step - of the extraction the articles' current readings, of the recognition those that asked the model - grouped by schema version and model, each with its number; a Stand made as a run would be made now (the code's schema version, the model in `OLLAMA_MODEL`) is current and offers nothing. "Anzahl prüfen" shows what the choice takes and, of an extraction, how many articles lack the text it needs. The worker queues its jobs ("Ereignisse neu extrahieren", "Entitäten neu erkennen", one per run, a dedupe key per reprocessing and run) behind every live job (priority 1), the newest article first, at most `reprocessings_per_hour`, while Ollama serves the model; a run whose job failed is not queued again by it. Its progress counts the runs it made; failed jobs while they are kept. Cancelling is all or nothing, running ones only. An article's page reprocesses its own outdated steps at once (priority 0, all or nothing; with the extraction, the recognition follows it). Logged by ids and counts only (rule 13).
- Privacy: none

### Speichermessung (`storage_measurement`)
- Store: SQL table `storage_measurement`, one row per day
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | measured_on | date | yes | | Primary key: the UTC day. UI: Tag. |
  | measured_at | timestamptz | yes | now() | |
  | database_bytes | bigint | yes | | `pg_database_size`. UI: Datenbank. |
  | table_bytes | jsonb | yes | | `{table: bytes}`: every table of the schema with its indexes and TOAST (`pg_total_relation_size`). |
  | article_count | bigint | yes | | UI: Artikel. |
  | raw_text_count | bigint | yes | | |
  | raw_text_bytes | bigint | yes | | The raw texts as stored, compressed: the answers and their plain texts (`pg_column_size`). UI: Rohtexte. |
  | backup_count | integer | yes | | The backup files of this database, uploaded ones too. |
  | backup_bytes | bigint | yes | | UI: Backups. |
  | backup_free_bytes | bigint | no | | Free on the backup store's file system; empty when it could not be read. The database volume's own is not visible to the application. UI: Frei. |

- Relationships: none.
- Rules: the worker measures once a UTC day, checking hourly (`storage_service.measure_if_due`); never changed. The Datenbestand page (`/data`) shows the state now - database, largest tables, raw texts and per Quelle, backups, free space - measured as it loads, the latest 60 days, and the growth per day over 7 and 30 days, from the measurement that far back, else the earliest (`requirements.md`, Verdichtung und Daten-Lifecycle, criterion 4).
- Privacy: none

### Domain (`fetch_host`)
- Store: SQL table `fetch_host`, one row per host
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | host | varchar(255) | yes | | Primary key: host name in lower case, with a port other than the default. |
  | next_slot_at | timestamptz | yes | now() | Start of the next free request slot; each reservation moves it one interval ahead. |
  | last_request_at | timestamptz | no | | When the last request was sent; a job running late waits until the interval has passed. |
  | paused_until | timestamptz | no | | The host asked for a pause (Retry-After on 429, 503 and other retryable answers), at most 24 hours; a 429 without one pauses it 5 minutes; no request goes there before, robots.txt included. A shorter pause never ends a longer one. |
  | robots_status | varchar(20) | no | | `rules`, `allow_all` (404 and other 4xx), `disallow_all` (401, 403); check constraint. Empty until asked. |
  | robots_txt | text | no | | The rules, while `robots_status` is `rules`; of a robots.txt above 500 KiB its first 500 KiB up to the last whole line (RFC 9309). |
  | robots_fetched_at | timestamptz | no | | Asked again after 24 hours. |
  | tdmrep_status | varchar(20) | no | | `rules` (a valid `/.well-known/tdmrep.json`) or `none` (no file, a 4xx, no valid TDMRep, or an answer above 100 KB - a page of the host's own); check constraint. Empty until asked. |
  | tdmrep_rules | text | no | | The file's rules as JSON (location, reserved, policy), while `tdmrep_status` is `rules`. |
  | tdmrep_fetched_at | timestamptz | no | | Asked again after 24 hours, before the next full text from the host. |

- Relationships: none; matched by the host of a URL.
- Rules: every request to a source's host - feed or full text - checks robots.txt (group `Seismo`, else `*`) and takes a request slot, never before the end of a pause (app/services/fetch_policy_service.py). A full text checks the host's tdmrep.json first (app/services/tdm_policy.py).
- Privacy: none

### Seed-Protokoll (`seed_log`)
- Store: SQL table `seed_log`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | seed_source | varchar(100) | yes | | Seed source: `sources` for `app/seeds/sources.json`, `entities` for `app/seeds/entities.json` (the starter list of sectors and commodities). Primary key with `item_key`. |
  | item_key | varchar(200) | yes | | The item's stable key in its seed file, independent of its display name. |
  | imported_at | timestamptz | yes | now() | |

- Relationships: none; an entry records only that the item was imported once, not which record it became.
- Rules: an item with an entry is never imported again, even after its record was renamed or deleted. Entry and record are written in one transaction; an item whose name is already taken is neither imported nor logged. Written only by the admin's import action, never by a migration, start or page load.
- Privacy: none

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
