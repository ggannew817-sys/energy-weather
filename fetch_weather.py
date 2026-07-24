#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""법인 위치별 월별 기상 특성치를 Open-Meteo(무료·키불필요)에서 수집.

산출:
  data/weather_monthly.csv  : 과거 월별 실측 기상변수(회귀 학습용)
  data/weather_normals.csv  : 월(1~12)별 평년값(사업계획/미래예측용, 기상중립 기준선)

변수:
  t_mean(°C), hdd_15/hdd_18(난방도일), cdd_18/cdd_22(냉방도일),
  ah_mean(g/kg, 절대습도), ldd(잠열도일 = Σmax(0, AH-base)) ← 배터리 드라이룸 제습부하 대리

주의: GitHub Actions에서 실행(requests 필요). 사내망 오프라인 로컬에선 실행 불가 — 문법검증만 가능.
사용: python fetch_weather.py [--start 2022-01-01] [--end auto]
"""
import json, csv, os, argparse, math, time, datetime as dt
from collections import defaultdict

try:
    import requests
except ImportError:
    requests = None

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HERE = os.path.dirname(os.path.abspath(__file__))


def abs_humidity_gkg(t_c, rh_pct, p_hpa=1013.25):
    """건구온도(°C)·상대습도(%) → 절대습도(혼합비, g/kg 건공기)."""
    if t_c is None or rh_pct is None:
        return None
    es = 6.112 * math.exp(17.67 * t_c / (t_c + 243.5))   # 포화수증기압 hPa
    e = max(0.0, rh_pct) / 100.0 * es                    # 수증기압 hPa
    if p_hpa - e <= 0:
        return None
    w = 0.622 * e / (p_hpa - e)                           # kg/kg
    return w * 1000.0                                     # g/kg


def _get_json(params, tries=3, timeout=60):
    """일시적 오류(타임아웃 등) 재시도 포함 GET."""
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(ARCHIVE, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as ex:  # noqa
            last = ex
            time.sleep(3 * (attempt + 1))
    raise last


def fetch_daily(lat, lon, start, end):
    """연 단위로 나눠 시간별(기온·상대습도)을 받아 일별 평균기온·절대습도로 집계.
    반환 {date: [t_mean, ah_mean]}. (요청 분할로 타임아웃 방지)"""
    out = {}
    y0, y1 = int(start[:4]), int(end[:4])
    for yr in range(y0, y1 + 1):
        s = max(start, f"{yr}-01-01")
        e = min(end, f"{yr}-12-31")
        params = {
            "latitude": lat, "longitude": lon,
            "start_date": s, "end_date": e,
            "hourly": "temperature_2m,relative_humidity_2m",
            "timezone": "UTC",
        }
        j = _get_json(params)
        h = j.get("hourly", {})
        tacc, aacc = defaultdict(list), defaultdict(list)
        for ts, t, rh in zip(h.get("time", []), h.get("temperature_2m", []),
                             h.get("relative_humidity_2m", [])):
            day = ts[:10]
            if t is not None:
                tacc[day].append(t)
            ah = abs_humidity_gkg(t, rh)
            if ah is not None:
                aacc[day].append(ah)
        for day in tacc:
            tmean = sum(tacc[day]) / len(tacc[day]) if tacc[day] else None
            amean = sum(aacc[day]) / len(aacc[day]) if aacc[day] else None
            out[day] = [tmean, amean]
    return out


def monthly_features(daily, base_hdd, base_cdd, base_ldd):
    """일별 → 월별 특성치 집계."""
    agg = defaultdict(lambda: {"t": [], "ah": []})
    for day, (t, ah) in daily.items():
        ym = day[:7]
        if t is not None:
            agg[ym]["t"].append(t)
        if ah is not None:
            agg[ym]["ah"].append(ah)
    rows = {}
    for ym, v in agg.items():
        ts, ahs = v["t"], v["ah"]
        if not ts:
            continue
        rows[ym] = {
            "t_mean": round(sum(ts) / len(ts), 3),
            "hdd_15": round(sum(max(0.0, 15 - t) for t in ts), 1),
            "hdd_18": round(sum(max(0.0, base_hdd - t) for t in ts), 1),
            "cdd_18": round(sum(max(0.0, t - base_cdd) for t in ts), 1),
            "cdd_22": round(sum(max(0.0, t - 22) for t in ts), 1),
            "ah_mean": round(sum(ahs) / len(ahs), 3) if ahs else "",
            "ldd": round(sum(max(0.0, a - base_ldd) for a in ahs), 1) if ahs else "",
        }
    return rows


def last_full_month_end():
    today = dt.date.today()
    first = today.replace(day=1)
    last_prev = first - dt.timedelta(days=1)
    return last_prev.isoformat()


FIELDS = ["t_mean", "hdd_15", "hdd_18", "cdd_18", "cdd_22", "ah_mean", "ldd"]


def load_cached_monthly(path):
    """레포에 커밋돼 있던 기존 weather_monthly.csv → {(plant,ym):{field:val}}."""
    rows = {}
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            rows[(r["plant"], r["month"])] = {f: r.get(f, "") for f in FIELDS}
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")            # 최초(캐시 없을 때) 전체 시작
    ap.add_argument("--end", default=None)
    ap.add_argument("--refresh-from", dest="refresh_from", default=None,
                    help="이 월(YYYY-MM)부터만 새로 수집, 이전은 캐시 재사용. 기본=올해 01월")
    ap.add_argument("--full", action="store_true", help="캐시 무시하고 전체 재수집")
    args = ap.parse_args()
    if requests is None:
        raise SystemExit("requests 미설치 — GitHub Actions에서 실행하세요(로컬 사내망은 오프라인).")

    cfg = json.load(open(os.path.join(HERE, "plants.json"), encoding="utf-8"))
    base_hdd = cfg["_base_hdd_C"]; base_cdd = cfg["_base_cdd_C"]; base_ldd = cfg["_base_ldd_gkg"]
    today = dt.date.today()
    end = args.end or today.isoformat()                        # 이번 달 부분치까지 포함
    refresh_from = args.refresh_from or f"{today.year}-01"      # 올해분만 매일 재수집

    outdir = os.path.join(HERE, "data")
    os.makedirs(outdir, exist_ok=True)
    monthly_path = os.path.join(outdir, "weather_monthly.csv")

    cached = {} if args.full else load_cached_monthly(monthly_path)
    use_cache = len(cached) > 0
    fetch_start = (refresh_from + "-01") if use_cache else args.start

    merged = {}   # (code, ym) -> {field:val}
    if use_cache:
        for (code, ym), vals in cached.items():
            if ym < refresh_from:                              # 과거(안 변함)는 캐시 보존
                merged[(code, ym)] = vals
        print(f"[증분] 캐시 {len(merged)}행 보존(<{refresh_from}) · {refresh_from}~{end[:7]}만 재수집", flush=True)
    else:
        print(f"[전체] 캐시 없음 → {fetch_start}~{end} 전체 수집(최초 1회 오래 걸림)", flush=True)

    for pl in cfg["plants"]:
        code = pl["code"]
        print(f"[{code}] {pl['name']} 수집 {fetch_start}~{end} ...", flush=True)
        daily = fetch_daily(pl["lat"], pl["lon"], fetch_start, end)
        feats = monthly_features(daily, base_hdd, base_cdd, base_ldd)
        for ym in sorted(feats):                               # 새로 받은 달로 갱신/추가
            merged[(code, ym)] = {f: feats[ym][f] for f in FIELDS}

    # weather_monthly.csv (병합 결과)
    with open(monthly_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["plant", "month"] + FIELDS)
        w.writeheader()
        for (code, ym) in sorted(merged, key=lambda k: (k[0], k[1])):
            row = {"plant": code, "month": ym}; row.update(merged[(code, ym)])
            w.writerow(row)

    # 평년값: 법인×월(01~12) 평균 — 병합 전체(과거+최신)에서 계산
    normals = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for (code, ym), vals in merged.items():
        mm = ym[5:7]
        for fld in FIELDS:
            v = vals.get(fld, "")
            if v not in ("", None):
                try: normals[code][mm][fld].append(float(v))
                except (TypeError, ValueError): pass
    with open(os.path.join(outdir, "weather_normals.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["plant", "mm"] + FIELDS)
        w.writeheader()
        for code in [p["code"] for p in cfg["plants"]]:
            for mm in [f"{i:02d}" for i in range(1, 13)]:
                row = {"plant": code, "mm": mm}
                for fld in FIELDS:
                    b = normals[code][mm][fld]
                    row[fld] = round(sum(b) / len(b), 3) if b else ""
                w.writerow(row)

    print(f"\n완료: weather_monthly.csv {len(merged)}행, weather_normals.csv 생성 ({outdir})")


if __name__ == "__main__":
    main()
