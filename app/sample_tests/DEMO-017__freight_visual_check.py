"""Does Acme Freight still LOOK right? Screenshot vs. a blessed baseline.

The first run saves a baseline next to this test (DEMO-017__baseline.png);
every later run compares the home page against it with OpenCV +
scikit-image and fails when too much moved -- leaving a copy of the page
with the changed areas painted red, so a person sees at a glance WHAT moved.

A small change (the version number in the header) stays under the
threshold; a redesign does not. After an INTENDED redesign, delete the
baseline file to re-bless the new look (the next run saves a fresh one).
"""
from pathlib import Path

SSIM_MIN = 0.97      # 1.0 = identical
DIFF_MAX = 0.02      # fraction of pixels allowed to differ


def run(page, ctx):
    page.goto(ctx.base_url + "freight/")
    page.wait_for_load_state("networkidle")        # the map is an image: let it land
    shot = ctx.artifacts_dir / "screenshots" / "current.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))

    baseline = Path(__file__).with_name(f"{ctx.test_id}__baseline.png")
    if not baseline.exists():
        baseline.write_bytes(shot.read_bytes())
        ctx.log(f"no baseline yet -- saved this run as the baseline ({baseline.name}). "
                f"Later runs compare against it.")
        return

    import cv2
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    current, expected = cv2.imread(str(shot)), cv2.imread(str(baseline))
    assert current is not None and expected is not None, "could not read the images"
    if current.shape != expected.shape:
        raise AssertionError(
            f"the page size changed: baseline {expected.shape[1]}x{expected.shape[0]}, "
            f"now {current.shape[1]}x{current.shape[0]}")
    score, diff = ssim(cv2.cvtColor(expected, cv2.COLOR_BGR2GRAY),
                       cv2.cvtColor(current, cv2.COLOR_BGR2GRAY), full=True)
    changed = float(np.mean((diff < 0.9).astype(np.float32)))
    ctx.log(f"similarity {score:.4f}; {changed * 100:.2f}% of pixels differ")

    marked = current.copy()
    marked[(255 - (diff * 255)).astype("uint8") > 60] = (0, 0, 255)
    cv2.imwrite(str(shot.with_name("02-what-changed.png")), marked)

    assert score >= SSIM_MIN and changed <= DIFF_MAX, (
        f"the home page looks different: similarity {score:.4f} (min {SSIM_MIN}), "
        f"{changed * 100:.1f}% of pixels changed (max {DIFF_MAX * 100:.0f}%). "
        f"02-what-changed.png in this run's screenshots marks it in red. If the "
        f"change is intended, delete {baseline.name} to re-bless the new look.")
