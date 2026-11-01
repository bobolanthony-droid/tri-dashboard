"""Incremental sync of intervals.icu data into ./data as JSON.

First run pulls the full history; later runs refetch only from the last
synced day (minus a small overlap to catch edits and late uploads).
Credentials come from environment variables or a local .env file and are
never printed.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BASE = "https://intervals.icu/api/v1"
HISTORY_START = date(2005, 1, 1)
OVERLAP_DAYS = 7


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    athlete = os.environ.get("INTERVALS_ATHLETE_ID", "").strip()
    key = os.environ.get("INTERVALS_API_KEY", "").strip()
    if not athlete or not key:
        sys.exit("Missing INTERVALS_ATHLETE_ID or INTERVALS_API_KEY (set them in .env or as secrets).")
    return athlete, key


class Api:
    def __init__(self, athlete, key):
        self.athlete = athlete
        token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
        # A non-default User-Agent: the API's firewall rejects "Python-urllib".
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json",
                        "User-Agent": "tri-dashboard-sync/1.0"}
        self.calls = 0

    def get(self, path, **params):
        path = path.replace("{a}", self.athlete)
        params = {k: v for k, v in params.items() if v is not None}
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(5):
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self.calls += 1
                    body = r.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** attempt * 2)
                    continue
                if e.code == 404:
                    return None
                # Never echo the URL: it contains the athlete ID.
                raise SystemExit(f"API error {e.code} on {path.replace(self.athlete, '<athlete>')}") from None
            except urllib.error.URLError:
                time.sleep(2 ** attempt * 2)
        raise SystemExit("API unreachable after retries")


def read_json(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


def year_chunks(start, end):
    cur = start
    while cur <= end:
        nxt = min(date(cur.year, 12, 31), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def main():
    athlete, key = load_env()
    api = Api(athlete, key)
    state_p = DATA / "sync_state.json"
    state = read_json(state_p, {})
    today = date.today()
    first_run = "last_synced" not in state
    start = HISTORY_START if first_run else date.fromisoformat(state["last_synced"]) - timedelta(days=OVERLAP_DAYS)
    print(f"{'Full' if first_run else 'Incremental'} sync from {start} to {today}")

    # Athlete settings (FTP, zones, sport settings)
    write_json(DATA / "athlete.json", api.get("/athlete/{a}"))

    # Activities, merged by id
    acts_p = DATA / "activities.json"
    acts = {a["id"]: a for a in read_json(acts_p, [])}
    new_ids = []
    for lo, hi in year_chunks(start, today):
        chunk = api.get("/athlete/{a}/activities", oldest=lo.isoformat(), newest=hi.isoformat()) or []
        for a in chunk:
            if a["id"] not in acts:
                new_ids.append(a["id"])
            acts[a["id"]] = a
    write_json(acts_p, sorted(acts.values(), key=lambda a: a.get("start_date_local", "")))

    # Intervals: once per activity, refetched for activities inside the overlap window
    int_dir = DATA / "intervals"
    recent_cut = (today - timedelta(days=OVERLAP_DAYS)).isoformat()
    fetched = 0
    for a in acts.values():
        p = int_dir / f"{a['id']}.json"
        recent = a.get("start_date_local", "") >= recent_cut
        if a.get("_note") or (p.exists() and not recent):
            continue  # Strava stubs have no data
        write_json(p, api.get(f"/activity/{a['id']}/intervals") or {})
        fetched += 1
        time.sleep(0.15)

    # Wellness, merged by date
    well_p = DATA / "wellness.json"
    well = {w["id"]: w for w in read_json(well_p, [])}
    for lo, hi in year_chunks(start, today):
        for w in api.get("/athlete/{a}/wellness", oldest=lo.isoformat(), newest=hi.isoformat()) or []:
            well[w["id"]] = w
    write_json(well_p, sorted(well.values(), key=lambda w: w["id"]))

    # Planned calendar (whole range each run so edits and deletions are picked up)
    write_json(DATA / "events.json", api.get("/athlete/{a}/events", oldest=HISTORY_START.isoformat(),
                                             newest=(today + timedelta(days=240)).isoformat()) or [])

    # Best-effort curves (whole history + last 84 days), refreshed every run
    curves = {
        "power_ride": api.get("/athlete/{a}/power-curves", type="Ride", curves="all,84d"),
        "pace_run": api.get("/athlete/{a}/pace-curves", type="Run", curves="all,84d"),
        "pace_swim": api.get("/athlete/{a}/pace-curves", type="Swim", curves="all,84d"),
    }
    write_json(DATA / "curves.json", curves)

    state.update(last_synced=today.isoformat(), last_run=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    write_json(state_p, state)
    print(f"Activities: {len(acts)} total, {len(new_ids)} new | intervals fetched: {fetched} | "
          f"wellness days: {len(well)} | API calls: {api.calls}")


if __name__ == "__main__":
    main()
