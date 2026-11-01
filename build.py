"""Build site/index.html from ./data: derive metrics, encrypt them, inline into template.html.

All athlete-specific rules live in the constants below.
"""
import base64
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "site" / "index.html"

# --- Athlete rules -----------------------------------------------------------
RACE_DATE = date(2027, 4, 15)           # "mid-April 2027"
GOAL_SECS = 3 * 3600
LAST_SPRINT = "1:28"
FTP_W = 176                             # Zwift ramp test
FTP_FROM = "2026-10-05"                 # older FTP values are ignored
COMBINED_RACE_DATE = "2026-09-13"       # sprint tri logged as one 26.7 km "run"
LONG_RUN_M = 6000                       # runs at/above this count as "longer"
SWIM_REST_FACTOR = 1.35                 # a length slower than 1.35x median includes rest
PBKDF2_ITER = 310_000

SPORTS = {
    "Swim": "Swim", "OpenWaterSwim": "Swim",
    "Ride": "Bike", "VirtualRide": "Bike", "MountainBikeRide": "Bike", "GravelRide": "Bike",
    "TrackRide": "Bike", "Cyclocross": "Bike",
    "Run": "Run", "VirtualRun": "Run", "TrailRun": "Run",
    "Padel": "Padel",
}
EXCLUDED_TYPES = {"WeightTraining"}     # strength is not logged


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def env(name):
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return os.environ.get(name, "").strip()


def is_combined_race(a):
    return (a["start_date_local"][:10] == COMBINED_RACE_DATE and a.get("type") == "Run"
            and (a.get("distance") or 0) > 20000)


def swim_lengths(iv):
    return [x for x in (iv or {}).get("icu_intervals") or []
            if (x.get("distance") or 0) > 0 and (x.get("moving_time") or 0) > 0]


def swim_set_pace(a, iv):
    """Pace per 100 m from swimming lengths only; lengths that contain wall rest are dropped."""
    dist, mov = a.get("distance") or 0, a.get("moving_time") or 0
    avg_incl_rest = (a.get("elapsed_time") or mov) / dist * 100 if dist else None
    if a["type"] == "OpenWaterSwim":
        return {"set": mov / dist * 100 if dist else None, "avg": avg_incl_rest, "kept": None, "dropped": 0, "ow": True}
    lens = swim_lengths(iv)
    if not lens:
        return {"set": None, "avg": avg_incl_rest, "kept": 0, "dropped": 0, "ow": False}
    med = statistics.median(x["moving_time"] for x in lens)
    kept = [x for x in lens if x["moving_time"] <= SWIM_REST_FACTOR * med]
    d = sum(x["distance"] for x in kept)
    t = sum(x["moving_time"] for x in kept)
    return {"set": t / d * 100 if d else None, "avg": avg_incl_rest, "kept": len(kept),
            "dropped": len(lens) - len(kept), "ow": False}


def css_from_test(a, iv, pool):
    """Find the 400 and 200 as continuous blocks of rest-free lengths; CSS = (T400 - T200) / 2."""
    lens = swim_lengths(iv)
    if len(lens) < 16 or not pool:
        return None
    med = statistics.median(x["moving_time"] for x in lens)
    blocks, cur = [], []
    for x in lens:
        if x["moving_time"] <= SWIM_REST_FACTOR * med:
            cur.append(x["moving_time"])
        elif cur:
            blocks.append(cur); cur = []
    if cur:
        blocks.append(cur)
    n400, n200 = 400 / pool, 200 / pool
    i400 = max(range(len(blocks)), key=lambda i: len(blocks[i]))
    b400 = blocks[i400]
    after = [b for b in blocks[i400 + 1:] if n200 * 0.6 <= len(b) <= n200 * 1.4]
    if len(b400) < n400 * 0.75 or not after:
        return None
    b200 = max(after, key=len)
    t400 = statistics.mean(b400) * n400
    t200 = statistics.mean(b200) * n200
    if t400 <= t200:
        return None
    return {"date": a["start_date_local"][:10], "id": a["id"], "t400": round(t400), "t200": round(t200),
            "css": (t400 - t200) / 2, "n400": len(b400), "n200": len(b200)}


def bike_speed(watts, mass, cda=0.32, crr=0.005, rho=1.2, eta=0.97):
    lo, hi = 1.0, 20.0
    for _ in range(60):
        v = (lo + hi) / 2
        p = (0.5 * rho * cda * v ** 3 + crr * mass * 9.81 * v) / eta
        lo, hi = (v, hi) if p < watts else (lo, v)
    return v


def riegel(t, d_from, d_to, k=1.06):
    return t * (d_to / d_from) ** k


def main():
    acts_raw = load("activities.json", [])
    wellness = load("wellness.json", [])
    athlete = load("athlete.json", {})
    curves = load("curves.json", {})
    events = load("events.json", [])
    state = load("sync_state.json", {})
    password = env("DASHBOARD_PASSWORD")
    if not password:
        sys.exit("Missing DASHBOARD_PASSWORD")
    today = date.today().isoformat()

    swim_set = next((s for s in athlete.get("sportSettings", []) if "Swim" in (s.get("types") or [])), {})
    thr_pace = 100 / swim_set["threshold_pace"] if swim_set.get("threshold_pace") else None
    weight = athlete.get("icu_weight") or athlete.get("weight")

    acts, flags, css_tests = [], [], []
    combined = None
    race_day_legs = []
    css_event_acts = {e.get("paired_activity_id") for e in events if "css" in (e.get("name") or "").lower()}
    old_ftp_rides = 0

    for a in acts_raw:
        t = a.get("type")
        if t in EXCLUDED_TYPES:
            continue
        d = a["start_date_local"][:10]
        if a.get("_note"):
            flags.append({"d": d, "kind": "Strava stub", "msg": f"{a.get('name', 'Activity')}: no data via API (Strava import)"})
            continue
        if is_combined_race(a):
            combined = a
            continue
        sport = SPORTS.get(t, "Other")
        hr = a.get("average_heartrate") or None
        iv = load(f"intervals/{a['id']}.json", {})
        row = {
            "id": a["id"], "d": d, "time": a["start_date_local"][11:16], "type": t, "sport": sport,
            "name": a.get("name") or t, "dist": a.get("distance"), "mov": a.get("moving_time") or 0,
            "el": a.get("elapsed_time"), "load": a.get("icu_training_load"), "hr": hr,
            "w": a.get("icu_average_watts"), "np": a.get("icu_weighted_avg_watts"),
            "ef": a.get("icu_efficiency_factor"), "dec": a.get("decoupling"), "src": a.get("source"),
            "race": d == COMBINED_RACE_DATE,
        }
        if row["race"]:
            race_day_legs.append((a, row))
        if sport == "Bike" and a.get("icu_ftp") and d < FTP_FROM:
            old_ftp_rides += 1
        if sport == "Swim":
            row["swim"] = swim_set_pace(a, iv)
            if a["id"] in css_event_acts or "css" in (a.get("name") or "").lower():
                c = css_from_test(a, iv, a.get("pool_length"))
                if c:
                    css_tests.append(c)
        if sport == "Run" and row["dist"]:
            row["pace"] = row["mov"] / row["dist"] * 1000
            row["long"] = row["dist"] >= LONG_RUN_M
            if hr:
                row["mpb"] = (row["dist"] / row["mov"]) * 60 / hr  # metres per heartbeat
        acts.append(row)

        # Data-quality flags
        name = row["name"]
        if row["src"] == "MANUAL":
            flags.append({"d": d, "kind": "Manual entry", "msg": f"{name}: no file, so no HR, power or intervals"})
        elif not hr and sport in ("Swim", "Bike", "Run", "Padel"):
            flags.append({"d": d, "kind": "Missing HR", "msg": f"{name}: no heart-rate data"})
        elif hr and hr < 100 and row["mov"] >= 1200 and sport in ("Bike", "Run"):
            flags.append({"d": d, "kind": "Suspect HR", "msg": f"{name}: avg HR {hr:.0f} looks too low (strap or optical dropout?)"})
        if sport == "Bike" and not row["w"] and row["src"] != "MANUAL":
            flags.append({"d": d, "kind": "No power", "msg": f"{name}: no power (outdoor ride without a power meter)"})
        if row["load"] is None and row["src"] != "MANUAL":
            flags.append({"d": d, "kind": "No load", "msg": f"{name}: no training load computed"})

    if combined:
        flags.append({"d": COMBINED_RACE_DATE, "kind": "Race file",
                      "msg": f"{combined.get('name')} ({combined['distance'] / 1000:.1f} km) is the whole sprint tri logged as one run. "
                             "Excluded from hours and run stats; the separate swim, bike and run leg files are used instead. "
                             "It also pollutes intervals.icu's run pace curve - consider changing its type there."})
    if old_ftp_rides:
        flags.append({"d": FTP_FROM, "kind": "Old FTP",
                      "msg": f"{old_ftp_rides} ride(s) before {FTP_FROM} carry an old FTP (250 W); ignored, {FTP_W} W used from {FTP_FROM}"})

    well_rows = []
    for w in wellness:
        eftp = next((s.get("eftp") for s in (w.get("sportInfo") or []) if s.get("type") == "Ride"), None)
        well_rows.append({"d": w["id"], "ctl": w.get("ctl"), "atl": w.get("atl"), "rhr": w.get("restingHR"),
                          "sleep": (w["sleepSecs"] / 3600) if w.get("sleepSecs") else None,
                          "sleepScore": w.get("sleepScore"), "eftp": eftp})
        if w["id"] < today and not w.get("restingHR"):
            flags.append({"d": w["id"], "kind": "Missing wellness", "msg": "No resting HR recorded"})
        if w["id"] < today and not w.get("sleepSecs"):
            flags.append({"d": w["id"], "kind": "Missing wellness", "msg": "No sleep recorded"})

    # --- Plan compliance against the real intervals.icu calendar -------------
    by_day_sport = {}
    for r in acts:
        by_day_sport.setdefault((r["d"], r["sport"]), []).append(r["id"])
    used = set()
    plan, excluded_strength = [], 0
    for e in sorted((e for e in events if e.get("category") == "WORKOUT"), key=lambda e: e["start_date_local"]):
        if e.get("type") in EXCLUDED_TYPES:
            excluded_strength += 1
            continue
        d = e["start_date_local"][:10]
        sport = SPORTS.get(e.get("type"), "Other")
        status, act = None, e.get("paired_activity_id")
        if act:
            status = "done"
        else:
            for dd in (d,):
                cand = [i for i in by_day_sport.get((dd, sport), []) if i not in used]
                if cand:
                    status, act = "done (unpaired)", cand[0]
            if not status:
                for off in (-1, 1):
                    dd = (date.fromisoformat(d) + timedelta(days=off)).isoformat()
                    cand = [i for i in by_day_sport.get((dd, sport), []) if i not in used]
                    if cand and dd <= today:
                        status, act = f"moved ({'day before' if off < 0 else 'day after'})", cand[0]
                        break
        if act:
            used.add(act)
        if not status:
            status = "upcoming" if d > today else ("today" if d == today else "missed")
        plan.append({"d": d, "sport": sport, "name": e.get("name") or e.get("type"), "status": status, "act": act})

    # --- Race projection ---------------------------------------------------------
    assumptions = []
    css = css_tests[-1]["css"] if css_tests else thr_pace
    css_src = (f"CSS test {css_tests[-1]['date']} (400 ~{css_tests[-1]['t400'] // 60}:{css_tests[-1]['t400'] % 60:02d}, "
               f"200 ~{css_tests[-1]['t200'] // 60}:{css_tests[-1]['t200'] % 60:02d})") if css_tests else "intervals.icu swim threshold pace"
    swim_pace = css + 4 if css else None
    swim_t = swim_pace * 15 if swim_pace else None
    assumptions.append(f"Swim: 1500 m at CSS + 4 s/100 m. CSS from {css_src}. Wetsuit gain assumed to cancel sighting and the mass start.")

    mass = (weight or 75) + 9
    race_w = 0.80 * FTP_W
    v = bike_speed(race_w, mass)
    bike_t = 40000 / v
    assumptions.append(f"Bike: 40 km flat, no wind, at 80% of FTP {FTP_W} W = {race_w:.0f} W; CdA 0.32 m² (road bike, drops), "
                       f"Crr 0.005, rider {f'{weight:.0f} kg (intervals.icu)' if weight else '75 kg (no weight in intervals.icu)'} + 9 kg bike "
                       f"-> {v * 3.6:.1f} km/h.")

    run_est = []
    legs = {SPORTS.get(a["type"]): (a, r) for a, r in race_day_legs}
    if "Run" in legs and legs["Run"][1]["dist"]:
        r = legs["Run"][1]
        run_est.append(("sprint run leg off the bike", riegel(r["mov"], r["dist"], 10000)))
    fresh = [r for r in acts if r["sport"] == "Run" and not r["race"] and (r["dist"] or 0) >= 4800]
    if fresh:
        best = min(fresh, key=lambda r: r["pace"])
        run_est.append((f"best training run {best['d']} ({best['dist'] / 1000:.1f} km) +4% off-bike",
                        riegel(best["mov"], best["dist"], 10000) * 1.04))
    run_t = statistics.mean(t for _, t in run_est) if run_est else None
    assumptions.append("Run: 10 km = mean of " + "; ".join(f"{n} -> {int(t // 60)}:{int(t % 60):02d}" for n, t in run_est)
                       + ". Scaled with Riegel (exponent 1.06).")

    t1, t2, t1_src, t2_src = 180, 90, "default", "default"
    if {"Swim", "Bike", "Run"} <= legs.keys():
        def start(a): return datetime.fromisoformat(a["start_date_local"][:19])
        s, b, r = legs["Swim"][0], legs["Bike"][0], legs["Run"][0]
        mt1 = (start(b) - start(s)).total_seconds() - (s.get("elapsed_time") or 0)
        mt2 = (start(r) - start(b)).total_seconds() - (b.get("elapsed_time") or 0)
        if 30 < mt1 < 600:
            t1, t1_src = mt1, "gap between your sprint swim and bike files"
        if 20 < mt2 < 400:
            t2, t2_src = mt2, "gap between your sprint bike and run files"
    assumptions.append(f"Transitions: T1 {int(t1 // 60)}:{int(t1 % 60):02d} ({t1_src}), T2 {int(t2 // 60)}:{int(t2 % 60):02d} ({t2_src}).")

    budget = {"Swim": 30 * 60, "T1": 180, "Bike": 80 * 60, "T2": 90, "Run": 55 * 60 + 30}
    proj = {"Swim": swim_t, "T1": t1, "Bike": bike_t, "T2": t2, "Run": run_t}
    assumptions.append("Sub-3:00 budget used for the weakest link: swim 30:00, T1 3:00, bike 1:20:00, T2 1:30, run 55:30.")

    cross = []
    if "Swim" in legs and legs["Swim"][1]["dist"]:
        r = legs["Swim"][1]
        cross.append(f"Sprint swim leg: {r['dist']:.0f} m open water at {int(r['mov'] / r['dist'] * 100 // 60)}:{int(r['mov'] / r['dist'] * 100 % 60):02d}/100 m")
    if "Bike" in legs and legs["Bike"][1]["dist"]:
        r = legs["Bike"][1]
        cross.append(f"Sprint bike leg: {r['dist'] / 1000:.1f} km at {r['dist'] / r['mov'] * 3.6:.1f} km/h (no power recorded); "
                     f"at that speed 40 km = {int(40000 / (r['dist'] / r['mov']) // 60)} min")
    sens = (40000 / bike_speed(race_w + 10, mass) - bike_t)
    cross.append(f"Bike sensitivity: +10 W race power changes the bike split by {sens / 60:+.1f} min")

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "today": today,
        "lastSync": state.get("last_run"),
        "race": {"date": RACE_DATE.isoformat(), "goal": GOAL_SECS, "lastSprint": LAST_SPRINT},
        "ftp": {"w": FTP_W, "from": FTP_FROM},
        "longRunM": LONG_RUN_M,
        "acts": acts, "well": well_rows, "plan": plan, "planExcludedStrength": excluded_strength,
        "css": {"tests": css_tests, "threshold": thr_pace},
        "curves": {k: [{"id": c.get("id"), "label": c.get("label"), "secs": c.get("secs"), "values": c.get("values")}
                       for c in ((curves.get("power_ride") or {}).get("list") or [])] for k in ("power",)},
        "projection": {"legs": proj, "budget": budget, "assumptions": assumptions, "cross": cross},
        "flags": sorted(flags, key=lambda f: f["d"], reverse=True),
    }

    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=PBKDF2_ITER).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(payload, separators=(",", ":")).encode(), None)
    blob = {"salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(iv).decode(),
            "ct": base64.b64encode(ct).decode(), "iter": PBKDF2_ITER}

    html = (ROOT / "template.html").read_text(encoding="utf-8").replace("/*__BLOB__*/null", json.dumps(blob))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    if "--debug-json" in sys.argv:
        (DATA / "payload_debug.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"Built {OUT.relative_to(ROOT)}: {len(acts)} activities, {len(well_rows)} wellness days, "
          f"{len(plan)} planned sessions, {len(flags)} flags, CSS tests {len(css_tests)}")


if __name__ == "__main__":
    main()
