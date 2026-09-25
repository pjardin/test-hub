"""JSON endpoints behind the live parts of the UI: the dashboard's
"running now" panel, per-run live view, run/kill actions, rescan, recorder."""
import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import appconfig
from core.models import Batch, Group, Run, Schedule, Test
from core.services import recorder as recorder_mod
from core.services import remote_recorder
from core.services import runner as runner_mod
from core.services import stats
from core.services.syncer import sync_from_files, write_groups_file, write_sidecar


def _err(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


def _body(request) -> dict:
    """Parsed JSON body, always a dict. A client sending `[]` or `"x"` gets
    defaults rather than a 500 from .get() on a list."""
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _int_arg(payload, key, default=0):
    try:
        return max(0, int(payload.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def _runner():
    runner = runner_mod.get()
    if runner is None:
        return None
    return runner


NO_RUNNER_MSG = ("The runner is not active in this process. Start the hub with "
                 "'python manage.py serve' (not runserver).")


# ---------------------------------------------------------------------------
# live status (dashboard + nav badge poll this)
# ---------------------------------------------------------------------------

def health(request):
    """Liveness for a load balancer. Deliberately says NOTHING about tests,
    runs or configuration: it is the ONE endpoint reachable without the
    password gate, so it must not leak anything.
    """
    return JsonResponse({"ok": True,
                         "service": "pw-testhub",
                         "runner": runner_mod.get() is not None})


def status(request):
    # Load iterations are deliberately NOT listed individually: a load run at
    # 4 users would show 4 churning anonymous rows and drown whatever else is
    # running. It is ONE activity, and it gets one row (below) with its own
    # progress.
    active_runs = list(Run.objects.filter(status__in=Run.ACTIVE_STATUSES,
                                          load_run__isnull=True)
                       .select_related("test", "batch", "batch__group", "batch__schedule")
                       .order_by("queued_at", "pk"))
    estimates = {}
    runs_payload = []
    for run in active_runs:
        test = run.test
        if test.pk not in estimates:
            estimates[test.pk] = stats.estimate_seconds(test)
        eta = stats.eta_for_run(run, estimates[test.pk])
        elapsed = run.elapsed_seconds
        runs_payload.append({
            "id": run.pk,
            "test_id": test.test_id,
            "name": test.display_name,
            "status": run.status,
            "batch_id": run.batch_id,
            "batch_label": run.batch.label if run.batch else "",
            "elapsed": round(elapsed, 1) if elapsed is not None else None,
            "eta": round(eta, 1) if eta is not None else None,  # null = not enough data
        })

    # group runs in progress, with when their last test should be done (the
    # runner's own queue simulated over its worker slots)
    ends = stats.queue_forecast(active_runs, appconfig.get_config().max_parallel, estimates)
    active_batches = stats.active_batches(active_runs, ends)

    batches_payload = []
    for batch in Batch.objects.order_by("-created_at")[:10]:
        counts = batch.runs_summary
        active = any(s in counts for s in Run.ACTIVE_STATUSES)
        batches_payload.append({
            "id": batch.pk, "label": batch.label, "trigger": batch.trigger,
            "counts": counts, "active": active,
            "created": timezone.localtime(batch.created_at).strftime("%H:%M:%S"),
        })

    from core.models import LoadRun
    load_payload = []
    for load_run in LoadRun.objects.filter(status__in=LoadRun.ACTIVE_STATUSES) \
                                   .select_related("test").order_by("id"):
        elapsed = load_run.elapsed_seconds or 0
        load_payload.append({
            "id": load_run.pk,
            "test_id": load_run.test.test_id,
            "label": load_run.label,
            "concurrency": load_run.concurrency,
            "in_flight": load_run.iterations.filter(status=Run.RUNNING).count(),
            "completed": load_run.iterations.exclude(
                duration_seconds=None).count(),
            "projected": load_run.projected_iterations,
            "remaining": max(0.0, load_run.duration_seconds - elapsed),
        })

    return JsonResponse({
        "ok": True,
        "runner_active": runner_mod.get() is not None,
        "runs": runs_payload,
        "active_batches": active_batches,
        "load_runs": load_payload,
        "recent_batches": batches_payload,
        "ts": timezone.localtime(timezone.now()).strftime("%H:%M:%S"),
    })


# ---------------------------------------------------------------------------
# starting runs
# ---------------------------------------------------------------------------

@require_POST
def run_tests(request):
    runner = _runner()
    if runner is None:
        return _err(NO_RUNNER_MSG, status=409)
    payload = _body(request)
    ids = payload.get("test_ids") or []
    tests = list(Test.objects.filter(test_id__in=ids, archived=False, enabled=True))
    if not tests:
        return _err("no enabled tests matched that selection")
    trigger = "manual" if len(tests) == 1 else "selection"
    batch = runner.enqueue_tests(tests, trigger=trigger)
    return JsonResponse({"ok": True, "batch_id": batch.pk,
                         "queued": [t.test_id for t in tests]})


@require_POST
def run_group(request, group_id):
    runner = _runner()
    if runner is None:
        return _err(NO_RUNNER_MSG, status=409)
    group = get_object_or_404(Group, pk=group_id)
    tests = list(group.tests.filter(enabled=True, archived=False))
    if not tests:
        return _err(f"group {group.name!r} has no enabled tests")
    batch = runner.enqueue_tests(tests, trigger="group", group=group)
    return JsonResponse({"ok": True, "batch_id": batch.pk, "queued": len(tests)})


# ---------------------------------------------------------------------------
# per-run live view
# ---------------------------------------------------------------------------

def run_live(request, run_id):
    run = get_object_or_404(Run.objects.select_related("test"), pk=run_id)
    cfg = appconfig.get_config()
    payload = {
        "ok": True,
        "status": run.status,
        "active": run.is_active,
        "elapsed": round(run.elapsed_seconds, 1) if run.elapsed_seconds is not None else None,
        "eta": None,
        "log_tail": "",
        "live_image": None,
        "error": run.error_message[:2000],
        "duration": run.duration_seconds,
    }
    if run.is_active:
        eta = stats.eta_for_run(run)
        payload["eta"] = round(eta, 1) if eta is not None else None
    if run.artifacts_rel:
        base = cfg.results_dir / run.artifacts_rel
        log_path = base / "run.log"
        if log_path.exists():
            try:
                # Seek, don't slurp: this is polled about once a second by
                # every open run page, and a chatty 30-minute test writes a
                # log far bigger than the 6 KB tail anyone reads.
                with open(log_path, "rb") as fh:
                    fh.seek(0, 2)
                    fh.seek(max(0, fh.tell() - 6000))
                    payload["log_tail"] = fh.read().decode("utf-8", "replace")
            except OSError:
                pass
        live = base / "live.jpg"
        if run.is_active and live.exists():
            from django.urls import reverse
            payload["live_image"] = (
                reverse("artifact", args=[f"{run.artifacts_rel}/live.jpg"])
                + f"?t={int(live.stat().st_mtime * 1000)}")
    return JsonResponse(payload)


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------

@require_POST
def kill_run(request, run_id):
    if runner_mod.kill_run(run_id):
        return JsonResponse({"ok": True})
    return _err("run is not active (already finished?)", status=409)


@require_POST
def kill_batch(request, batch_id):
    get_object_or_404(Batch, pk=batch_id)
    n = runner_mod.kill_batch(batch_id)
    return JsonResponse({"ok": True, "killed": n})


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------

@require_POST
def set_clock(request):
    """Set the machine's clock to the time the tester's browser supplied.
    Air-gapped boxes have no NTP; a wrong clock reads as a broken hub."""
    from core.services import systemclock
    value = _body(request).get("datetime", "")
    try:
        stamp = systemclock.set_system_time(value)
    except systemclock.ClockError as exc:
        return _err(str(exc), status=400)
    except Exception as exc:                       # noqa: BLE001 — show something human
        return _err(f"could not set the clock: {exc}", status=500)
    return JsonResponse({"ok": True, "set_to": stamp})


@require_POST
def rescan(request):
    summary = sync_from_files()
    return JsonResponse({"ok": True, "summary": {
        "created": summary["created"], "updated": len(summary["updated"]),
        "archived": summary["archived"], "restored": summary["restored"],
        "groups": summary["groups"], "errors": summary["errors"],
    }})


@require_POST
def schedule_toggle(request, schedule_id):
    sched = get_object_or_404(Schedule, pk=schedule_id)
    sched.enabled = not sched.enabled
    sched.save(update_fields=["enabled"])
    if sched.test_id:
        write_sidecar(sched.test)
    else:
        write_groups_file()
    return JsonResponse({"ok": True, "enabled": sched.enabled})


@require_POST
def schedule_delete(request, schedule_id):
    sched = get_object_or_404(Schedule, pk=schedule_id)
    test, group = sched.test, sched.group
    sched.delete()
    if test:
        write_sidecar(test)
    if group:
        write_groups_file()
    return JsonResponse({"ok": True})


def doctor(request):
    from core.services import diagnostics
    checks = diagnostics.run_checks(quick=bool(request.GET.get("quick")))
    return JsonResponse({"ok": True, "checks": checks,
                         "summary": diagnostics.summary(checks),
                         "version": diagnostics.app_version()})


@require_POST
def target_check(request):
    """Can THIS hub machine reach the target URL? First thing to verify on
    a new deployment. Uses stdlib urllib so it works offline/air-gapped."""
    import urllib.error
    import urllib.request
    cfg = appconfig.get_config()
    url = cfg.target_url
    started = timezone.now()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pw-testhub-check"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            ms = (timezone.now() - started).total_seconds() * 1000
            return JsonResponse({"ok": True, "status": resp.status,
                                 "ms": round(ms), "url": url})
    except urllib.error.HTTPError as exc:
        ms = (timezone.now() - started).total_seconds() * 1000
        # an HTTP error still proves the target is REACHABLE
        return JsonResponse({"ok": True, "status": exc.code, "ms": round(ms),
                             "url": url, "note": "reachable (HTTP error status)"})
    except Exception as exc:
        return JsonResponse({"ok": False, "url": url,
                             "error": f"{type(exc).__name__}: {exc}"}, status=200)


@require_POST
def delete_run_data(request, run_id):
    """Delete one run: its artifacts (default -- charts survive) or the whole
    history row."""
    from core.services.housekeeping import delete_run, delete_run_artifacts
    run = get_object_or_404(Run, pk=run_id)
    if run.is_active:
        return _err("that run is still active -- kill it first", status=409)
    payload = _body(request)
    whole = bool(payload.get("whole_run"))
    freed = delete_run(run) if whole else delete_run_artifacts(run)
    return JsonResponse({"ok": True, "freed_bytes": freed, "whole_run": whole})


@require_POST
def purge_test_data(request, test_id):
    """Clear a test's run data, optionally keeping the newest N runs."""
    from core.services.housekeeping import purge_test
    test = get_object_or_404(Test, test_id=test_id)
    payload = _body(request)
    result = purge_test(test,
                        artifacts_only=not payload.get("whole_runs"),
                        keep_last=_int_arg(payload, "keep_last"))
    return JsonResponse({"ok": True, **result})


@require_POST
def purge_all_data(request):
    """Bulk cleanup, optionally limited to one status (e.g. only 'passed')."""
    from core.services.housekeeping import purge_all
    payload = _body(request)
    status_filter = payload.get("status") or None
    if status_filter and status_filter not in dict(Run.STATUS_CHOICES):
        return _err("unknown status filter")
    result = purge_all(artifacts_only=not payload.get("whole_runs"),
                       keep_last_per_test=_int_arg(payload, "keep_last"),
                       status=status_filter)
    return JsonResponse({"ok": True, **result})


@require_POST
def storage_check(request):
    from core.services import storage
    return JsonResponse(storage.check())


@require_POST
def storage_offload(request):
    from core.services import storage
    return JsonResponse({"ok": True, **storage.offload_backlog()})


@require_POST
def prune_results_api(request):
    from core.services.housekeeping import prune_results
    days = max(1, _int_arg(_body(request), "days", 30) or 30)
    result = prune_results(days)
    return JsonResponse({"ok": True, **result})


# ---------------------------------------------------------------------------
# recorder (playwright codegen)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# remote recorder (record through the hub, no display needed anywhere)
# ---------------------------------------------------------------------------

@require_POST
def remote_rec_start(request):
    payload = _body(request)
    try:
        result = remote_recorder.start(payload.get("url") or "",
                                       test_id=str(payload.get("test_id") or ""),
                                       start_path=str(payload.get("start") or ""))
    except ValueError as exc:
        return _err(str(exc))
    return JsonResponse(result, status=200 if result.get("ok") else 409)


@require_POST
def remote_rec_apply(request):
    """Write a finished recording into an EXISTING test's file: append the
    recorded steps to run()'s body, or replace the file with the generated
    code. Returns where the recorder tab should navigate next."""
    from core.services.remote_recorder import (append_steps_to_code,
                                               append_steps_to_code_ts,
                                               build_code, build_code_ts,
                                               take_pending)
    from core.services.syncer import read_test_code, write_test_code
    payload = _body(request)
    mode = payload.get("mode")
    if mode not in ("append", "replace"):
        return _err("mode must be 'append' or 'replace'")
    pending = take_pending()
    if not pending:
        return _err("no finished recording to apply", status=409)
    test_id = pending.get("test_id")
    if not test_id:
        return _err("this recording was not started from an existing test", status=409)
    test = get_object_or_404(Test, test_id=test_id, archived=False)
    # the target test's language decides which emitter writes the file --
    # a recording is language-neutral until this moment
    is_ts = test.test_type == Test.TYPE_TS
    if mode == "append":
        start_path = pending.get("start_path", "")
        if is_ts:
            new_code = append_steps_to_code_ts(read_test_code(test), pending["steps"],
                                               start_path)
        else:
            new_code = append_steps_to_code(read_test_code(test), pending["steps"],
                                            start_path)
            if new_code is None:
                return _err("could not find run() in the existing file -- "
                            "use Copy code and merge by hand", status=409)
    else:
        cfg = appconfig.get_config()
        new_code = (build_code_ts if is_ts else build_code)(
            pending["steps"], cfg.target_url, pending.get("start_path", ""))
    write_test_code(test, new_code)
    from django.urls import reverse
    return JsonResponse({"ok": True,
                         "redirect": reverse("test_edit", args=[test.test_id])})


def remote_rec_status(request):
    return JsonResponse(remote_recorder.status())


def remote_rec_frame(request):
    session = remote_recorder.current()
    if session is None:
        return HttpResponse(status=204)
    since = request.GET.get("seq", "")
    frame, seq = session.get_frame()
    if not frame or (since and since == str(seq)):
        return HttpResponse(status=204)
    response = HttpResponse(frame, content_type="image/jpeg")
    response["X-Seq"] = str(seq)
    response["Cache-Control"] = "no-store, max-age=0"
    return response


@require_POST
def remote_rec_input(request):
    session = remote_recorder.current()
    if session is None or session.state not in ("starting", "recording"):
        return _err("no active recording session", status=409)
    try:
        event = _body(request)
    except ValueError:
        return _err("bad JSON body")
    session.send_input(event)
    return JsonResponse({"ok": True})


@require_POST
def remote_rec_inspect(request):
    """Right-click in the recorder: what element is at (x, y)? The session
    thread answers asynchronously; the client watches inspect_seq in status."""
    session = remote_recorder.current()
    if session is None or session.state not in ("starting", "recording"):
        return _err("no active recording session", status=409)
    try:
        event = _body(request)
        session.request_inspect(float(event.get("x", 0)), float(event.get("y", 0)))
    except (ValueError, TypeError):
        return _err("bad coordinates")
    return JsonResponse({"ok": True})


@require_POST
def remote_rec_add_step(request):
    session = remote_recorder.current()
    if session is None or session.state not in ("starting", "recording"):
        return _err("no active recording session", status=409)
    try:
        step = _body(request)
    except ValueError:
        return _err("bad JSON body")
    if not session.add_manual_step(step):
        return _err("unknown step action")
    return JsonResponse({"ok": True})


@require_POST
def remote_rec_stop(request):
    return JsonResponse(remote_recorder.stop())


@require_POST
def remote_rec_clear(request):
    remote_recorder.clear()
    return JsonResponse({"ok": True})


@require_POST
def recorder_start(request):
    payload = _body(request)
    result = recorder_mod.start(payload.get("url") or "")
    return JsonResponse(result, status=200 if result.get("ok") else 409)


def recorder_status(request):
    return JsonResponse(recorder_mod.status())


@require_POST
def recorder_stop(request):
    return JsonResponse(recorder_mod.stop())


# ---------------------------------------------------------------------------
# load / performance testing
# ---------------------------------------------------------------------------

@require_POST
def load_preview(request):
    """What would this plan actually do? Answered before anything starts."""
    from core.services import loadtest
    payload = _body(request)
    test = get_object_or_404(Test, test_id=payload.get("test_id", ""))
    return JsonResponse({"ok": True, **loadtest.plan_preview(
        test,
        _int_arg(payload, "duration_seconds", 600) or 600,
        _int_arg(payload, "concurrency", 3) or 1,
        _int_arg(payload, "think_time_seconds", 0),
        _int_arg(payload, "max_iterations", 0))})


@require_POST
def load_start(request):
    from core.services import loadtest
    payload = _body(request)
    test = get_object_or_404(Test, test_id=payload.get("test_id", ""),
                             archived=False)
    try:
        load_run = loadtest.start_load_run(
            test,
            duration_seconds=_int_arg(payload, "duration_seconds", 600) or 600,
            concurrency=_int_arg(payload, "concurrency", 3) or 1,
            ramp_seconds=_int_arg(payload, "ramp_seconds", 0),
            think_time=_int_arg(payload, "think_time_seconds", 0),
            max_iterations=_int_arg(payload, "max_iterations", 0),
            keep_artifacts=bool(payload.get("keep_artifacts")),
            label=str(payload.get("label") or "")[:300])
    except ValueError as exc:
        return _err(str(exc))
    return JsonResponse({"ok": True, "load_run_id": load_run.pk,
                         "projected": load_run.projected_iterations})


def load_live(request, load_run_id):
    from core.models import LoadRun
    from core.services import loadtest
    load_run = get_object_or_404(LoadRun, pk=load_run_id)
    return JsonResponse(loadtest.live_status(load_run))


@require_POST
def load_kill(request, load_run_id):
    from core.services import loadtest
    stopped = loadtest.kill_load_run(load_run_id)
    return JsonResponse({"ok": stopped,
                         "error": "" if stopped else "that load test is not running"})


@require_POST
def load_plan_delete(request, plan_id):
    from core.models import LoadPlan
    LoadPlan.objects.filter(pk=plan_id).delete()
    return JsonResponse({"ok": True})
