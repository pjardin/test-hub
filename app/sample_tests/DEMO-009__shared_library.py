"""A test built the way a real code base is: page objects in a shared
package, the test itself just intent + assertions.

The class lives in _lib/pages.py — the syncer ignores anything starting
with "_", and the harness puts the tests folder on sys.path, so a whole
team library (page objects, data builders, custom waits) drops in beside
the tests and travels with them. The only contract the hub asks of THIS
file is run(page, ctx). See CONNECT-YOUR-TESTS.md for the full recipe.
"""
from _lib.pages import DemoPage


def run(page, ctx):
    demo = DemoPage(page, ctx).open()
    assert demo.title(), "page has no title"

    with ctx.timed("submit via page object"):
        demo.submit_request("Grace Hopper", "grace@example.gov", "high")

    body = demo.result_text()
    ctx.log(f"result: {body!r}")
    assert "Grace Hopper" in body, "submitted name not in the result"
    ctx.screenshot("done")
