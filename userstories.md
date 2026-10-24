# User Stories

Status flow: Backlog -> Planned -> Implemented. When moving a story, keep its epic heading (create it in the target section if missing). Skill `neues-feature` moves stories through these sections.

## Priorities

Epics are worked on in four phases; acceptance criteria per epic are in `requirements.md`, "Acceptance Criteria per Epic".

1. Phase A - prove the data flow: Projektfundament und Laufzeitumgebung; Quellenadapter und Ingestion (one or two sources); Normalisierung, Deduplizierung und Clustering; Strukturierte Ereignisextraktion; Ereignisidentität und zeitliche Entwicklung.
2. Phase B - build persistent knowledge: Entitäten und Wissensmodell; Verdichtung und Daten-Lifecycle; Dashboard, Recherche und Alerts (a minimal research dashboard first); Live-Kurse und Kursdatenspeicher (as early as possible, because high-resolution price history exists only from the day collection starts).
3. Phase C - assess market impact: Markt- und Wirkungsmodell; Historische Markt- und Ereignisdaten; Prognoseprotokoll und Evaluation.
4. Phase D - operations and scaling: Skalierung und Betriebsqualität; extending sources, languages, history and models; regular evaluation and revision of the event model.

The first sprint does not build any comprehensive stock forecasting yet. It proves the whole data flow:
1. Ingest a limited selection of news sources.
2. Normalise articles and detect duplicates.
3. Create structured event candidates from articles.
4. Merge articles into events.
5. Store event changes with timestamps and source evidence.
6. Build a simple dashboard for event details.
7. Archive or delete raw data in a controlled way after successful processing.
8. Measure quality, throughput, storage needs and error rates.

## Backlog

### Epic: Architecture
1. Modularize the code of the app --> Efficiency in AI
2.

### Epic: Admin / Settings UX
1. As an admin, I want a settings page with multiple independent sections to show a quick-navigation row and collapsible sections, so I can jump straight to and focus on just the part I need.
2. As an admin, I want to remove several list items at once (a checkbox per item plus one bulk action) instead of clicking a separate delete button per item.
3. As an admin, mass import and export of settings and selected or all data, so I can quickly set up a new instance or back up an existing one.

### Epic: Quellenadapter und Ingestion
10. As an analyst, I want the ReliefWeb API as a source type, so humanitarian and conflict reports come in through the route ReliefWeb offers for automated access, since its robots.txt excludes the RSS feeds.
11. As an analyst, I want the EIA Open Data API as a source of energy data releases (e.g. the weekly petroleum and natural gas storage reports), with its API key from the environment, since EIA's robots.txt excludes its RSS feeds.

### Epic: Strukturierte Ereignisextraktion

### Epic: Ereignisidentität und zeitliche Entwicklung
1. As an analyst, I want each event candidate assigned to an existing event (Ereignis) or turned into a new event with a stable event ID, so several reports about the same event form one consistent event record.
2. As an analyst, I want every change to an event's state (start, update, escalation, closure, resumption) stored as a timestamped version with its source evidence, and contradicting reports kept side by side instead of merged into one fact, so the development of an event is traceable.
3. As an analyst, I want to merge and split wrongly grouped events, with every merge and split recorded in an audit trail, so grouping errors can be corrected.

### Epic: Entitäten und Wissensmodell
1. As an analyst, I want companies, subsidiaries, persons, countries, authorities, products, commodities and sectors recognised as entities, with different name forms and identifiers resolved to one entity and uncertain matches flagged, so events are linked to the right actors.
2. As an analyst, I want relationships between entities (supplier, customer, competitor, regulator, commodity dependency) stored with source, validity period and quality flag, and their historical changes kept, so the knowledge model stays traceable over time.

### Epic: Verdichtung und Daten-Lifecycle
1. As an admin, I want raw article texts deleted according to their source's retention period, but only after processing and validation have succeeded, so storage stays bounded while events, evidence quotes and source references remain understandable and verifiable.
2. As an admin, I want storage usage monitored and selected articles reprocessed after a schema or model change, so the knowledge base can evolve without re-running the whole history.

### Epic: Live-Kurse und Kursdatenspeicher
1. As an analyst, I want prices of stocks, ETFs and indices (Wertpapiere) as close to real time as the licensed source allows, each shown with its timestamp, source, exchange, currency and a real-time or delayed label, so I can follow the market reaction to an event while it unfolds.
2. As an admin, I want to mark Wertpapiere as tracked (verfolgt) so their prices (Kurspunkte) are collected continuously at the highest resolution the source offers and kept at full resolution for a configurable retention period (Plattform-Einstellung; at least 3 months by default, longer or unlimited when the measured storage need allows it) before being compressed into coarser intervals instead of deleted, so I have high-resolution price history for later event studies.

### Epic: Markt- und Wirkungsmodell
1. As an analyst, I want impact hypotheses (Wirkungsannahmen) that link events to companies, sectors and assets - direct or indirect; positive, negative or neutral; short-, medium- or long-term - each naming mechanism, affected entities, horizon, evidence and counterarguments, and classified as evidenced, plausible or speculative, so I can judge possible market effects.
2. As an analyst, I want direction and probability assessed separately, insufficient data to lead to low confidence or an explicit abstention, and already expected or priced-in events taken into account where data exists, so hypotheses never overstate their certainty.

### Epic: Historische Markt- und Ereignisdaten
1. As an analyst, I want licensed historical price, volume, benchmark and company data in a point-in-time model that handles splits, dividends, delistings and identity changes, so an analysis can reconstruct what was known on a given date and later information never leaks into earlier forecasts.
2. As an analyst, I want event-study windows for different horizons and separate training, validation and test periods, with data gaps and adjustments documented, so market reactions to events can be measured against a market or sector benchmark.

### Epic: Prognoseprotokoll und Evaluation
1. As an analyst, I want every forecast (Prognose) logged immutably before its outcome is known - with creation time, data state, model version and a target metric such as the return relative to a sector index after 5, 20 or 60 trading days - so forecasts and actual outcomes are stored separately and cannot be adjusted afterwards.
2. As an analyst, I want reproducible backtests with transaction costs, realistic execution assumptions and simple baselines, measuring hit rate, calibration, return, drawdown and stability across market phases and flagging data leakage and multiple testing, so a model is never judged on in-sample results alone and paper trading precedes any real trading.

### Epic: Dashboard, Recherche und Alerts
1. As an analyst, I want an overview of new and developing events and an event detail page with chronology, sources and entities, so I can follow what is happening and trace every statement back to its sources.
2. As an analyst, I want to filter events by country, sector, company, event type, relevance and time horizon, including new events and material changes, so I find what matters to me.
3. As an analyst, I want watchlists and configurable, throttled rule-based alerts, so I learn about relevant new events and material changes without checking manually.
4. As an analyst, I want historical search and similar past events, with uncertainty and contradicting evidence shown instead of hidden behind a single score, so I can put current events into context.

### Epic: Skalierung und Betriebsqualität
1. As an admin, I want a status page with articles per hour, cost per article, processing latency, error rate, LLM calls, job queue length and storage usage, so hardware and source decisions are based on measured values.
2. As an admin, I want separate worker pools with live news prioritised over historical backfill, plus batching, caching and limited parallelism, so historical processing never slows down live ingestion uncontrolled and workers scale independently.
3. As an admin, I want regular load, backup and recovery tests with documented results, so capacity is expanded only on the basis of measurements.

## Planned


## Implemented

### Epic: Projektfundament und Laufzeitumgebung
1. As a developer, I want a devcontainer and a Docker Compose stack with separate `api`, `worker` and `db` (PostgreSQL) services - configured only through environment variables, with health checks, structured logs and Alembic migrations - so the system starts reproducibly on the AI-Station and Claude Code is still available after every container rebuild.
2. As a developer, I want the project in a Git repository with a remote and a `.gitignore` that keeps `.env`, local settings and other secrets out, so every change is versioned and the CI/CD pipeline (skill `deploy-setup`) can build on it.
3. As an admin, I want to log in to the dashboard with the admin credentials from the env file, so the platform is not openly accessible.
4. As an admin, I want a PostgreSQL-based job queue with leases, retries and a visible job status, plus a configurable limit for parallel LLM calls, so a restart never leaves jobs lost or stuck, a retried job has no duplicate effect, and the GPU is not overloaded.
5. As an admin, I want scripted database backups and a documented restore that has been tested once, so data can be recovered after a failure.

### Epic: Quellenadapter und Ingestion
1. As an admin, I want to manage the news sources (Quellen) - name, type, URL, active flag, fetch interval, language, terms-of-use note and raw-text retention period - and to import a starter catalogue of open business and central-bank feeds only when I trigger it, so I control what is ingested and under which rules.
2. As an analyst, I want a uniform source adapter interface with RSS as the first adapter, storing source name, URL, publication time, fetch time and language separately, so new source types can be added without changing the event model.
3. As an analyst, I want the full text of every new article fetched from its URL - respecting robots.txt and a per-domain rate limit - and kept in a temporary raw-data layer, so the event extraction works on complete texts.
4. As an admin, I want failed fetches logged and retried with backoff, rate limits per source, and a status per source (last success, last error, consecutive failures), so temporary source outages cause no data loss.
5. As an analyst, I want a GDELT DOC 2.0 API adapter with configurable queries as the second source, so ingestion covers global events beyond the configured feeds.
6. As an admin, I want historical backfill to run separately from live ingestion, so a backfill never delays current news.
7. As an admin, I want full-text fetches that a host's bot protection blocks (e.g. the BFS article pages on www.admin.ch answer HTTP 403 to the Seismo User-Agent) recognised per source and resolved through an allowed route - a contact in the User-Agent, an official API or feed with the full text, or the publisher's permission - never by disguising the fetcher, so such sources still deliver complete texts.
8. As an analyst, I want every Quelle to carry its region - the continent it reports from, or worldwide - and the source list sortable and filterable by it, with the starter catalogue bringing the region of each source, so coverage stays balanced across continents and can be viewed per continent.
9. As an admin, I want full texts fetched only where the publisher allows text and data mining - honouring a machine-readable reservation (TDMRep: the `tdm-reservation` header or meta tag, `/.well-known/tdmrep.json`) and a list of hosts whose terms forbid it, starting with those found for the starter catalogue (BBC, France 24, The Guardian, SRF, tagesschau, SCMP, Times of India, CNBC, FreightWaves, The Loadstar, OilPrice, Rigzone, Defense News) - and otherwise keeping only the article's metadata, so articles found through GDELT respect their publishers' terms as well.

### Epic: Normalisierung, Deduplizierung und Clustering
1. As an analyst, I want article HTML cleaned to plain text and the metadata normalised (canonical URL, title, UTC timestamps, detected language), so all later steps work on consistent data.
2. As an analyst, I want identical articles recognised by canonical URL and content hash, so repeated fetches never create duplicate articles.
3. As an analyst, I want near-duplicate and syndicated articles linked to each other without blindly merging different events, and wrong links correctable, so the same report from several outlets is recognised and every article stays traceable to its source.

### Epic: Strukturierte Ereignisextraktion
1. As an analyst, I want a versioned event schema and an LLM extraction via Ollama - for articles that pass a configurable relevance pre-filter - that turns an article into event candidates with event type, actors, location, time, status and impacts, each statement labelled as fact, claim, forecast or interpretation and backed by a text quote, so events are structured and evidence-based.
2. As an analyst, I want the LLM output validated against the JSON schema, invalid output retried and logged, missing details marked as unknown instead of invented, separate confidence values for extraction, source quality and inference, and the model version, schema version and extraction time stored, so every extracted statement is traceable.
3. As an admin, I want the worker's stop grace period (`stop_grace_period` in `docker-compose.yml`) to be longer than the longest LLM call, so a deploy or restart lets a running extraction finish instead of killing it and repeating the call once its lease has expired.
4. As an admin, I want LLM jobs to wait while Ollama is unreachable instead of using up their attempts, so a switched-off or restarting LLM machine delays the extraction but never makes jobs fail.

### Epic: Data Integrity
1. As an admin, I want any starter/seed content to be imported only when I explicitly ask for it, and never duplicated or resurrected just because I renamed or deleted it afterwards.
