"""Shared code for tests — page objects, helpers, anything reusable.

The syncer ignores every file and folder whose name starts with "_", so
nothing in here is ever mistaken for a test. The harness puts the tests
folder on sys.path, so tests import it like any package:

    from _lib.pages import DemoPage

Structure it however your team likes; this is an ordinary Python package.
It travels with the tests (export zip, disc, git) exactly like they do.
"""
