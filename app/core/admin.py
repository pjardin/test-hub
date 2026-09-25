from django.contrib import admin

from .models import Batch, Group, Run, Schedule, Test


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ("test_id", "name", "version_tag", "enabled", "archived", "file_path")
    search_fields = ("test_id", "name", "tags_json")
    list_filter = ("enabled", "archived")


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    filter_horizontal = ("tests",)


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ("__str__", "enabled", "last_fired")


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("id", "label", "trigger", "created_at")


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ("id", "test", "status", "trigger", "queued_at", "duration_seconds",
                    "target_version", "browser")
    list_filter = ("status", "trigger", "browser", "target_version")
    search_fields = ("test__test_id",)
