"""testhub demo_release [VERSION] [--incident outage|slow|clear]
-- list the demo site's releases, deploy one, or start/clear a simulated incident.

The Release console (/demo/freight/releases/) does the same from a browser;
this is for scripts and terminals: deploy 2.0.0, queue the regression group,
deploy the next. The deployed version is a file in the data directory, so it
takes effect at once for the serving hub too -- no restart.
"""
from django.core.management.base import BaseCommand, CommandError

from core import appconfig
from freight import releases


class Command(BaseCommand):
    help = "List the Acme Freight demo releases, or deploy one: testhub demo_release 2.0.0"

    def add_arguments(self, parser):
        parser.add_argument("version", nargs="?",
                            help="the release to deploy (omit to just list them)")
        parser.add_argument("--incident", choices=["outage", "slow", "clear"],
                            help="simulate an outage / a slowdown, or clear it")

    def handle(self, *args, **opts):
        version = opts.get("version")
        if version:
            try:
                rel = releases.deploy(version, by="terminal")
            except ValueError:
                raise CommandError(
                    f"no release {version!r} -- choose one of: "
                    + ", ".join(r.version for r in releases.RELEASES))
            self.stdout.write(f"deployed Acme Freight {rel.version} ({rel.name})")
        if opts.get("incident"):
            kind = opts["incident"]
            releases.set_incident(None if kind == "clear" else kind, by="terminal")
            self.stdout.write(releases.INCIDENTS.get(kind, "incident cleared -- all systems normal"))
        live = releases.deployed()
        for r in releases.RELEASES:
            mark = "*" if r.version == live.version else " "
            self.stdout.write(f" {mark} {r.version:6s} {r.name:14s} {r.summary}")
        now = releases.incident()
        if now:
            self.stdout.write(f"INCIDENT IN PROGRESS: {releases.INCIDENTS[now]}")
        cfg = appconfig.get_config()
        if cfg.target_is_demo:
            self.stdout.write(f"new runs are stamped with website version {cfg.target_version}")
        else:
            self.stdout.write("note: the hub is testing another site (target.url is set), "
                              "so its runs are not stamped with the demo's version")
