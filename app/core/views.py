"""Page views. JSON endpoints live in api.py.

Forms are parsed by hand (a handful of fields each); every mutation that
touches test definitions immediately regenerates the files under tests/, so
the folder can always be copied to another machine without losing UI edits.
"""
import json
import re
import mimetypes
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie

from core import appconfig
from core.models import Batch, Group, LoadRun, Run, Schedule, Test
from core.services import recorder, stats
from core.services import runner as runner_mod
from core.services.humanize import humanize_code
from core.services.scheduler import next_slot
from core.services.syncer import (NEW_TEST_TEMPLATE, NEW_TS_TEST_TEMPLATE,
                                  TestFileError, archive_test, create_test,
                                  read_test_code, write_groups_file,
                                  write_sidecar, write_test_code)

PAGE_SIZE = 50
BROWSER_CHOICES = ["chromium", "chrome", "msedge", "system-chromium"]


# ---------------------------------------------------------------------------
# template context available everywhere
# ---------------------------------------------------------------------------

def site_context(request):
    cfg = appconfig.get_config()
    return {
        "cfg_target_name": cfg.target_name,
        "cfg_target_url": cfg.target_url,
        "cfg_target_version": cfg.target_version,
        "cfg_target_is_demo": cfg.target_is_demo,
        "cfg_public_url": cfg.public_url,
        "runner_active": runner_mod.get() is not None,
        # A load test is ONE activity, not one per virtual user.
        "nav_active_runs": (
            Run.objects.filter(status__in=Run.ACTIVE_STATUSES,
                               load_run__isnull=True).count()
            + LoadRun.objects.filter(status__in=LoadRun.ACTIVE_STATUSES).count()),
        "app_version": cfg.app_version,
        "auth_on": bool(cfg.password),
    }


def _runner_or_message(request):
    runner = runner_mod.get()
    if runner is None:
        messages.error(request, "The runner is not active in this process -- "
                                "start the hub with:  python manage.py serve")
    return runner


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

def dashboard(request):
    overview = stats.global_overview(days=7)
    recent_batches = (Batch.objects.prefetch_related("runs__test")
                      .order_by("-created_at")[:8])
    day_series = stats.runs_per_day(days=14)
    failures = stats.new_vs_chronic()
    return render(request, "core/dashboard.html", {
        "overview": overview,
        "new_failures": failures["new"],
        "recent_batches": recent_batches,
        "failing": stats.failing_now()[:8],
        "chart_days": day_series,
        "schedules_next": _upcoming_schedules(limit=5),
    })


def _upcoming_schedules(limit=5):
    cfg = appconfig.get_config()
    now = timezone.now()
    rows = []
    for sched in Schedule.objects.filter(enabled=True).select_related("test", "group"):
        due = next_slot(sched, now, cfg.timezone)
        if due:
            rows.append({"schedule": sched, "due": due})
    rows.sort(key=lambda r: r["due"])
    return rows[:limit]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def tests_list(request):
    query = request.GET.get("q", "").strip()
    tag = request.GET.get("tag", "").strip()
    tests = Test.objects.filter(archived=False)
    if query:
        tests = tests.filter(test_id__icontains=query) | \
                Test.objects.filter(archived=False, name__icontains=query)
        tests = tests.distinct()
    rows = []
    all_tags = set()
    for test in tests.prefetch_related("groups"):
        all_tags.update(test.tags)
        if tag and tag not in test.tags:
            continue
        last = test.runs.filter(load_run__isnull=True).order_by("-queued_at").first()
        s = stats.test_stats(test)
        rows.append({"test": test, "last": last, "stats": s,
                     "spark": stats.sparkline(test, points=16)})
    return render(request, "core/tests_list.html", {
        "rows": rows,
        "groups": Group.objects.all(),
        "all_tags": sorted(all_tags),
        "query": query,
        "tag": tag,
    })


def test_detail(request, test_id):
    test = get_object_or_404(Test, test_id=test_id)
    s = stats.test_stats(test)
    history = stats.history_series(test, limit=200)
    versions = stats.version_breakdown(test)

    # Load iterations are excluded here too: one load run would otherwise
    # bury the test's real history under a thousand rows. They are listed on
    # the load run's own page, and each still has its own run page.
    runs = test.runs.filter(load_run__isnull=True)
    status_f = request.GET.get("status", "")
    version_f = request.GET.get("version", "")
    if status_f:
        runs = runs.filter(status=status_f)
    if version_f:
        runs = runs.filter(target_version=version_f)
    page = Paginator(runs.select_related("batch"), PAGE_SIZE) \
        .get_page(request.GET.get("page"))

    return render(request, "core/test_detail.html", {
        "test": test,
        "stats": s,
        "strip": history[-60:],
        "chart_history": history,
        "chart_versions": versions,
        "page": page,
        "status_filter": status_f,
        "version_filter": version_f,
        "status_choices": [s0 for s0, _ in Run.STATUS_CHOICES],
        "version_choices": sorted({v["version"] for v in versions}),
        "chart_timings": stats.timing_trends(test),
        "chart_histogram": stats.duration_histogram(test),
        "err_clusters": stats.error_clusters(days=90, test=test, limit=5),
        "spark": stats.sparkline(test, points=30),
        "code": read_test_code(test),
        "plain_steps": humanize_code(read_test_code(test)),
    })


def test_new(request):
    if request.method == "POST":
        # The editor prefill is the PYTHON starter (client JS swaps it when
        # the language select changes). If the posted code is still the
        # pristine template of the OTHER language -- JS off, or a browser
        # that never fired the change -- discard it so create_test writes
        # the correct language's template instead of a mismatched file.
        code = request.POST.get("code", "").strip()
        lang = request.POST.get("language", "python")
        tpl_py = NEW_TEST_TEMPLATE.format(name="New test").strip()
        tpl_ts = NEW_TS_TEST_TEMPLATE.format(name="New test").strip()
        if (lang == "ts" and code == tpl_py) or (lang != "ts" and code == tpl_ts):
            code = ""
        try:
            test = create_test(
                request.POST.get("test_id", ""),
                request.POST.get("name", "").strip(),
                code=(code + "\n") if code else "",
                language=request.POST.get("language", "python"),
                description=request.POST.get("description", "").strip(),
                tags=[t.strip() for t in request.POST.get("tags", "").split(",") if t.strip()],
                version_tag=request.POST.get("version_tag", "").strip(),
                timeout_seconds=_int(request.POST.get("timeout_seconds"), 0),
                video_mode=_video_mode(request.POST.get("video_mode")),
                enabled=bool(request.POST.get("enabled")),
            )
        except TestFileError as exc:
            messages.error(request, str(exc))
            return render(request, "core/test_edit.html",
                          _edit_context(None, request.POST))
        test.groups.set(Group.objects.filter(pk__in=request.POST.getlist("groups")))
        write_sidecar(test)
        write_groups_file()
        messages.success(request, f"Created {test.test_id} "
                                  f"({test.file_path} + sidecar written)")
        return redirect("test_detail", test_id=test.test_id)
    ctx = _edit_context(None, None)
    copy_from = request.GET.get("copy", "").strip()
    if copy_from:
        src = Test.objects.filter(test_id=copy_from, archived=False).first()
        if src:
            ctx.update({
                "form_name": f"{src.name} (copy)",
                "form_description": src.description,
                "form_tags": ", ".join(src.tags),
                "form_version_tag": src.version_tag,
                "form_timeout": src.timeout_seconds,
                "code": read_test_code(src),
            })
            if src.test_type == Test.TYPE_TS:
                ctx["form_language"] = "ts"
                ctx["editor_mode"] = "javascript"
            messages.success(request, f"Duplicating {src.test_id} -- give the copy "
                                      "a new id, then Create.")
    if request.GET.get("recorded"):
        from core.services.remote_recorder import take_pending
        pending = take_pending()
        want_ts = request.GET.get("lang") == "ts"
        if pending.get("code"):
            ctx["code"] = pending["code_ts"] if want_ts else pending["code"]
            ctx["form_language"] = "ts" if want_ts else "python"
            if want_ts:
                ctx["editor_mode"] = "javascript"
            messages.success(request,
                             ("TypeScript recording" if want_ts else "Recording")
                             + " inserted below -- give the test an "
                               "id and a name, adjust the code, then Create.")
        else:
            messages.error(request, "No finished recording was waiting (it may "
                                    "have been used already).")
    return render(request, "core/test_edit.html", ctx)


def test_edit(request, test_id):
    test = get_object_or_404(Test, test_id=test_id, archived=False)
    if request.method == "POST":
        if request.POST.get("action") == "archive":
            archive_test(test)
            write_groups_file()
            messages.success(request, f"{test.test_id} archived -- files moved to "
                                      "tests/_archive/, history kept")
            return redirect("tests_list")
        test.name = request.POST.get("name", "").strip() or test.test_id
        test.description = request.POST.get("description", "").strip()
        test.tags = [t.strip() for t in request.POST.get("tags", "").split(",") if t.strip()]
        test.version_tag = request.POST.get("version_tag", "").strip()
        test.timeout_seconds = _int(request.POST.get("timeout_seconds"), 0)
        test.video_mode = _video_mode(request.POST.get("video_mode"))
        test.enabled = bool(request.POST.get("enabled"))
        test.save()
        code = request.POST.get("code", "")
        if code.strip():
            write_test_code(test, code.rstrip() + "\n")
        test.groups.set(Group.objects.filter(pk__in=request.POST.getlist("groups")))
        write_sidecar(test)
        write_groups_file()
        messages.success(request, f"Saved {test.test_id} (files updated)")
        return redirect("test_detail", test_id=test.test_id)
    return render(request, "core/test_edit.html", _edit_context(test, None))


def _edit_context(test, post):
    ctx = {
        "test": test,
        "all_groups": Group.objects.all(),
        "member_group_ids": set(test.groups.values_list("pk", flat=True)) if test else set(),
        "code": read_test_code(test) if test else NEW_TEST_TEMPLATE.format(name="New test"),
        "recorder_available": recorder.display_available(),
        # the editor follows the test's language (highlighting, hints, and
        # which starter template the New-test language select swaps in)
        "editor_mode": ("javascript" if test and test.test_type == Test.TYPE_TS
                        else "python"),
        "template_py": NEW_TEST_TEMPLATE.format(name="New test"),
        "template_ts": NEW_TS_TEST_TEMPLATE.format(name="New test"),
    }
    if post is not None:  # re-render after a validation error, keep input
        ctx.update({
            "form_test_id": post.get("test_id", ""),
            "form_language": post.get("language", "python"),
            "form_name": post.get("name", ""),
            "form_description": post.get("description", ""),
            "form_tags": post.get("tags", ""),
            "form_version_tag": post.get("version_tag", ""),
            "form_timeout": post.get("timeout_seconds", ""),
            "code": post.get("code", ""),
        })
        if ctx["form_language"] == "ts":
            ctx["editor_mode"] = "javascript"
    return ctx


def _int(value, fallback):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _video_mode(value):
    """Anything unrecognised means 'inherit the global setting' -- never
    trust a posted value into a stored choice field."""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if value in ("on", "off") else ""


# ---------------------------------------------------------------------------
# runs / batches
# ---------------------------------------------------------------------------

def runs_list(request):
    # Normal runs only by default -- a load test adds hundreds and would
    # flood this page. ?load=1 shows them when you actually want them.
    runs = Run.objects.select_related("test", "batch")
    if request.GET.get("load") != "1":
        runs = runs.filter(load_run__isnull=True)
    for key, field in (("status", "status"), ("trigger", "trigger"),
                       ("version", "target_version")):
        val = request.GET.get(key, "").strip()
        if val:
            runs = runs.filter(**{field: val})
    test_f = request.GET.get("test", "").strip()
    if test_f:
        runs = runs.filter(test__test_id=test_f)
    page = Paginator(runs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "core/runs_list.html", {
        "page": page,
        "status_choices": [s for s, _ in Run.STATUS_CHOICES],
        "trigger_choices": [t for t, _ in Batch.TRIGGER_CHOICES],
        # DISTINCT in the database, and no inherited ORDER BY: this used to
        # pull one string per run (100k rows) just to fill a dropdown.
        "version_choices": sorted(
            Run.objects.exclude(target_version="").order_by()
            .values_list("target_version", flat=True).distinct()),
        "test_choices": Test.objects.filter(archived=False).values_list("test_id", flat=True),
        "f_status": request.GET.get("status", ""),
        "f_trigger": request.GET.get("trigger", ""),
        "f_version": request.GET.get("version", ""),
        "f_test": test_f,
    })


def error_short(message: str) -> str:
    """The human sentence(s) of an error, without the traceback wall.
    Works on stored messages from any past run."""
    if not message:
        return ""
    text = message.split("Call log:")[0]
    text = text.split("Traceback (most recent call last)")[0]
    lines = [l for l in text.strip().splitlines() if l.strip()][:6]
    return "\n".join(lines)[:500]


# Everything the HUB writes into a run folder. Anything else there was saved
# by the test itself -- downloads, reports -- and is listed on the run page,
# or a file a test keeps (DEMO-014's route plan, an accessibility report) is
# on disk with no way to reach it from the UI.
HUB_RUN_FILES = frozenset({"run.log", "result.json", "security.json", "trace.zip",
                           "video.webm", "failure.png", "live.jpg", "pw-report.json"})
HUB_RUN_DIRS = frozenset({"frames", "screenshots", "_video_tmp", "test-results"})
SAVED_FILES_MAX = 50


def saved_by_test(names):
    """The run-folder paths (relative, '/'-separated) the TEST saved."""
    out = []
    for rel in sorted(names):
        if rel.split("/", 1)[0] in HUB_RUN_DIRS or rel in HUB_RUN_FILES:
            continue
        out.append(rel)
        if len(out) >= SAVED_FILES_MAX:
            break
    return out


def _run_folder_files(base):
    """Relative paths in a local run folder, never descending into the hub's
    own folders (a long run has thousands of frames)."""
    names = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return names
    for p in entries:
        if p.is_symlink() or p.name in HUB_RUN_DIRS:
            continue
        if p.is_file():
            names.append(p.name)
        elif p.is_dir():
            for q in sorted(p.rglob("*")):
                if q.is_file() and not q.is_symlink():
                    names.append(q.relative_to(base).as_posix())
        if len(names) > 10 * SAVED_FILES_MAX:
            break
    return names


def run_detail(request, run_id):
    run = get_object_or_404(Run.objects.select_related("test", "batch"), pk=run_id)
    cfg = appconfig.get_config()
    art = {}
    log_text = ""
    frames = []
    if run.artifacts_rel:
        base = cfg.results_dir / run.artifacts_rel
        if run.artifacts_remote:
            # files live in the bucket; the index recorded at upload time
            # tells us what exists without listing S3 on every page view
            import json as _json
            try:
                files = _json.loads(run.artifacts_index_json or "[]")
            except ValueError:
                files = []
            art = {
                "base": run.artifacts_rel,
                "remote": True,
                "screenshots": sorted(f.split("/", 1)[1] for f in files
                                      if f.startswith("screenshots/") and f.endswith(".png")),
                "video": "video.webm" in files,
                "trace": "trace.zip" in files,
                "failure": "failure.png" in files,
                "result_json": "result.json" in files,
                "saved": [{"name": f, "size": None} for f in saved_by_test(files)],
            }
        else:
            shots_dir = base / "screenshots"
            art = {
                "base": run.artifacts_rel,
                "remote": False,
                "screenshots": sorted(p.name for p in shots_dir.glob("*.png")) if shots_dir.is_dir() else [],
                "video": (base / "video.webm").exists(),
                "trace": (base / "trace.zip").exists(),
                "failure": (base / "failure.png").exists(),
                "result_json": (base / "result.json").exists(),
                "saved": [],
            }
            for rel in saved_by_test(_run_folder_files(base)):
                try:
                    art["saved"].append({"name": rel, "size": (base / rel).stat().st_size})
                except OSError:
                    pass
        log_path = base / "run.log"
        if log_path.exists() and not run.is_active:
            # Read only the tail: this file can be hundreds of KB and the
            # live view polls it about once a second.
            with open(log_path, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 20000))
                log_text = fh.read().decode("utf-8", "replace")
        elif run.artifacts_remote:
            # Offloaded: the log is in the bucket. Read its tail from there
            # (one ranged GET) so the page shows it like a local run's -- the
            # page used to print "(no log)" for EVERY offloaded run. If the
            # bucket cannot be read, the page points at the link instead.
            art["log_remote"] = True
            from core.services import storage
            log_text = storage.read_tail(f"{run.artifacts_rel}/run.log") or ""
        # Activity reel: frames the harness saved whenever the page painted.
        # Filenames carry elapsed ms. Thin very long reels for the player.
        if run.artifacts_remote:
            # Frames live in the bucket; build the reel from the index that
            # was recorded at upload time (each URL redirects to a presigned
            # link when fetched).
            import json as _json
            from django.urls import reverse as _reverse
            try:
                names = sorted(f for f in _json.loads(run.artifacts_index_json or "[]")
                               if f.startswith("frames/") and f.endswith(".jpg"))
            except ValueError:
                names = []
            step = max(1, len(names) // 1200)
            for name in names[::step]:
                try:
                    t_ms = int(name.rsplit("/", 1)[1].split("-", 1)[1].split(".")[0])
                except (IndexError, ValueError):
                    continue
                frames.append({"t": t_ms,
                               "u": _reverse("artifact",
                                             args=[f"{run.artifacts_rel}/{name}"])})
        frames_dir = base / "frames"
        if not frames and not run.is_active and frames_dir.is_dir():
            names = sorted(frames_dir.glob("f-*.jpg"))
            step = max(1, len(names) // 1200)
            from django.urls import reverse
            for p in names[::step]:
                try:
                    t_ms = int(p.stem.split("-", 1)[1])
                except (IndexError, ValueError):
                    continue
                frames.append({"t": t_ms,
                               "u": reverse("artifact",
                                            args=[f"{run.artifacts_rel}/frames/{p.name}"])})
    # Step timings on the replay's clock. The harness records a step when it
    # ENDS (t = seconds since the run started, the same zero as the replay's
    # frames), so its start is t - ms. Listed by start, so the list reads as
    # a timeline and a ctx.timed() block comes before the steps inside it.
    # TypeScript runs state each start ("start", read from the run's trace);
    # one without them (no trace kept, an older hub or engine) is listed,
    # not synced to the replay.
    timings = []
    for t in run.timings:
        end = float(t.get("t") or 0)
        if "start" in t:
            start = float(t["start"] or 0)
        else:
            start = max(0.0, end - float(t.get("ms") or 0) / 1000.0)
        # a ctx.timed() / test.step() phase, or a TypeScript test case:
        # highlighted AROUND the steps inside it, never as the current one
        timings.append(dict(t, start=round(start, 3), end=round(end, 3),
                            is_block=t.get("op") in ("block", "test")))
    timings.sort(key=lambda t: (t["start"], -float(t.get("ms") or 0), not t["is_block"]))
    if run.test.test_type == Test.TYPE_TS:
        timings_synced = bool(frames) and all("start" in t for t in run.timings)
    else:
        timings_synced = bool(frames)
    newer = (Run.objects.filter(test=run.test, pk__gt=run.pk)
             .order_by("pk").values_list("pk", flat=True).first())
    older = (Run.objects.filter(test=run.test, pk__lt=run.pk)
             .order_by("-pk").values_list("pk", flat=True).first())
    from core.services import security as security_svc
    return render(request, "core/run_detail.html", {
        "security_findings": security_svc.run_findings(run),
        "newer_id": newer,
        "older_id": older,
        "run": run,
        "art": art,
        "log_text": log_text,
        "frames": frames,
        "timings": timings,
        "timings_synced": timings_synced,
        "error_short": error_short(run.error_message),
        "eta": stats.eta_for_run(run) if run.is_active else None,
    })


HISTORY_ROWS = 30     # runs listed on the Groups / Schedules pages


def batch_detail(request, batch_id):
    batch = get_object_or_404(Batch.objects.prefetch_related("runs__test"), pk=batch_id)
    runs = list(batch.runs.select_related("test").order_by("pk"))
    # when this batch's last run should finish: the runner's queue simulated
    # over its worker slots -- the same figure the dashboard shows
    eta_total = None
    if any(r.status in Run.ACTIVE_STATUSES for r in runs):
        queue = list(Run.objects.filter(status__in=Run.ACTIVE_STATUSES, load_run__isnull=True)
                     .select_related("test").order_by("queued_at", "pk"))
        ends = stats.queue_forecast(queue, appconfig.get_config().max_parallel)
        mine = [ends.get(r.pk) for r in runs if r.status in Run.ACTIVE_STATUSES]
        if mine and all(x is not None for x in mine):
            eta_total = max(mine)
    # the stats: each test against what it USUALLY takes (its earlier
    # ordinary runs, never load iterations), and why anything failed
    rows = []
    for r in runs:
        usual = stats.usual_duration(r.test, r.pk, exclude_batch=batch)
        versus, versus_class = stats.versus_usual(
            r.duration_seconds if r.status in Run.REPRESENTATIVE_STATUSES else None, usual)
        why = ""
        if r.status not in (Run.PASSED,) + Run.ACTIVE_STATUSES:
            why = (error_short(r.error_message).splitlines() or [""])[0][:220]
        rows.append({"r": r, "usual": usual, "versus": versus, "versus_class": versus_class,
                     "why": why})
    # the same group's or schedule's previous and next run, to step through them
    same = None
    if batch.schedule_id:
        same = Batch.objects.filter(schedule_id=batch.schedule_id)
    elif batch.group_id:
        same = Batch.objects.filter(group_id=batch.group_id)
    older = newer = None
    if same is not None:
        older = same.filter(created_at__lt=batch.created_at).order_by("-created_at").first()
        newer = same.filter(created_at__gt=batch.created_at).order_by("created_at").first()
    return render(request, "core/batch_detail.html", {
        "batch": batch,
        "runs": runs,
        "rows": rows,
        "summary": stats.batch_summary(batch, runs),
        "counts": batch.runs_summary,
        "active": batch.is_active,
        "eta_total": eta_total,
        "older": older,
        "newer": newer,
        # the label adds nothing when it only repeats the title
        "show_label": bool(batch.label) and batch.trigger != "schedule" and not (
            batch.group and batch.label == f"group: {batch.group.name}"),
    })


# ---------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------

def groups_list(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "Group name is required")
        elif Group.objects.filter(name=name).exists():
            messages.error(request, f"Group {name!r} already exists")
        else:
            group = Group.objects.create(
                name=name, description=request.POST.get("description", "").strip())
            write_groups_file()
            messages.success(request, f"Group {name!r} created")
            return redirect("group_detail", group_id=group.pk)
    groups = []
    for group in Group.objects.prefetch_related("tests"):
        last_batch = Batch.objects.filter(group=group).order_by("-created_at").first()
        groups.append({"group": group, "last_batch": last_batch,
                       "last": stats.batch_summary(last_batch) if last_batch else None})
    # what was run: every group run, however it was started (the Run group
    # button, a group's schedule, `testhub runtest --group`), newest first
    recent = (Batch.objects.filter(group__isnull=False)
              .select_related("group", "schedule").prefetch_related("runs")[:HISTORY_ROWS])
    return render(request, "core/groups.html", {
        "groups": groups,
        "history": [stats.batch_summary(b, list(b.runs.all())) for b in recent],
        "history_rows": HISTORY_ROWS,
    })


def group_detail(request, group_id):
    group = get_object_or_404(Group, pk=group_id)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "members":
            group.tests.set(Test.objects.filter(pk__in=request.POST.getlist("tests"),
                                                archived=False))
            group.description = request.POST.get("description", group.description).strip()
            group.save()
            write_groups_file()
            messages.success(request, "Group membership saved (files updated)")
        elif action == "delete":
            group.delete()
            write_groups_file()
            messages.success(request, f"Group {group.name!r} deleted")
            return redirect("groups_list")
        return redirect("group_detail", group_id=group.pk)
    batches = (Batch.objects.filter(group=group)
               .prefetch_related("runs").order_by("-created_at")[:15])
    return render(request, "core/group_detail.html", {
        "chart_grouptrend": stats.group_trend(group),
        "group": group,
        "member_ids": set(group.tests.values_list("pk", flat=True)),
        "all_tests": Test.objects.filter(archived=False),
        "batches": batches,
        "schedules": group.schedules.all(),
    })


# ---------------------------------------------------------------------------
# schedules
# ---------------------------------------------------------------------------

def schedules_list(request):
    cfg = appconfig.get_config()
    if request.method == "POST":
        target = request.POST.get("target", "")
        days = [int(d) for d in request.POST.getlist("days")]
        time_str = request.POST.get("time", "09:00")
        if not days:
            messages.error(request, "Pick at least one weekday")
            return redirect("schedules_list")
        sched = Schedule()
        if target.startswith("test:"):
            sched.test = get_object_or_404(Test, test_id=target[5:], archived=False)
        elif target.startswith("group:"):
            sched.group = get_object_or_404(Group, pk=int(target[6:]))
        else:
            messages.error(request, "Pick a test or a group to schedule")
            return redirect("schedules_list")
        from core.services.syncer import parse_hhmm
        sched.days = days
        sched.time_of_day = parse_hhmm(time_str)
        browser = request.POST.get("browser", "").strip()
        sched.browser = browser if browser in BROWSER_CHOICES else ""
        sched.enabled = True
        sched.save()
        if sched.test_id:
            write_sidecar(sched.test)
        else:
            write_groups_file()
        messages.success(request, f"Schedule added: {sched}")
        return redirect("schedules_list")

    now = timezone.now()
    rows = []
    for sched in Schedule.objects.select_related("test", "group"):
        last = sched.batches.order_by("-created_at").first()
        rows.append({"s": sched, "next": next_slot(sched, now, cfg.timezone),
                     "last": stats.batch_summary(last) if last else None})
    rows.sort(key=lambda r: (r["next"] is None, r["next"] or now))
    # what ran on a schedule, newest first. Batches from before 2.16 have no
    # link to their schedule; their label still says which one.
    fired = (Batch.objects.filter(trigger="schedule")
             .select_related("group", "schedule", "schedule__test", "schedule__group")
             .prefetch_related("runs__test")[:HISTORY_ROWS])
    return render(request, "core/schedules.html", {
        "rows": rows,
        "history": [stats.batch_summary(b, list(b.runs.all())) for b in fired],
        "history_rows": HISTORY_ROWS,
        "tests": Test.objects.filter(archived=False, enabled=True),
        "groups": Group.objects.all(),
        "tz": cfg.timezone,
        "browsers": BROWSER_CHOICES,
        "default_browser": cfg.browser,
    })


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def metrics(request):
    days = _int(request.GET.get("days"), 30) or 30
    table = stats.per_test_table(days=days)
    with_runs = [r for r in table if r["runs"]]
    slowest = sorted((r for r in with_runs if r["avg_duration"]),
                     key=lambda r: -(r["avg_duration"] or 0))[:10]
    flakiest = sorted((r for r in with_runs if r["flakiness"]),
                      key=lambda r: -(r["flakiness"] or 0))[:10]
    failures = stats.new_vs_chronic()
    return render(request, "core/metrics.html", {
        "days": days,
        "failures": failures,
        "clusters": stats.error_clusters(days=days),
        "chart_steps": stats.slowest_steps(days=days),
        "heatmap": stats.failure_heatmap(days=max(days, 30)),
        "health": stats.suite_health(),
        "overview": stats.global_overview(days=days),
        "chart_days": stats.runs_per_day(days=days),
        "chart_slowest": slowest,
        "chart_flakiest": flakiest,
        "chart_triggers": stats.trigger_split(days=days),
        "chart_versions": stats.version_split(days=days),
        "failing": stats.failing_now(),
        "table": table,
    })


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def analytics(request):
    """Everything by WEBSITE version: which version of the site under test
    made which tests slower or flakier. The CSV sections are text on
    purpose — the lab's chat tool moves text between networks, not files."""
    from core.services import analytics as an
    cfg = appconfig.get_config()
    versions, matrix = an.per_test_by_version()
    matrix_rows = [
        {"test": test, "cells": [cells.get(v) for v in versions]}
        for test, cells in sorted(matrix.items(), key=lambda kv: kv[0].test_id)
    ]
    pair, trend = an.speed_trend()
    return render(request, "core/analytics.html", {
        "summary": an.versions_summary(),
        "versions": versions,
        "matrix_rows": matrix_rows,
        "trend": trend,
        "trend_pair": pair,
        "sections": an.csv_sections(),
        "current_version": cfg.target_version or an.UNVERSIONED,
    })


def settings_page(request):
    cfg = appconfig.get_config()
    from core import secretstore

    if request.method == "POST" and request.POST.get("action") == "secret-set":
        name = (request.POST.get("secret_name") or "").strip()
        value = request.POST.get("secret_value") or ""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name or ""):
            messages.error(request, "Name may use letters, digits, '.', '-' "
                                    "and '_' only")
        elif not value:
            messages.error(request, "A credential needs a value")
        else:
            store = secretstore.load(cfg.data_dir)
            store[name] = value
            secretstore.save(cfg.data_dir, store)
            # The value is never echoed back, not even in the confirmation.
            messages.success(request, f"Saved credential {name!r}. Use it in a "
                                      f"test as ctx.secret({name!r}).")
        return redirect("settings_page")

    if request.method == "POST" and request.POST.get("action") == "secret-delete":
        name = (request.POST.get("secret_name") or "").strip()
        store = secretstore.load(cfg.data_dir)
        if store.pop(name, None) is not None:
            secretstore.save(cfg.data_dir, store)
            messages.success(request, f"Removed credential {name!r}")
        return redirect("settings_page")

    if request.method == "POST" and request.POST.get("action") == "restore":
        test = get_object_or_404(Test, test_id=request.POST.get("test_id"), archived=True)
        import os as _os
        archive_dir = cfg.tests_dir / "_archive"
        moved = 0
        for name in (test.file_path, str(Path(test.file_path).with_suffix(".json"))):
            src = archive_dir / name
            if src.exists():
                _os.replace(src, cfg.tests_dir / name)
                moved += 1
        from core.services.syncer import sync_from_files
        sync_from_files()
        if moved:
            messages.success(request, f"{test.test_id} restored ({moved} file(s) "
                                      "moved back) and re-synced")
        else:
            messages.error(request, f"files for {test.test_id} not found in "
                                    "tests/_archive/ -- restore them manually")
        return redirect("settings_page")
    if request.method == "POST":
        updates = {
            "target": {
                "name": request.POST.get("target_name", cfg.target_name).strip(),
                "url": request.POST.get("target_url", "").strip(),
                "version": request.POST.get("target_version", "").strip(),
            },
            "runner": {
                "browser": request.POST.get("browser", cfg.browser),
                "headed": bool(request.POST.get("headed")),
                "max_parallel": _int(request.POST.get("max_parallel"), cfg.max_parallel) or 1,
                # Disk knobs: each of these is a real chunk of space per run.
                "video": bool(request.POST.get("video")),
                "trace": bool(request.POST.get("trace")),
                "activity_frames": bool(request.POST.get("activity_frames")),
                "live_screenshots": bool(request.POST.get("live_screenshots")),
                "security_checks": bool(request.POST.get("security_checks")),
                "keep_awake": bool(request.POST.get("keep_awake")),
            },
            "storage": {
                "backend": request.POST.get("storage_backend", cfg.storage_backend),
                "bucket": request.POST.get("s3_bucket", cfg.s3_bucket).strip(),
                "prefix": request.POST.get("s3_prefix", cfg.s3_prefix).strip(),
                "region": request.POST.get("s3_region", cfg.s3_region).strip(),
                "endpoint_url": request.POST.get("s3_endpoint", cfg.s3_endpoint).strip(),
                "delete_local_after_upload": bool(request.POST.get("s3_delete_local")),
            },
            "schedule": {
                "timezone": request.POST.get("timezone", cfg.timezone).strip()
                            or cfg.timezone,
            },
            "maintenance": {
                "retention_days": _int(request.POST.get("retention_days"), cfg.retention_days),
            },
        }
        appconfig.save_config(updates)
        messages.success(request, f"Saved to {appconfig.config_path()}. "
                                  "Worker-count and timezone changes take effect "
                                  "after a restart of `manage.py serve`.")
        return redirect("settings_page")
    from core.services.housekeeping import results_usage
    from core.services import diagnostics
    return render(request, "core/settings_page.html", {
        "secret_names": secretstore.names(cfg.data_dir),
        "secrets_loose": secretstore.is_loose(secretstore.path_for(cfg.data_dir)),
        "checks": diagnostics.run_checks(quick=True),
        "app_version": cfg.app_version,
        "cfg": cfg,
        "config_path": appconfig.config_path(),
        "archived": Test.objects.filter(archived=True),
        "browsers": BROWSER_CHOICES,
        "usage": results_usage(),
        "server_now": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
    })


@ensure_csrf_cookie
def recorder_page(request):
    cfg = appconfig.get_config()
    test_id = request.GET.get("test", "").strip()
    test = Test.objects.filter(test_id=test_id, archived=False).first() if test_id else None
    from core.services.remote_recorder import clean_start_path
    try:
        # ?start=freight/ begins the recording on a page UNDER the target
        start_path = clean_start_path(request.GET.get("start", ""))
    except ValueError:
        start_path = ""
    return render(request, "core/recorder.html", {
        "target_url": cfg.target_url,
        "start_path": start_path,
        "for_test": test,
    })


def backup_download(request):
    """Everything that would hurt to lose, as one zip."""
    from django.http import HttpResponse
    from core.services import backup as backup_svc
    data = backup_svc.build_backup(include_results=bool(request.GET.get("results")))
    response = HttpResponse(data, content_type="application/zip")
    stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M%S")
    response["Content-Disposition"] = f'attachment; filename="testhub-backup-{stamp}.zip"'
    return response


def tests_export(request):
    """Test definitions only -- the format you carry between machines."""
    from django.http import HttpResponse
    from core.services import backup as backup_svc
    ids = [i for i in request.GET.get("ids", "").split(",") if i]
    data = backup_svc.export_tests(ids or None)
    response = HttpResponse(data, content_type="application/zip")
    stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d")
    response["Content-Disposition"] = f'attachment; filename="testhub-tests-{stamp}.zip"'
    return response


def tests_import(request):
    """Upload a tests zip (from another machine) and sync it in."""
    from core.services import backup as backup_svc
    from core.services.syncer import sync_from_files
    if request.method != "POST" or "file" not in request.FILES:
        return redirect("settings_page")
    upload = request.FILES["file"]
    tmp = appconfig.get_config().data_dir / f".import-{upload.name[:60]}"
    with open(tmp, "wb") as fh:
        for chunk in upload.chunks():
            fh.write(chunk)
    try:
        backup_svc.auto_backup(reason="before-import")
        report = backup_svc.import_tests(tmp)
    except Exception as exc:
        messages.error(request, f"Import failed: {exc}")
        return redirect("settings_page")
    finally:
        tmp.unlink(missing_ok=True)
    sync_from_files()
    if report["written"]:
        messages.success(request, f"Imported {len(report['written'])} file(s) and "
                                  "re-synced. A safety backup was taken first.")
    for err in report["errors"][:5]:
        messages.error(request, err)
    if not report["written"] and not report["errors"]:
        messages.error(request, "That zip contained no test files.")
    return redirect("settings_page")


def metrics_csv(request):
    """The per-test table as CSV -- for sharing numbers with people who do
    not have hub access (works offline, opens in any spreadsheet)."""
    import csv
    from django.http import HttpResponse
    days = _int(request.GET.get("days"), 30) or 30
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="testhub-metrics-{days}d.csv"'
    writer = csv.writer(response)
    writer.writerow(["test_id", "name", "runs", "pass_rate_pct", "avg_seconds",
                     "p95_seconds", "flakiness", "last_status"])
    for row in stats.per_test_table(days=days):
        writer.writerow([
            row["test_id"], row["name"], row["runs"],
            f"{row['pass_rate']:.1f}" if row["pass_rate"] is not None else "",
            f"{row['avg_duration']:.2f}" if row["avg_duration"] is not None else "",
            f"{row['p95_duration']:.2f}" if row["p95_duration"] is not None else "",
            f"{row['flakiness']:.2f}" if row["flakiness"] is not None else "",
            row["last_status"] or "",
        ])
    return response


def login_page(request):
    """The optional shared-password gate (site.password)."""
    cfg = appconfig.get_config()
    if not cfg.password:
        return redirect("dashboard")
    error = ""
    if request.method == "POST":
        if request.POST.get("password", "") == cfg.password:
            request.session["hub_auth"] = "ok"
            request.session.set_expiry(60 * 60 * 12)
            # ?next= is attacker-controllable, so only follow it when it
            # points back at this host (otherwise it is an open redirect).
            from django.utils.http import url_has_allowed_host_and_scheme
            nxt = request.GET.get("next") or ""
            if nxt and url_has_allowed_host_and_scheme(
                    nxt, allowed_hosts={request.get_host()},
                    require_https=request.is_secure()):
                return redirect(nxt)
            return redirect("dashboard")
        error = "Wrong password."
    return render(request, "core/login.html", {"error": error})


def logout_page(request):
    request.session.flush()
    return redirect("login_page")


def help_page(request):
    return render(request, "core/help.html", {})


def run_report(request, run_id):
    """One self-contained HTML file a tester can attach to an email or a
    ticket: outcome, plain-language error, timings, screenshots embedded."""
    import base64
    run = get_object_or_404(Run.objects.select_related("test"), pk=run_id)
    cfg = appconfig.get_config()
    shots = []
    total = 0
    if run.artifacts_rel:
        base = cfg.results_dir / run.artifacts_rel
        candidates = sorted((base / "screenshots").glob("*.png")) if (base / "screenshots").is_dir() else []
        if (base / "failure.png").exists():
            candidates.append(base / "failure.png")
        for path in candidates:
            data = path.read_bytes()
            if total + len(data) > 8_000_000:
                break
            total += len(data)
            shots.append({"name": path.name,
                          "b64": base64.b64encode(data).decode("ascii")})
    body = render(request, "core/run_report.html", {
        "run": run,
        "error_short": error_short(run.error_message),
        "timings": run.timings,
        "shots": shots,
        "generated": timezone.localtime(timezone.now()),
    }).content
    from django.http import HttpResponse
    response = HttpResponse(body, content_type="text/html; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="run-{run.pk}-{run.test.test_id}-{run.status}.html"')
    return response


# ---------------------------------------------------------------------------
# demo target (the page the sample tests drive)
# ---------------------------------------------------------------------------

def demo(request):
    # ?timer=<seconds> shortens the countdown so tests (and the offline
    # verifications) can exercise the long-wait path in seconds instead of
    # half an hour. The default is the real thing: 30 minutes.
    try:
        timer_seconds = max(1, min(7200, int(request.GET.get("timer", 1800))))
    except (TypeError, ValueError):
        timer_seconds = 1800
    return render(request, "core/demo.html",
                  {"submitted": None, "timer_seconds": timer_seconds})


# --- the demo target's login, so authenticated checks have something real ---
# Deliberately implemented CORRECTLY: the session id is regenerated on login,
# the private page refuses anonymous callers, logout clears the session
# server-side, and private responses are no-store. That way the shipped
# example shows what a PASSING authenticated check looks like. Append
# ?weak=fixation to see the failing case on purpose.
DEMO_USER, DEMO_PASSWORD = "tester", "demo-password"


def demo_login(request):
    # A session exists BEFORE authenticating -- which is normal: most apps
    # keep a "next" URL, a CSRF nonce or a locale here. It is also what makes
    # session fixation possible at all: without a pre-login session there is
    # no identifier for an attacker to have planted.
    request.session.setdefault("visited_login", True)
    if request.method == "POST":
        if (request.POST.get("username") == DEMO_USER
                and request.POST.get("password") == DEMO_PASSWORD):
            if request.GET.get("weak") != "fixation":
                # The important line: a new session id after authenticating.
                request.session.cycle_key()
            request.session["demo_user"] = DEMO_USER
            return redirect("demo_private")
        return render(request, "core/demo_login.html",
                      {"error": "Wrong username or password"}, status=401)
    return render(request, "core/demo_login.html", {})


def demo_private(request):
    if not request.session.get("demo_user"):
        return redirect("demo_login")
    response = render(request, "core/demo_private.html",
                      {"user": request.session["demo_user"]})
    response["Cache-Control"] = "no-store"
    return response


def demo_logout(request):
    request.session.flush()            # server-side, not just the cookie
    return redirect("demo_login")


def demo_submit(request):
    return render(request, "core/demo.html", {"submitted": {
        "name": request.GET.get("name", ""),
        "email": request.GET.get("email", ""),
        "priority": request.GET.get("priority", ""),
    }})


# ---------------------------------------------------------------------------
# artifact files
# ---------------------------------------------------------------------------

# served with CSP sandbox: types a browser would run as a document
ACTIVE_ARTIFACT_TYPES = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml",
                                   "text/xml", "application/xml"})


def artifact(request, relpath):
    cfg = appconfig.get_config()
    base = cfg.results_dir.resolve()
    path = (base / relpath).resolve()
    # Containment FIRST: this guard used to sit after the S3 branch, so a
    # traversal path was handed straight to presigned_url() as an object key.
    if base not in path.parents and path != base:
        raise Http404()
    if not path.is_file() and cfg.storage_backend == "s3":
        # offloaded run: hand out a short-lived presigned URL instead
        from core.services import storage
        url = storage.presigned_url(relpath)
        if url:
            return redirect(url)
    if not path.is_file():
        raise Http404()
    ctype, _ = mimetypes.guess_type(str(path))
    if path.suffix in (".log", ".txt", ".lock"):
        ctype = "text/plain; charset=utf-8"
    response = FileResponse(open(path, "rb"), content_type=ctype or "application/octet-stream")
    if path.name == "live.jpg":
        response["Cache-Control"] = "no-store, max-age=0"
    # A run folder holds files a TEST wrote, and a test writes what the site
    # under test gave it -- a saved page, a report quoting its HTML. Served
    # plainly from the hub's origin, an HTML or SVG file would run that
    # site's scripts AS THE HUB (its session, its CSRF token). `sandbox`
    # renders it as an opaque origin with scripts off; nosniff stops a
    # browser from promoting any other file to HTML.
    if (ctype or "").split(";")[0] in ACTIVE_ARTIFACT_TYPES:
        response["Content-Security-Policy"] = "sandbox"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# load / performance testing
# ---------------------------------------------------------------------------

def load_list(request):
    """Saved load plans, plus what has been run."""
    from core.models import LoadPlan, LoadRun
    from core.services import loadtest
    runs = (LoadRun.objects.select_related("test", "plan")
            .order_by("-id")[:PAGE_SIZE])
    return render(request, "core/load_list.html", {
        "plans": LoadPlan.objects.select_related("test").all(),
        "runs": runs,
        "tests": Test.objects.filter(archived=False, enabled=True),
        "capacity": loadtest.capacity_hint(),
    })


def load_new(request):
    """Plan a load test against one existing test.

    The form is baseline-first on purpose: you cannot sensibly choose a
    duration without knowing how long one pass takes, so the page shows that
    (and the projected iteration count) before there is anything to submit.
    """
    from core.models import LoadPlan, LoadRun
    from core.services import loadtest
    test_id = request.GET.get("test") or request.POST.get("test") or ""
    test = Test.objects.filter(test_id=test_id, archived=False).first()

    if request.method == "POST":
        if test is None:
            messages.error(request, "Pick a test to put under load")
            return redirect("load_new")
        duration = _int(request.POST.get("duration_seconds"), 600) or 600
        concurrency = _int(request.POST.get("concurrency"), 3) or 1
        ramp = _int(request.POST.get("ramp_seconds"), 0)
        think = _int(request.POST.get("think_time_seconds"), 0)
        cap = _int(request.POST.get("max_iterations"), 0)
        keep = bool(request.POST.get("keep_artifacts"))
        plan = None
        if request.POST.get("save_plan") and request.POST.get("name", "").strip():
            plan = LoadPlan.objects.create(
                name=request.POST["name"].strip(), test=test,
                description=request.POST.get("description", "").strip(),
                duration_seconds=duration, concurrency=concurrency,
                ramp_seconds=ramp, think_time_seconds=think,
                max_iterations=cap, keep_artifacts=keep)
        try:
            load_run = loadtest.start_load_run(
                test, duration_seconds=duration, concurrency=concurrency,
                ramp_seconds=ramp, think_time=think, max_iterations=cap,
                keep_artifacts=keep, plan=plan,
                label=request.POST.get("name", "").strip())
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(f"{reverse('load_new')}?test={test.test_id}")
        return redirect("load_detail", load_run_id=load_run.pk)

    preview = None
    if test is not None:
        preview = loadtest.plan_preview(
            test,
            _int(request.GET.get("duration_seconds"), 600) or 600,
            _int(request.GET.get("concurrency"), 3) or 1,
            _int(request.GET.get("think_time_seconds"), 0))
    return render(request, "core/load_new.html", {
        "tests": Test.objects.filter(archived=False, enabled=True),
        "test": test,
        "preview": preview,
        "capacity": loadtest.capacity_hint(),
        "form": {
            "duration_seconds": _int(request.GET.get("duration_seconds"), 600) or 600,
            "concurrency": _int(request.GET.get("concurrency"), 3) or 1,
            "ramp_seconds": _int(request.GET.get("ramp_seconds"), 0),
            "think_time_seconds": _int(request.GET.get("think_time_seconds"), 0),
        },
    })


def load_detail(request, load_run_id):
    """Watch it happen, then read what it found."""
    from core.models import LoadRun
    from core.services import loadtest
    load_run = get_object_or_404(
        LoadRun.objects.select_related("test", "plan"), pk=load_run_id)
    summary = load_run.summary if not load_run.is_active else loadtest.summarize(load_run)
    slowest = (load_run.iterations.exclude(duration_seconds=None)
               .select_related("test").order_by("-duration_seconds")[:10])
    failures = (load_run.iterations.exclude(status=Run.PASSED)
                .exclude(status__in=Run.ACTIVE_STATUSES)
                .select_related("test").order_by("iteration")[:20])
    return render(request, "core/load_detail.html", {
        "load_run": load_run,
        "summary": summary,
        "timeline": json.dumps(loadtest.timeline(load_run)),
        "steps": loadtest.step_breakdown(load_run),
        "slowest": slowest,
        "failures": failures,
    })


def security_page(request):
    """Current security posture of the application under test."""
    from core.services import security as security_svc
    return render(request, "core/security.html", {
        "posture": security_svc.posture(),
        "checks_on": appconfig.get_config().security_checks,
    })
