"""Plattform-Einstellungen (requirements.md, "Settings"). Benutzer-Einstellungen follow with user management."""
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.services import (
    backup_service,
    extraction_service,
    platform_settings_service,
    seed_service,
    source_service,
    transfer_service,
)
from app.services.backup_service import InvalidBackup
from app.services.platform_settings_service import InvalidSetting
from app.services.transfer_service import InvalidImport
from app.web.forms import download, form_values, wants_json
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/settings/platform", tags=["settings"])

SAVED = "Gespeichert"
TRANSFER_SECTION = "/settings/platform#import-export"
BACKUP_SECTION = "/settings/platform#backup-dateien"
# A checkbox: the browser sends it only when it is ticked.
Checkbox = Annotated[str, Form()]
BackupNames = Annotated[list[str], Form(alias="backup")]


@router.get("")
def platform_settings(request: Request) -> Response:
    engine = request.app.state.engine
    context = {
        "settings": platform_settings_service.current(engine),
        "fields": platform_settings_service.FORM_FIELDS,
        "sources_seed": seed_service.sources_status(engine),
        "entities_seed": seed_service.entities_status(engine),
        "sources_without_region": len(source_service.list_sources(engine, region=source_service.NO_REGION)),
        "llm": extraction_service.llm_status(*request.app.state.llm),
        "backups": backup_service.stored_backups(request.app.state.backup_dir, engine.url.database),
        "backup_dir_exists": request.app.state.backup_dir.is_dir(),
        "max_import_mb": transfer_service.MAX_FILE_BYTES // (1024 * 1024),
    }
    return templates.TemplateResponse(request, "settings_platform.html", context)


@router.post("")
def save_platform_settings(request: Request, form: Annotated[dict[str, str], Depends(form_values)]) -> Response:
    """static/app.js saves in place and gets JSON; without JavaScript the page is shown again with a message.

    Each section's form sends only its own settings.
    """
    try:
        platform_settings_service.save(request.app.state.engine, form)
    except InvalidSetting as error:
        if wants_json(request):
            # 200 like the login form shown again with an error (routes/auth.py): browsers log every
            # 4xx answer as a console error, and an invalid entry is no error of the page.
            return JSONResponse({"error": str(error)})
        flash(request, str(error), "error")
    else:
        if wants_json(request):
            return JSONResponse({"status": SAVED})
        flash(request, SAVED)
    return RedirectResponse("/settings/platform", status_code=303)


@router.get("/export")
def export_configuration(request: Request, einstellungen: str = "", quellen: str = "") -> Response:
    """A plain GET form: a download that changes nothing."""
    if not (einstellungen or quellen):
        flash(request, "Bitte Einstellungen, Quellen oder beides wählen.", "error")
        return RedirectResponse(TRANSFER_SECTION, status_code=303)
    return download(transfer_service.export(request.app.state.engine, bool(einstellungen), bool(quellen)))


@router.post("/import")
def import_configuration(
    request: Request, file: Annotated[UploadFile, File()], einstellungen: Checkbox = "", quellen: Checkbox = ""
) -> Response:
    if not file.filename:
        flash(request, "Bitte eine Exportdatei wählen.", "error")
        return RedirectResponse(TRANSFER_SECTION, status_code=303)
    # One byte beyond the limit is enough to refuse the file.
    content = file.file.read(transfer_service.MAX_FILE_BYTES + 1)
    try:
        result = transfer_service.import_file(request.app.state.engine, content, bool(einstellungen), bool(quellen))
    except InvalidImport as error:
        flash(request, f"Nichts importiert. {error}", "error")
        return RedirectResponse(TRANSFER_SECTION, status_code=303)
    parts = []
    if result.settings_taken:
        parts.append("Plattform-Einstellungen übernommen")
    if quellen:
        parts.append(f"{result.sources_imported} {'Quelle' if result.sources_imported == 1 else 'Quellen'} angelegt")
    text = f"Import: {', '.join(parts)}."
    if result.name_taken:
        flash(request, f"{text} Übersprungen, Name schon vergeben: {name_list(result.name_taken)}.", "error")
    else:
        flash(request, text)
    return RedirectResponse(TRANSFER_SECTION, status_code=303)


@router.post("/backups/create")
def create_backup(request: Request) -> Response:
    if backup_service.enqueue_now(request.app.state.engine) is None:
        flash(request, "Ein Backup ist schon eingereiht oder läuft.", "error")
    else:
        flash(request, "Backup eingereiht: der Worker legt es an, Status unter Jobs.")
    return RedirectResponse(BACKUP_SECTION, status_code=303)


@router.post("/backups/upload")
def upload_backup(request: Request, file: Annotated[UploadFile, File()]) -> Response:
    if not file.filename:
        flash(request, "Bitte eine Backup-Datei wählen.", "error")
        return RedirectResponse(BACKUP_SECTION, status_code=303)
    try:
        backup = backup_service.store_upload(
            request.app.state.backup_dir, request.app.state.engine.url.database, file.file
        )
    except InvalidBackup as error:
        flash(request, f"Nicht hochgeladen. {error}", "error")
    else:
        flash(request, f"Hochgeladen als {backup.name}.")
    return RedirectResponse(BACKUP_SECTION, status_code=303)


@router.post("/backups/delete")
def delete_backups(request: Request, names: BackupNames = []) -> Response:
    result = backup_service.delete_stored(request.app.state.backup_dir, request.app.state.engine.url.database, names)
    if result.blocked:
        flash(request, f"Nichts gelöscht, blockiert: {name_list(result.blocked)}.", "error")
    elif not result.processed:
        flash(request, "Keine Backups ausgewählt.", "error")
    else:
        flash(request, f"{result.processed} {'Backup' if result.processed == 1 else 'Backups'} gelöscht.")
    return RedirectResponse(BACKUP_SECTION, status_code=303)


@router.get("/backups/{name}")
def download_backup(request: Request, name: str) -> Response:
    path = backup_service.stored_path(request.app.state.backup_dir, request.app.state.engine.url.database, name)
    if path is None:
        flash(request, "Backup nicht gefunden.", "error")
        return RedirectResponse(BACKUP_SECTION, status_code=303)
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.post("/seed/sources")
def import_source_catalogue(request: Request) -> Response:
    """The only way the starter catalogue gets in: an explicit admin action (architecture.md, "Seed Data")."""
    result = seed_service.import_sources(request.app.state.engine)
    text = f"Starterkatalog Quellen: {result.imported} importiert, {result.skipped} schon früher importiert."
    if result.name_taken:
        flash(request, f"{text} Nicht importiert, Name schon vergeben: {name_list(result.name_taken)}.", "error")
    else:
        flash(request, text)
    return RedirectResponse("/settings/platform#starterdaten", status_code=303)


@router.post("/seed/entities")
def import_entity_catalogue(request: Request) -> Response:
    """The sectors and commodities the entity recognition offers come in only through this admin action as well."""
    result = seed_service.import_entities(request.app.state.engine)
    text = f"Starterliste Branchen und Rohstoffe: {result.imported} importiert, {result.skipped} schon früher importiert."
    if result.name_taken:
        flash(request, f"{text} Nicht importiert, Schlüssel schon vergeben: {name_list(result.name_taken)}.", "error")
    else:
        flash(request, text)
    return RedirectResponse("/settings/platform#starterdaten", status_code=303)


@router.post("/seed/source-regions")
def take_catalogue_regions(request: Request) -> Response:
    """Regions for sources from before regions existed; an explicit admin action like the import."""
    given = seed_service.take_catalogue_regions(request.app.state.engine)
    flash(request, f"{given} {'Quelle hat' if given == 1 else 'Quellen haben'} die Region ihres Katalogeintrags erhalten.")
    return RedirectResponse("/settings/platform#starterdaten", status_code=303)
