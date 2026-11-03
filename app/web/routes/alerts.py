"""Alerts: what the alert rules found, the newest first, read selected or all; and the rules themselves, each with
its watchlist, filter, triggers and throttle (alert_service)."""
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import alert_service, event_service
from app.services.alert_service import InvalidRule
from app.services.bulk import BulkResult
from app.web.forms import form_values
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/alerts", tags=["alerts"])

NOT_FOUND = "Alert-Regel nicht gefunden."
FormValues = Annotated[dict[str, str], Depends(form_values)]
AlertIds = Annotated[list[int], Form(alias="alert_id")]
RuleIds = Annotated[list[int], Form(alias="rule_id")]
Back = Annotated[str, Form()]


@router.get("")
def inbox(request: Request, show: str = "") -> Response:
    """Ungelesene by default, Alle by a plain link."""
    context = {"inbox": alert_service.inbox(request.app.state.engine, unread_only=show != "all")}
    return templates.TemplateResponse(request, "alerts.html", context)


@router.post("/read")
def mark_read(request: Request, alert_ids: AlertIds = [], back: Back = "") -> Response:
    result = alert_service.mark_read(request.app.state.engine, alert_ids)
    if result.blocked:
        flash(request, f"Nichts als gelesen markiert, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Alert' if result.processed == 1 else 'Alerts'} als gelesen markiert.")
    else:
        flash(request, "Keine Alerts ausgewählt.", "error")
    return RedirectResponse(_inbox(back), status_code=303)


@router.post("/read-all")
def mark_all_read(request: Request, back: Back = "") -> Response:
    count = alert_service.mark_all_read(request.app.state.engine)
    flash(request, f"{count} {'Alert' if count == 1 else 'Alerts'} als gelesen markiert." if count else "Keine ungelesenen Alerts.")
    return RedirectResponse(_inbox(back), status_code=303)


@router.get("/rules")
def rule_list(request: Request) -> Response:
    context = {"rules": alert_service.rules(request.app.state.engine)}
    return templates.TemplateResponse(request, "alert_rules.html", context)


@router.get("/rules/new")
def new_rule(request: Request) -> Response:
    """Empty, or with the filter of the event list it was asked from (Als Alert-Regel speichern)."""
    return _form(request, None, alert_service.new_rule(request.query_params))


@router.post("/rules")
def create_rule(request: Request, form: FormValues) -> Response:
    try:
        rule = alert_service.create(request.app.state.engine, form)
    except InvalidRule as error:
        flash(request, f"Nicht angelegt: {error}", "error")
        return RedirectResponse("/alerts/rules/new", status_code=303)
    flash(request, f"Alert-Regel {rule.name} angelegt.")
    return RedirectResponse("/alerts/rules", status_code=303)


@router.post("/rules/activate")
def activate(request: Request, rule_ids: RuleIds = []) -> Response:
    return _bulk(request, alert_service.set_active(request.app.state.engine, rule_ids, True), "aktiviert")


@router.post("/rules/deactivate")
def deactivate(request: Request, rule_ids: RuleIds = []) -> Response:
    return _bulk(request, alert_service.set_active(request.app.state.engine, rule_ids, False), "deaktiviert")


@router.post("/rules/delete")
def delete(request: Request, rule_ids: RuleIds = []) -> Response:
    return _bulk(request, alert_service.delete(request.app.state.engine, rule_ids), "gelöscht")


@router.get("/rules/{rule_id}")
def rule_page(request: Request, rule_id: int) -> Response:
    rule = alert_service.get(request.app.state.engine, rule_id)
    if rule is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/alerts/rules", status_code=303)
    return _form(request, rule, asdict(rule))


@router.post("/rules/{rule_id}")
def save_rule(request: Request, rule_id: int, form: FormValues) -> Response:
    try:
        rule = alert_service.update(request.app.state.engine, rule_id, form)
    except InvalidRule as error:
        flash(request, f"Nicht gespeichert: {error}", "error")
        return RedirectResponse(f"/alerts/rules/{rule_id}", status_code=303)
    if rule is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/alerts/rules", status_code=303)
    flash(request, "Gespeichert. Die Regel zählt ab jetzt.")
    return RedirectResponse("/alerts/rules", status_code=303)


def _form(request: Request, rule, values: dict) -> Response:
    """The filter's fields are the event list's, its watchlist among them."""
    watchlist = {"watchlist": str(values["watchlist_id"])} if values["watchlist_id"] else {}
    context = {
        "rule": rule,
        "values": values,
        "filter": event_service.parse_filter(values["criteria"] | watchlist, with_period=False),
        "choices": event_service.filter_choices(request.app.state.engine),
        "limits": {"max_per_hour": alert_service.MAX_PER_HOUR, "cooldown_hours": alert_service.COOLDOWN_HOURS},
    }
    return templates.TemplateResponse(request, "alert_rule.html", context)


def _bulk(request: Request, result: BulkResult, verb: str) -> Response:
    if result.blocked:
        flash(request, f"Nichts {verb}, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Alert-Regel' if result.processed == 1 else 'Alert-Regeln'} {verb}.")
    else:
        flash(request, "Keine Alert-Regeln ausgewählt.", "error")
    return RedirectResponse("/alerts/rules", status_code=303)


def _inbox(back: str) -> str:
    return "/alerts?show=all" if back == "all" else "/alerts"
