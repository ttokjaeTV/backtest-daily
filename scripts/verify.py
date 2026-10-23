#!/usr/bin/env python3
"""
일별 데이터 검증 — portfolio-sheet-data 의 월별 자료와 대조한다.

  가격   각 달 마지막 거래일 종가 = 월별 파일의 그달 종가 (0.1% 이내)
           국내 ETF → kr_etf_krx/(KRX 실제 종가) · 국내 주식 → kr_stock/(네이버) · 미국 → us/(야후)
  배당   미국: 월별 합계 = data/bt/div/U · 국내: 연도별 합계 = data/bt/div/K (배당락일과 기준일의 달이 다를 수 있어 연 단위)
  환율   각 달 마지막 날 매매기준율 = usdkrw_month.csv
  빈 구간 거래일 사이가 12일 넘게 벌어진 곳 (긴 연휴·거래정지 확인용)

사용: python scripts/verify.py        (끝난 달만 비교한다. 문제가 있으면 종료 코드 1)
"""

import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PSD, SITE, div_path, load_universe, log, now_kst, price_path, read_csv, read_series, to_filename  # noqa: E402

H = os.path.join(PSD, "data", "history")
BT = os.path.join(PSD, "data", "bt", "div")
TOL = 0.001


def month_end(daily):
    out = {}
    for d in sorted(daily):
        out[d[:7]] = float(daily[d])
    return out


def monthly(path, col="종가"):
    out = {}
    for r in read_csv(path):
        try:
            out[r["월"]] = float(r[col])
        except (KeyError, ValueError):
            pass
    return out


def main():
    cur = now_kst().strftime("%Y-%m")
    stat = defaultdict(lambda: [0, 0, 0])          # 구분 → [종목, 비교한 달, 다른 달]
    worst, gaps, dstat, dbad = [], [], defaultdict(lambda: [0, 0]), []
    size = 0
    for r in load_universe():
        key, kind = r["키"], r["구분"]
        mk, code = key.split(":", 1)
        path = price_path(key)
        if not os.path.exists(path):
            stat[mk + kind][0] += 0
            worst.append((9, key, "파일 없음"))
            continue
        size += os.path.getsize(path)
        px = read_series(path)
        ref = {"KE": os.path.join(H, "kr_etf_krx", f"{code}.csv"),
               "KS": os.path.join(H, "kr_stock", f"{code}.csv"),
               "UE": os.path.join(H, "us", f"{to_filename(code)}.csv"),
               "US": os.path.join(H, "us", f"{to_filename(code)}.csv")}[mk + kind]
        me, mo = month_end(px), monthly(ref)
        s = stat[mk + kind]
        s[0] += 1
        first = min(px)[:7]
        for m, v in mo.items():
            # 첫 달은 월별 쪽이 상장 첫날부터가 아닐 수 있어 뺀다
            if m >= cur or m not in me or m == first:
                continue
            s[1] += 1
            diff = me[m] / v - 1 if v else 0
            if abs(diff) > TOL:
                s[2] += 1
                worst.append((abs(diff), key, f"{m} 일별 {me[m]:g} vs 월별 {v:g}"))
        ds = sorted(px)
        for a, b in zip(ds, ds[1:]):
            if (date.fromisoformat(b) - date.fromisoformat(a)).days > 12:
                gaps.append(f"{key} {a}~{b}")

        # 배당
        dp = div_path(key)
        if os.path.exists(dp):
            size += os.path.getsize(dp)
        dv = {d: float(v) for d, v in read_series(dp).items()}
        bt = {r2["월"]: float(r2["금액"]) for r2 in read_csv(os.path.join(BT, mk, f"{to_filename(code)}.csv"))}
        unit = (lambda d: d[:7]) if mk == "U" else (lambda d: d[:4])
        a, b = defaultdict(float), defaultdict(float)
        for d, v in dv.items():
            if d[:7] < cur:
                a[unit(d)] += v
        for m, v in bt.items():
            if m < cur:
                b[unit(m)] += v
        for k in set(a) | set(b):
            if k <= min(px)[:len(k)]:
                continue                         # 가격 첫 기간(과 그 이전) 배당은 일부러 뺀다 — 배당락일을 정할 거래일이 없다
            dstat[mk][0] += 1
            if abs(a[k] - b[k]) > max(1e-6, 0.005 * max(a[k], b[k])):
                dstat[mk][1] += 1
                dbad.append(f"{key} {k} 일별 {a[k]:.6g} vs 월별 {b[k]:.6g}")

    fx = read_series(os.path.join(SITE, "fx.csv"))
    size += os.path.getsize(os.path.join(SITE, "fx.csv")) if fx else 0
    fme = month_end(fx)
    fmo = monthly(os.path.join(H, "usdkrw_month.csv"), "매매기준율")
    fbad = [m for m, v in fmo.items() if m in fme and m < cur and abs(fme[m] / v - 1) > TOL]

    log("가격 (각 달 마지막 거래일 vs 월별 파일, 0.1% 넘게 다르면 불일치)")
    names = {"KE": "국내 ETF (KRX 실제 종가)", "KS": "국내 주식 (네이버)", "UE": "미국 ETF", "US": "미국 주식"}
    for k in ("KE", "KS", "UE", "US"):
        n, c, bad = stat[k]
        log(f"  {names[k]}: {n}종목 · {c}개월 비교 · 불일치 {bad} ({bad / c * 100 if c else 0:.3f}%)")
    worst.sort(reverse=True)
    for w in worst[:15]:
        log(f"    {w[1]} {w[2]}" + (f" ({w[0] * 100:+.2f}%)" if w[0] < 9 else ""))
    log(f"배당 (미국 월별 · 국내 연별 합계): 미국 {dstat['U'][0]}건 중 불일치 {dstat['U'][1]} · "
        f"국내 {dstat['K'][0]}건 중 불일치 {dstat['K'][1]}")
    for x in dbad[:15]:
        log(f"    {x}")
    log(f"환율: {len(fx)}일 · 월말 비교 {len([m for m in fmo if m in fme and m < cur])}개월 · 불일치 {len(fbad)} {fbad[:5]}")
    log(f"12일 넘는 빈 구간: {len(gaps)}곳 " + " | ".join(gaps[:12]))
    log(f"용량: 가격·배당·환율 {size / 1e6:.0f}MB")
    total = sum(s[1] for s in stat.values())
    nbad = sum(s[2] for s in stat.values())
    if (total and nbad > total * 0.005) or fbad or any(w[0] >= 9 for w in worst):
        sys.exit(1)


if __name__ == "__main__":
    main()
