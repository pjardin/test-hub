"""The demo's JSON endpoints: city autocomplete, job progress, notes.

Same release rules as the pages (headers, speed, bugs). Signed-in-only
endpoints answer 401 JSON rather than redirecting -- a fetch() that follows
a redirect to an HTML sign-in page is how front ends end up parsing HTML as
JSON.
"""
import datetime as dt
import functools
import json

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from freight import catalog, docks, fleet, jobs, mail
from freight.web import (current_user, freight_view, next_note_fails, no_store,
                         release_of)

MAX_NOTE = 500


def _signed_in(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        user = current_user(request)
        if user is None:
            return JsonResponse({"error": "Sign in to the staff portal first"}, status=401)
        request.freight_user = user
        return no_store(view(request, *args, **kwargs))
    return wrapper


@freight_view
@require_GET
def version(request):
    release = release_of(request)
    return JsonResponse({"version": release.version, "name": release.name,
                         "theme": release.flags["theme"]})


@freight_view
@require_GET
def cities(request):
    hits = catalog.suggest(request.GET.get("q", ""), limit=8)
    return JsonResponse({"results": [
        {"label": catalog.label(c), "name": c.name, "country": c.country}
        for c in hits]})


@freight_view
@require_GET
@_signed_in
def job_status(request, job_id):
    job = jobs.get(job_id)
    if job is None:
        return JsonResponse({"error": "No such job (the server may have restarted)"},
                            status=404)

    def since(name):
        try:
            return max(0, int(request.GET.get(name, 0)))
        except ValueError:
            return 0
    return JsonResponse(job.snapshot(log_since=since("log"), series_since=since("series")))


@freight_view
@require_POST
@_signed_in
def job_cancel(request, job_id):
    job = jobs.get(job_id)
    if job is None:
        return JsonResponse({"error": "No such job"}, status=404)
    job.cancel()
    return JsonResponse({"ok": True, "state": job.state})


@freight_view
@require_POST
@_signed_in
def add_note(request, sid):
    shipment = catalog.shipment(sid)
    if shipment is None:
        return JsonResponse({"error": f"No shipment {sid}"}, status=404)
    try:
        text = str(json.loads(request.body or b"{}").get("text", "")).strip()
    except (ValueError, AttributeError):
        text = ""
    if not text:
        return JsonResponse({"error": "Write something first"}, status=400)
    if len(text) > MAX_NOTE:
        return JsonResponse({"error": f"Keep notes under {MAX_NOTE} characters"}, status=400)
    if next_note_fails(release_of(request)):
        # the 2.0.0 regression: a busy notes service, every third save
        return JsonResponse({"error": "The notes service is busy -- try again"},
                            status=503)
    note = {"text": text, "by": request.freight_user["name"],
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    changes = request.session.get("freight_changes", {})
    changes.setdefault(shipment.id, {}).setdefault("notes", []).append(note)
    request.session["freight_changes"] = changes
    return JsonResponse({"ok": True, "note": note}, status=201)


@freight_view
@require_POST
@_signed_in
def dock_action(request):
    """The drag-and-drop page's one endpoint: assign / unassign / auto / clear."""
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    action = body.get("action")
    if action == "assign":
        error = docks.assign(request.session, str(body.get("arrival", "")),
                             str(body.get("door", "")), str(body.get("slot", "")))
        if error:
            return JsonResponse({"ok": False, "error": error}, status=409)
    elif action == "unassign":
        docks.unassign(request.session, str(body.get("arrival", "")))
    elif action == "auto":
        docks.auto(request.session)
    elif action == "clear":
        docks.clear(request.session)
    else:
        return JsonResponse({"ok": False, "error": "unknown action"}, status=400)
    return JsonResponse({"ok": True, "assignments": docks.assignments(request.session)})


@freight_view
@require_GET
@_signed_in
def fleet_state(request, region):
    try:
        return JsonResponse(fleet.state(region))
    except KeyError:
        return JsonResponse({"error": f"no region {region!r}"}, status=404)


@freight_view
@require_GET
def mailbox_state(request):
    """What the Mailbox page polls: it reloads itself when newer mail lands."""
    msgs = mail.delivered(to=request.GET.get("to"))
    return no_store(JsonResponse({"count": len(msgs), "newest": msgs[0].id if msgs else 0}))
