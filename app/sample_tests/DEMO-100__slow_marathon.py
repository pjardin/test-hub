"""Deliberately slow test (~75s).

Exists so you can try the operational features safely:
  * watch the live preview while it runs,
  * see the ETA math on the dashboard (after a couple of runs),
  * practice the Kill button on something harmless.
"""
import time


def run(page, ctx):
    page.goto(ctx.base_url)
    for i in range(15):
        page.click("#reveal-btn")
        page.evaluate(f"document.title = 'marathon step {i + 1}/15'")
        ctx.log(f"step {i + 1}/15")
        time.sleep(5)
    ctx.screenshot("marathon-done")
