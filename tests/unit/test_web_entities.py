"""Entitäten pages in the web layer, without a database: the services are replaced."""
import pytest
from fastapi.testclient import TestClient

from app.services import entity_correction_service, entity_service, event_correction_service, event_service, seed_service
from app.services.bulk import BulkResult
from app.services.entity_correction_service import SimilarEntity, SplitResult
from app.services.entity_service import AliasView, EntityDetail, EntityListItem, EntityOverview, EntityRef, EventEntity, MentionView
from app.services.event_service import EventDetail
from app.services.seed_service import SeedResult
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of
from tests.unit.test_web_events import event


def entity(entity_id, **fields):
    values = dict(id=entity_id, kind="person", name=f"Volodymyr <{entity_id}>", alias_count=2, uncertain_count=0,
                  report_count=5, event_count=4)
    return EntityListItem(**values | fields)


def alias(alias_id, **fields):
    values = dict(id=alias_id, scheme="name", value=f"Form {alias_id}", match="founding", similarity=None, uncertain=False,
                  confirmed_at=None, created_at=NOW, mention_count=1)
    return AliasView(**values | fields)


def mention(mention_id, **fields):
    values = dict(id=mention_id, source="actor", detail={"role": "President of Ukraine", "quote": "said the president"},
                  alias_value="Vladimir Zelensky", uncertain=False, candidate_id=3, report_title="Report", event_id=8,
                  event_title="Event", article_id=30, moment=NOW, source_display_name="Ukrinform")
    return MentionView(**values | fields)


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def pages(monkeypatch):
    calls = {"overview": EntityOverview([entity(2, uncertain_count=1), entity(1, kind="company", name="Siemens")], total=2, waiting=0)}

    def overview(_engine, kind, uncertain_only, search):
        calls["filters"] = (kind, uncertain_only, search)
        return calls["overview"]

    def correct(name):
        def run(_engine, entity_id, ids):
            calls[name] = (entity_id, ids)
            return calls.get(f"{name}_result", BulkResult(processed=len(ids), blocked=[]))
        return run

    monkeypatch.setattr(entity_service, "overview", overview)
    monkeypatch.setattr(entity_service, "detail", lambda _engine, entity_id: calls.get("detail"))
    monkeypatch.setattr(entity_correction_service, "similar_entities", lambda _engine, entity_id: calls.get("similar", []))
    for name in ("confirm", "split", "merge"):
        monkeypatch.setattr(entity_correction_service, name, correct(name))
    return calls


def test_the_list_shows_every_entity_with_its_kind_and_counts_linked_to_its_page(client, pages):
    html = client.get("/entities").text

    assert text_of(html, "entity-count").startswith("2 Entitäten.")
    assert tag_of(html, "entity-name-2") == '<a href="/entities/2" data-testid="entity-name-2">'
    assert text_of(html, "entity-name-2") == "Volodymyr &lt;2&gt;"
    assert "status-uncertain" in html.split('data-testid="entity-uncertain-2"')[1][:80]
    assert text_of(html, "entity-uncertain-1") == "–" and text_of(html, "entity-reports-1") == "5"
    assert 'aria-current="page"' in tag_of(html, "nav-entities")
    assert pages["filters"] == ("", False, "")


def test_the_list_filters_by_kind_uncertain_and_name_in_the_url(client, pages):
    pages["overview"] = EntityOverview([], total=0, waiting=3)

    html = client.get("/entities?kind=person&uncertain=1&q=zel").text

    assert pages["filters"] == ("person", True, "zel")
    assert 'value="person" selected' in html and "checked" in tag_of(html, "filter-uncertain") and 'value="zel"' in tag_of(html, "filter-q")
    assert text_of(html, "no-entities") == "Keine Entitäten für diese Auswahl."
    assert "3 Extraktionen warten auf die Entitätenerkennung." in text_of(html, "entity-count")


def test_more_entities_than_shown_says_so(client, pages):
    pages["overview"] = EntityOverview([entity(1)], total=250, waiting=0)

    assert text_of(client.get("/entities").text, "entity-count") == "Angezeigt: die ersten 1 von 250 Entitäten nach Name."


def test_the_entity_page_flags_an_uncertain_name_form_and_offers_to_confirm_or_split(client, pages):
    pages["detail"] = EntityDetail(
        entity(7, uncertain_count=1), [alias(1), alias(2, match="llm", similarity=0.857, uncertain=True, value="Vladimir Zelensky")],
        [mention(5, uncertain=True)], 1,
    )

    html = client.get("/entities/7").text

    assert tag_of(html, "alias-list").startswith('<form class="stack" method="post" action="/entities/7/confirm" data-bulk-form')
    assert "disabled" in tag_of(html, "confirm") and "formaction" not in tag_of(html, "confirm")
    split = tag_of(html, "split")
    assert 'formaction="/entities/7/split"' in split and 'data-confirm="Namensform|Namensformen"' in split and 'data-confirm-verb="abtrennen"' in split
    assert 'name="alias_id" value="2" data-select-row data-name="Vladimir Zelensky"' in tag_of(html, "select-alias-2")
    assert text_of(html, "alias-uncertain-2") == "unsicher" and tag_of(html, "alias-uncertain-1") is None
    assert "Ähnlicher Name, vom LLM als gleich beurteilt (Ähnlichkeit 0,86)" in html
    assert tag_of(html, "mention-report-5") == '<a href="/articles/30#events" data-testid="mention-report-5">'
    assert tag_of(html, "mention-event-5") == '<a href="/events/8" data-testid="mention-event-5">'
    assert "Akteur: President of Ukraine" in html and "„said the president“" in html
    assert [text_of(html, f"section-nav-{name}") for name in ("aliases", "mentions", "similar")] == ["Namensformen", "Berichte", "Ähnliche Entitäten"]


def test_the_entity_page_shows_its_parent_and_children(client, pages):
    pages["detail"] = EntityDetail(
        entity(7, kind="subsidiary", name="Google"), [alias(1)], [], 0,
        parent=EntityRef(3, "Alphabet", "company"), children=[EntityRef(9, "Gmail", "product")],
    )

    html = client.get("/entities/7").text

    assert "<th scope=\"row\">Mutterunternehmen</th>" in html and tag_of(html, "meta-parent") is not None
    assert '<a href="/entities/9">Gmail</a> (Produkt)' in html
    assert text_of(html, "no-mentions") == "Kein Bericht nennt diese Entität."


def test_a_merged_entity_leads_to_the_one_it_went_into_and_offers_nothing_to_correct(client, pages):
    pages["detail"] = EntityDetail(entity(7, alias_count=0), [], [], 0, merged_into=EntityRef(3, "Volodymyr Zelenskyy", "person"))

    html = client.get("/entities/7").text

    assert '<a href="/entities/3">Volodymyr Zelenskyy</a>' in html
    assert tag_of(html, "confirm") is None and tag_of(html, "similar-section") is None


def test_the_entity_page_lists_similar_entities_to_merge_into_it(client, pages):
    pages["detail"] = EntityDetail(entity(7), [alias(1)], [], 0)
    pages["similar"] = [SimilarEntity(entity(9, name="Vladimir Zelensky"), 0.31)]

    html = client.get("/entities/7").text

    assert tag_of(html, "similar-list").startswith('<form class="stack" method="post" action="/entities/7/merge" data-bulk-form')
    merge = tag_of(html, "merge")
    assert 'data-confirm="Entität|Entitäten"' in merge and 'data-confirm-verb="zusammenführen"' in merge
    assert 'name="entity_id" value="9" data-select-row data-name="Vladimir Zelensky"' in tag_of(html, "select-similar-9")
    assert text_of(html, "similar-score-9") == "0,31"


def test_an_unknown_entity_goes_back_to_the_list(client, pages):
    response = client.get("/entities/404")

    assert (response.status_code, response.headers["location"]) == (303, "/entities")


@pytest.mark.parametrize(("action", "form", "message"), [
    ("confirm", {"alias_id": ["2", "3"]}, "2 Namensformen bestätigt."),
    ("merge", {"entity_id": ["9"]}, "1 Entität in diese zusammengeführt."),
])
def test_a_correction_runs_on_the_selection_and_says_what_it_did(client, pages, action, form, message):
    response = client.post(f"/entities/7/{action}", data=form)

    assert response.status_code == 303 and response.headers["location"].startswith("/entities/7#")
    assert pages[action] == (7, [int(value) for value in next(iter(form.values()))])
    assert message in client.get("/entities").text


def test_a_split_names_the_new_entity_and_a_blocked_one_names_what_blocked_it(client, pages):
    pages["split_result"] = SplitResult(processed=1, blocked=[], entity_id=12)
    client.post("/entities/7/split", data={"alias_id": ["2"]})
    assert "1 Namensform als neue Entität 12 abgetrennt." in client.get("/entities").text

    pages["split_result"] = SplitResult(processed=0, blocked=["Rohöl (Starterliste)"])
    client.post("/entities/7/split", data={"alias_id": ["2"]})
    assert "Nichts abgetrennt, blockiert: Rohöl (Starterliste)." in client.get("/entities").text


def test_nothing_selected_is_said(client, pages):
    client.post("/entities/7/confirm")
    assert "Keine Namensformen ausgewählt." in client.get("/entities").text


def test_the_event_page_lists_the_entities_its_reports_name(client, monkeypatch):
    monkeypatch.setattr(event_service, "detail", lambda _engine, event_id: EventDetail(event(8)))
    monkeypatch.setattr(event_correction_service, "similar_events", lambda _engine, event_id: [])
    monkeypatch.setattr(entity_service, "of_event", lambda _engine, event_id: [
        EventEntity(3, "person", "Volodymyr Zelenskyy", 2, True, ["Vladimir Zelensky", "Volodymyr Zelenskyy"]),
        EventEntity(4, "country", "Ukraine", 1, False, ["Ukraine"]),
    ])

    html = client.get("/events/8").text

    assert tag_of(html, "entity-name-3") == '<a href="/entities/3" data-testid="entity-name-3">'
    assert text_of(html, "entity-forms-3") == "Vladimir Zelensky, Volodymyr Zelenskyy"
    assert text_of(html, "entity-uncertain-3") == "unsicher" and tag_of(html, "entity-uncertain-4") is None
    assert text_of(html, "section-nav-entities") == "Entitäten"


def test_the_starter_list_comes_in_only_by_the_admin_s_button(client, monkeypatch):
    monkeypatch.setattr(seed_service, "import_entities", lambda _engine: SeedResult(imported=52, skipped=0, name_taken=[]))

    response = client.post("/settings/platform/seed/entities")

    assert (response.status_code, response.headers["location"]) == (303, "/settings/platform#starterdaten")
