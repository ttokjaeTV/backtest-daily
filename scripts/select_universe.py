#!/usr/bin/env python3
"""
일별 데이터 대상 종목 고르기 → site/universe.csv   (분기마다, 한 번 들어온 종목은 빼지 않는다)

  국내 주식  시가총액 2조 원 이상            또는 최근 60거래일 평균 거래대금 300억 원 이상
  국내 ETF   순자산 1,000억 원 이상           또는 최근 60거래일 평균 거래대금 100억 원 이상
  미국 주식  시가총액 500억 달러 이상         또는 3개월 평균 거래대금 10억 달러 이상
  미국 ETF   순자산 50억 달러 이상            또는 3개월 평균 거래대금 1억 달러 이상

자료
  국내 시총·순자산  portfolio-sheet-data/data/history/krx_{stock,etf}_month/ 의 가장 최근 달 (KRX 월말)
  국내 거래대금      다음(Daum) 일봉 candleAccTradePrice. 시총 3,000억·순자산 300억 이상만 조회한다
                     (2026-10 실측: 그 아래에서 기준을 넘는 종목은 없었다)
  미국              야후 quote (marketCap · netAssets · averageDailyVolume3Month × 가격)
  대상 범위          계산기 검색 목록과 같은 현재 상장 종목 (etf_prices.csv · kr_stocks.csv · us_prices.csv)

★ 기준에서 밀려나도 universe.csv 에서 빼지 않는다. 사용자가 보던 결과가 갑자기 월별로 바뀌지 않게.
★ 야후·다음 응답이 절반 넘게 비면 그 시장은 새로 넣지 않고 오류로 끝낸다.

사용: python scripts/select_universe.py        (DRY=1 이면 저장하지 않고 개수만)
"""

import glob
import http.cookiejar
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PSD, SITE, UA, UNIVERSE, UNIVERSE_COLS, fmt, http_json, load_universe,  # noqa: E402
                    log, now_kst, read_csv, write_rows)

RULES = {   # (규모 기준, 거래대금 기준, 거래대금 조회 하한)
    "KS": (2e12, 3e10, 3e11),
    "KE": (1e11, 1e10, 3e10),
    "US": (5e10, 1e9, None),
    "UE": (5e9, 1e8, None),
}
DRY = os.environ.get("DRY") == "1"
H = os.path.join(PSD, "data", "history")


def latest(folder):
    files = sorted(glob.glob(os.path.join(H, folder, "*.csv")))
    if not files:
        raise SystemExit(f"{folder} 가 없습니다 (PSD={PSD})")
    return files[-1]


def daum_value(code):
    """최근 60거래일 평균 거래대금(원). 실패면 None."""
    try:
        d = http_json(f"https://finance.daum.net/api/charts/A{code}/days?limit=60&adjusted=false",
                      {"Referer": f"https://finance.daum.net/quotes/A{code}"}, tries=3, timeout=20)
        v = [x["candleAccTradePrice"] for x in (d or {}).get("data", [])]
        return sum(v) / len(v) if v else 0.0
    except Exception:
        return None


def korea():
    out, fails, asked = [], 0, 0
    live_s = {r["종목코드"]: r["종목명"] for r in read_csv(os.path.join(PSD, "data", "kr_stocks.csv"))}
    live_e = {r["종목코드"]: r["종목명"] for r in read_csv(os.path.join(PSD, "data", "etf_prices.csv"))}
    for kind, folder, col, live in (("KS", "krx_stock_month", "시가총액", live_s),
                                    ("KE", "krx_etf_month", "순자산총액", live_e)):
        size_min, val_min, ask_min = RULES[kind]
        path = latest(folder)
        rows = [r for r in read_csv(path) if r["종목코드"] in live and r.get(col)]
        ask = []
        for r in rows:
            size = float(r[col])
            if size >= size_min:
                out.append((f"K:{r['종목코드']}", live[r["종목코드"]], kind[1],
                            "시총" if kind == "KS" else "순자산", fmt(size)))
            elif size >= ask_min:
                ask.append(r["종목코드"])
        with ThreadPoolExecutor(4) as ex:
            vals = list(ex.map(daum_value, ask))
        asked += len(ask)
        for code, v in zip(ask, vals):
            if v is None:
                fails += 1
            elif v >= val_min:
                out.append((f"K:{code}", live[code], kind[1], "거래대금", fmt(round(v))))
        log(f"  {kind}: {os.path.basename(path)} 기준 {sum(1 for o in out if o[2] == kind[1])}종목 "
            f"(거래대금 조회 {len(ask)})")
    if asked and fails > asked / 2:
        raise SystemExit(f"다음 거래대금 조회 실패 {fails}/{asked} — 중단")
    return out


def yahoo_quotes(symbols):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    try:
        op.open("https://fc.yahoo.com", timeout=20)
    except Exception:
        pass                                   # 404 여도 쿠키는 남는다
    crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20).read().decode()
    out = {}
    for i in range(0, len(symbols), 150):
        batch = symbols[i:i + 150]
        url = ("https://query1.finance.yahoo.com/v7/finance/quote?fields=marketCap,netAssets,"
               f"averageDailyVolume3Month,regularMarketPrice&crumb={crumb}&symbols=" + ",".join(batch))
        for attempt in range(3):
            try:
                for q in json.load(op.open(url, timeout=30))["quoteResponse"]["result"]:
                    out[q["symbol"]] = q
                break
            except Exception as e:
                log(f"  야후 {i} 재시도 {type(e).__name__}")
                time.sleep(3 * (attempt + 1))
        time.sleep(0.5)
    return out


def usa():
    prices = read_csv(os.path.join(PSD, "data", "us_prices.csv"))
    sym = json.load(open(os.path.join(PSD, "data", "us_symbols.json"), encoding="utf-8"))
    ysym = {r["티커"]: r["티커"].replace(".", "-") for r in prices}
    q = yahoo_quotes(sorted(set(ysym.values())))
    if len(q) < len(ysym) / 2:
        raise SystemExit(f"야후 quote {len(q)}/{len(ysym)} — 중단")
    out = []
    for r in prices:
        t = r["티커"]
        x = q.get(ysym[t])
        if not x:
            continue
        etf = (sym.get(t) or {}).get("etf") or r.get("구분") == "ETF"
        size_min, val_min, _ = RULES["UE" if etf else "US"]
        size = x.get("netAssets" if etf else "marketCap") or 0
        val = (x.get("averageDailyVolume3Month") or 0) * (x.get("regularMarketPrice") or 0)
        if size >= size_min:
            out.append((f"U:{t}", r["종목명"] or t, "E" if etf else "S", "순자산" if etf else "시총", fmt(round(size))))
        elif val >= val_min:
            out.append((f"U:{t}", r["종목명"] or t, "E" if etf else "S", "거래대금", fmt(round(val))))
    log(f"  미국: quote {len(q)}/{len(ysym)} · 주식 {sum(o[2] == 'S' for o in out)} · ETF {sum(o[2] == 'E' for o in out)}")
    return out


def main():
    today = now_kst().strftime("%Y-%m-%d")
    old = {r["키"]: r for r in load_universe()}
    picked = korea() + usa()
    new = 0
    for key, name, kind, why, val in picked:
        if key in old:
            old[key].update({"이름": name, "기준": why, "값": val})
        else:
            old[key] = {"키": key, "이름": name, "구분": kind, "기준": why, "값": val, "첫선정일": today}
            new += 1
    rows = [[old[k][c] for c in UNIVERSE_COLS] for k in sorted(old)]
    by = {}
    for r in rows:
        g = r[0][0] + r[2]
        by[g] = by.get(g, 0) + 1
    log(f"완료: 이번 선정 {len(picked)} · 새로 들어옴 {new} · 전체 {len(rows)} {by}")
    if not DRY:
        for r in rows:                      # 쉼표가 든 이름은 CSV 가 깨지므로 바꿔 둔다
            r[1] = r[1].replace(",", " ")
        write_rows(os.path.join(SITE, UNIVERSE), UNIVERSE_COLS, rows)


if __name__ == "__main__":
    main()
