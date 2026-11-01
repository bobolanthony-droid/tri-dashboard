# Triathlon training dashboard

This repo builds a daily dashboard from intervals.icu for an Olympic-distance triathlon (target: mid-April 2027, goal under 3:00:00). It runs on GitHub Actions and is published on GitHub Pages as a **password-encrypted** page.

- `sync.py`: incremental pull from the intervals.icu API into `data/`. The first run pulls the full history.
- `build.py`: derives the metrics, encrypts them with AES-GCM, and writes `site/index.html` from `template.html`. Athlete-specific rules are constants at the top of the file.
- `cache_crypt.py`: encrypts `data/` for the Actions cache. No training data is ever committed.
- `.github/workflows/daily.yml`: runs daily at 05:00 UTC, and can also be started by hand from the Actions tab.

The workflow needs three secrets: `INTERVALS_ATHLETE_ID`, `INTERVALS_API_KEY` and `DASHBOARD_PASSWORD`. For local runs, put the same three values in `.env` (git-ignored), then run:

```
pip install -r requirements.txt
python sync.py
python build.py
```
