# Third-party components

## Included in this repository

| component | where | licence |
|---|---|---|
| [Chart.js](https://www.chartjs.org/) 4.4.9 | `app/core/static/core/vendor/chart.umd.min.js` | MIT |
| [CodeMirror](https://codemirror.net/5/) 5.65.16 (editor + modes/add-ons) | `app/core/static/core/vendor/codemirror/` | MIT |
| [axe-core](https://github.com/dequelabs/axe-core) 4.13.0, unmodified | `app/sample_tests/_lib/axe/axe.min.js` | MPL-2.0 (full text in `app/sample_tests/_lib/axe/LICENSE`) |
| [Natural Earth](https://www.naturalearthdata.com/) map data, simplified | `app/freight/assets/geo.json` (the demo site's maps) | public domain |

They are vendored (not loaded from a CDN) because the hub is built to run
on networks with no internet access.

## Installed by `./testhub.sh setup` (not part of this repository)

| component | licence |
|---|---|
| Django 4.2 | BSD-3-Clause |
| waitress | ZPL 2.1 |
| Playwright for Python 1.60, `@playwright/test` / Playwright 1.58.2 | Apache-2.0 |
| Chromium (Chrome for Testing builds, downloaded by Playwright) | BSD-3-Clause and others |
| numpy, scikit-image | BSD-3-Clause |
| opencv-python-headless | Apache-2.0 (OpenCV) / MIT (packaging) |
| boto3 | Apache-2.0 |
| TypeScript | Apache-2.0 |
| tzdata | Apache-2.0 |
