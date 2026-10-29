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
| Plattform-Einstellungen | `platform_settings` | Settings that apply to the whole instance, edited by the admin (`requirements.md`, "Settings"). | none |
| Backfill | `backfill` | A past period of a Quelle fetched after the fact, page by page, always behind live work. | none |
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
13. Persons are personal data. Until user management exists, only the admin can read and export them. Logs reference entities and articles by ID only, never by name or text.
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
  | full_text_status | varchar(20) | yes | `pending` | `pending`, `stored`, `disallowed` (robots.txt), `unsupported` (content type), `failed`, `blocked` (the host refused it, HTTP 401 or 403, often a bot protection), `reserved` (the publisher reserves text and data mining: its domain is on the list, or TDMRep says so - nothing of the page is kept), `skipped` (source inactive or deleted, stored before full texts existed, or a duplicate by its link: the page is the original's); check constraint. Set once, from `pending`; only the admin's "Blockierte Volltexte abrufen" puts `blocked` back to `pending`. UI: Volltext - Ausstehend, Gespeichert, Gesperrt, Nicht unterstützt, Fehlgeschlagen, Blockiert, Vorbehalten, Übersprungen. |
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
- Rules: the temporary raw-data layer: deleted after its source's retention period once processing has succeeded (rule 8, epic Verdichtung und Daten-Lifecycle); nothing deletes it yet. The normalisation turns it into plain text.
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

- Relationships: belongs to one Artikel; has any number of Ereigniskandidaten.
- Rules: made by the job "Ereignisse extrahieren" (`extraction_service`) for an article that passes the pre-filter: an original (never a duplicate, never a later copy of a linked article) of a source that extracts, in a listed language, with enough text, not too old - at most a number per hour, newest first (Plattform-Einstellungen). One valid run per article; a run with an unusable answer is stored as well, and the job asks the model once more right away, told what was wrong; a second unusable answer leaves the job to its retry. An article whose extraction job failed comes back only when the admin requeues the job. Every text the model writes is English, every quote verbatim. Kept with the metadata.
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
  | actors | jsonb | yes | `[]` | `[{name, role, quote}]`: who acts or is directly concerned; an actor without a quote found in the text is left out (schema 2). Entities come with their epic. |
  | impacts | jsonb | yes | `[]` | `[{target, target_kind, direction, horizon, description, quote}]`: only effects the article itself names, an impact without a quote found left out (schema 2); `target_kind` company, sector, commodity, country, market, other; `direction` positive, negative, mixed, unclear; `horizon` days, weeks, months, years, unknown. Not a Wirkungsannahme of the platform. |
  | event_id | bigint | no | | Its Ereignis; empty until assigned. Foreign key to `event`, `ON DELETE SET NULL`. Index. |
  | assignment_method | varchar(10) | no | | How it came to its event: `new` (it founded it), `score` (similar enough by its words), `llm` (in between, the LLM judged it the same incident); check constraint. UI: Neues Ereignis, Ähnlichkeit, LLM. |
  | assignment_score | double precision | no | | Its similarity to the event when assigned: TF-IDF cosine of the English words of title, summary, place and actors with the event's most similar report, 0 to 1, three decimals. Empty for one that founded its event. |
  | assigned_at | timestamptz | no | | |

- Relationships: belongs to one Extraktion and its Artikel; has one or more Aussagen; belongs to one Ereignis once assigned.
- Rules: stored only with at least one Aussage that has a Beleg. Assigned to an Ereignis by the job "Ereignis zuordnen" (see Ereignis).
- Privacy: none (actors may name persons; once entities exist they follow rule 13)

### Ereignis (`event`)
- Store: SQL table `event`
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | id | bigint identity | yes | | The stable event ID (rule 1). UI: Ereignis-ID. |
  | title | text | yes | | Of the report that founded it, English; kept as later reports come in. |
  | event_type | varchar(40) | yes | | Of the founding report; the values of Ereigniskandidat, check constraint. |
  | country | varchar(2) | no | | Of the founding report; empty where it states none (rule 5). |
  | first_reported_at | timestamptz | yes | | The publication (else fetch) time of its earliest report. |
  | last_reported_at | timestamptz | yes | | The same of its latest report. Index. |
  | created_at | timestamptz | yes | now() | |

- Relationships: has one or more Ereigniskandidaten, its reports.
- Rules: one incident with its direct continuation - an attack and its death toll, a quake and its aftershocks - never a topic (the admin's decision of 2026-10-24). The job "Ereignis zuordnen" (`event_service`) assigns every Ereigniskandidat without one, the earliest report first, so the first report founds the event: compared with the events reported from 7 days before to 7 days after it - from 0.5 similarity it joins the most similar, below 0.2 it founds a new one, in between the LLM chooses among the up to 3 most similar or none. Never an event that has a report of the same article. The assignment runs under an advisory lock, after comparing again: two workers never found two events for one incident. The list (UI: Ereignisse, `/events`) shows the 100 most recently reported; the event page (`/events/<id>`) its reports, the earliest first, each with its article and how it came to the event. Merging and splitting, with their audit trail (rule 3), come with story 3 of the epic.
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

### Domain (`fetch_host`)
- Store: SQL table `fetch_host`, one row per host
- Fields:

  | Field | Type | Required | Default | Notes |
  |-------|------|----------|---------|-------|
  | host | varchar(255) | yes | | Primary key: host name in lower case, with a port other than the default. |
  | next_slot_at | timestamptz | yes | now() | Start of the next free request slot; each reservation moves it one interval ahead. |
  | last_request_at | timestamptz | no | | When the last request was sent; a job running late waits until the interval has passed. |
  | paused_until | timestamptz | no | | The host asked for a pause (Retry-After on 429, 503 and other retryable answers), at most 24 hours; no request goes there before, robots.txt included. A shorter pause never ends a longer one. |
  | robots_status | varchar(20) | no | | `rules`, `allow_all` (404 and other 4xx), `disallow_all` (401, 403); check constraint. Empty until asked. |
  | robots_txt | text | no | | The rules, while `robots_status` is `rules`. |
  | robots_fetched_at | timestamptz | no | | Asked again after 24 hours. |
  | tdmrep_status | varchar(20) | no | | `rules` (a valid `/.well-known/tdmrep.json`) or `none` (no file, a 4xx or no valid TDMRep); check constraint. Empty until asked. |
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
  | seed_source | varchar(100) | yes | | Seed source, e.g. `sources` for `app/seeds/sources.json`. Primary key with `item_key`. |
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
