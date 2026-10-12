#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WA(폴란드) 법인의 시간별 기온·습도 수집 — 실시간 전력 예측모델의 외생요인용.

기존 fetch_weather.py는 월별 집계(HDD/CDD 등 회귀모델 입력)만 만들고 시간별 원값은 버린다.
realtime_peak(15분 수요전력 예측)은 더 세밀한 시간별 기온·습도가 필요해서 별도 스크립트로
둔다 — 같은 리포·같은 Open-Meteo Archive API를 그대로 쓰되, 집계하지 않고 시간별로 저장.

⚠️ [확인 필요] 타임존 가정: EUM 전력 계측 타임스탬프가 폴란드 현지시(Europe/Warsaw, 서머타임
포함)로 기록된다고 가정하고 이 스크립트도 timezone=Europe/Warsaw로 받는다. 실제로 UTC로
기록되는 설비라면 여기 timezone 값을 "UTC"로 바꿔야 한다 — realtime_peak 쪽과 맞춰볼 것.

산출: data/weather_hourly_wa.csv (ts, temp_c, rh_pct, ah_gkg)
사용: python fetch_weather_hourly.py [--start 2026-06-01] [--end auto]
주의: GitHub Actions에서 실행(requests 필요). 사내망 오프라인 로컬에선 실행 불가 — 문법검증만 가능.
"""
import json, csv, os, argparse, math, time, datetime as dt

try:
    import requests
except ImportError:
    requests = None

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HERE = os.path.dirname(os.path.abspath(__file__))
TIMEZONE = "Europe/Warsaw"   # [확인 필요] — 위 docstring 참조


def abs_humidity_gkg(t_c, rh_pct, p_hpa=1013.25):
    if t_c is None or rh_pct is None:
        return None
    es = 6.112 * math.exp(17.67 * t_c / (t_c + 243.5))
    e = max(0.0, rh_pct) / 100.0 * es
    if p_hpa - e <= 0:
        return None
    return 0.622 * e / (p_hpa - e) * 1000.0


def _get_json(params, tries=3, timeout=60):
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


def fetch_hourly(lat, lon, start, end):
    """연 단위로 나눠 시간별 기온·상대습도를 받는다(타임아웃 방지). 반환: [(ts_iso, t_c, rh_pct, ah_gkg), ...]."""
    out = []
    y0, y1 = int(start[:4]), int(end[:4])
    for yr in range(y0, y1 + 1):
        s = max(start, f"{yr}-01-01")
        e = min(end, f"{yr}-12-31")
        params = {
            "latitude": lat, "longitude": lon,
            "start_date": s, "end_date": e,
            "hourly": "temperature_2m,relative_humidity_2m",
            "timezone": TIMEZONE,
            "models": "era5_seamless",  # ERA5-Land(~9km) 우선, 없으면 ERA5(~25km)
        }
        j = _get_json(params)
        h = j.get("hourly", {})
        for ts, t, rh in zip(h.get("time", []), h.get("temperature_2m", []),
                             h.get("relative_humidity_2m", [])):
            ah = abs_humidity_gkg(t, rh)
            out.append((ts, t, rh, round(ah, 3) if ah is not None else ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--plant", default="WA")
    args = ap.parse_args()
    if requests is None:
        raise SystemExit("requests 미설치 — GitHub Actions에서 실행하세요(로컬 사내망은 오프라인).")

    cfg = json.load(open(os.path.join(HERE, "plants.json"), encoding="utf-8"))
    pl = next((p for p in cfg["plants"] if p["code"] == args.plant), None)
    if pl is None:
        raise SystemExit(f"plants.json에 code={args.plant} 없음")

    end = args.end or dt.date.today().isoformat()
    print(f"[{pl['code']}] {pl['name']} 시간별 수집 {args.start}~{end} (tz={TIMEZONE}) ...", flush=True)
    rows = fetch_hourly(pl["lat"], pl["lon"], args.start, end)

    outdir = os.path.join(HERE, "data")
    os.makedirs(outdir, exist_ok=True)
    out_path = os.path.join(outdir, f"weather_hourly_{args.plant.lower()}.csv")
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ts", "temp_c", "rh_pct", "ah_gkg"])
        for ts, t, rh, ah in rows:
            w.writerow([ts, t, rh, ah])

    print(f"완료: {out_path} ({len(rows)}행)")


if __name__ == "__main__":
    main()
