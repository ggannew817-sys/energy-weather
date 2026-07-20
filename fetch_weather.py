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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    if requests is None:
        raise SystemExit("requests 미설치 — GitHub Actions에서 실행하세요(로컬 사내망은 오프라인).")

    cfg = json.load(open(os.path.join(HERE, "plants.json"), encoding="utf-8"))
    base_hdd = cfg["_base_hdd_C"]; base_cdd = cfg["_base_cdd_C"]; base_ldd = cfg["_base_ldd_gkg"]
    end = args.end or last_full_month_end()

    FIELDS = ["t_mean", "hdd_15", "hdd_18", "cdd_18", "cdd_22", "ah_mean", "ldd"]
    monthly_rows = []
    normals = defaultdict(lambda: defaultdict(list))  # code -> mm -> {field:[..]}

    for pl in cfg["plants"]:
        code = pl["code"]
        print(f"[{code}] {pl['name']} 수집 {args.start}~{end} ...", flush=True)
        daily = fetch_daily(pl["lat"], pl["lon"], args.start, end)
        feats = monthly_features(daily, base_hdd, base_cdd, base_ldd)
        for ym in sorted(feats):
            row = {"plant": code, "month": ym}
            row.update(feats[ym])
            monthly_rows.append(row)
            mm = ym[5:7]
            for f in FIELDS:
                val = feats[ym][f]
                if val != "":
                    normals[code][mm].append((f, val))

    outdir = os.path.join(HERE, "data")   # 리포 루트(weather-service) 내부에 산출
    os.makedirs(outdir, exist_ok=True)

    with open(os.path.join(outdir, "weather_monthly.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["plant", "month"] + FIELDS)
        w.writeheader(); w.writerows(monthly_rows)

    # 평년값: 법인×월(01~12) 평균
    with open(os.path.join(outdir, "weather_normals.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["plant", "mm"] + FIELDS)
        w.writeheader()
        for code in [p["code"] for p in cfg["plants"]]:
            for mm in [f"{i:02d}" for i in range(1, 13)]:
                bucket = defaultdict(list)
                for fld, val in normals[code].get(mm, []):
                    bucket[fld].append(val)
                row = {"plant": code, "mm": mm}
                for fld in FIELDS:
                    row[fld] = round(sum(bucket[fld]) / len(bucket[fld]), 3) if bucket[fld] else ""
                w.writerow(row)

    print(f"\n완료: weather_monthly.csv {len(monthly_rows)}행, weather_normals.csv 생성 ({outdir})")


if __name__ == "__main__":
    main()
