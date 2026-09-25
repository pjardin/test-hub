"""Visual comparison: does the page still LOOK right?

Shows the pattern for screenshot comparison with the bundled image stack.
First run saves a baseline into the test's own folder; later runs compare
against it and fail if too many pixels moved.

Tune SSIM_MIN / DIFF_MAX to taste: anti-aliasing and clocks always differ a
little, so a threshold is normal. Delete the baseline PNG to re-bless it
after an intentional UI change.
"""
from pathlib import Path

SSIM_MIN = 0.97      # 1.0 = identical
DIFF_MAX = 0.02      # fraction of pixels allowed to differ


def run(page, ctx):
    page.goto(ctx.base_url)
    page.wait_for_selector("#name")

    shot = ctx.artifacts_dir / "screenshots" / "current.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))

    baseline = Path(__file__).with_name(f"{ctx.test_id}__baseline.png")
    if not baseline.exists():
        baseline.write_bytes(shot.read_bytes())
        ctx.log(f"no baseline yet — saved this run as the baseline "
                f"({baseline.name}). Re-run to start comparing.")
        return

    import cv2
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    current = cv2.imread(str(shot))
    expected = cv2.imread(str(baseline))
    assert current is not None and expected is not None, "could not read the images"

    if current.shape != expected.shape:
        raise AssertionError(
            f"page size changed: baseline {expected.shape[1]}x{expected.shape[0]}, "
            f"now {current.shape[1]}x{current.shape[0]}")

    grey_a = cv2.cvtColor(expected, cv2.COLOR_BGR2GRAY)
    grey_b = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    score, diff = ssim(grey_a, grey_b, full=True)
    changed = float(np.mean((diff < 0.9).astype(np.float32)))
    ctx.log(f"similarity {score:.4f} · {changed*100:.2f}% of pixels differ")

    # Always leave a human-readable diff image behind, pass or fail.
    heat = (255 - (diff * 255)).astype("uint8")
    overlay = current.copy()
    overlay[heat > 60] = (0, 0, 255)          # mark differing areas in red
    cv2.imwrite(str(shot.with_name("02-diff.png")), overlay)

    assert score >= SSIM_MIN and changed <= DIFF_MAX, (
        f"the page looks different: similarity {score:.4f} (min {SSIM_MIN}), "
        f"{changed*100:.2f}% pixels changed (max {DIFF_MAX*100:.0f}%). "
        f"See 02-diff.png in this run's screenshots — red marks what moved.")
