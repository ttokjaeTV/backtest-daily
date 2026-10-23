#!/usr/bin/env python3
"""
일별 가격·배당·환율 → site/   (universe.csv 의 종목만)

  site/K/{코드}.csv        날짜,종가
      국내 ETF   다음 일봉 adjusted=false — 그날 실제 종가 (KRX 와 같음. 2026-10 실측 24,670·39,500 일치)
                 다음은 최근 5,000거래일(2006-07~)까지만 준다. 그 이전은 krx_backfill.py 로 한 번 채운다.
      국내 주식  네이버 일봉 — 분할·병합 보정 종가 (portfolio-sheet-data kr_stock/ 월봉과 같은 값), 2000-01~
  site/U/{티커}.csv        날짜,종가   야후 일봉 close — 분할만 보정(배당 미반영), USD
  site/div/K/{코드}.csv    날짜,금액   날짜 = 배당락일, 금액 = 가격과 같은 단위
      ETF   세이브로 분배금 그대로 (가격이 실제 종가라 단위가 같다)
      주식  세이브로 배당금 ÷ (그달 KRX 실제 종가 ÷ 네이버 보정 종가) — build_backtest_data.py 와 같은 방식
      배당락일 = 기준일 이전 마지막 거래일의 바로 앞 거래일 (T+2 결제. 12-31 기준일도 맞는다)
  site/div/U/{티커}.csv    날짜,금액   야후 배당 이벤트(배당락일, 분할 보정 금액) + us_dividends_patch.csv
  site/fx.csv              날짜,매매기준율   서울외국환중개 고시, 1990-03~
  site/index.json          계산기용 목록

받는 방식
  inc  (기본) 파일 마지막 날짜 20일 전부터 다시 받아 합친다. 겹치는 날 값이 0.2% 넘게 다르면
             (분할·병합이 소급된 것) 그 종목만 처음부터 다시 받아 통째로 바꾼다.
  full        모든 종목을 처음부터 다시 받는다 (매주 — 분할·배당 정정 반영)
  배당(국내)은 portfolio-sheet-data 의 세이브로 파일로 매번 전부 다시 만든다.

사용: python scripts/build_daily.py [inc|full] [K|U|fx|div|index ...]
      LIMIT=N (종목 수 상한, 시험용) · WORKERS=4
"""

import bisect
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PSD, SITE, div_path, fmt, http, http_json, load_universe, log, now_kst,  # noqa: E402
                    price_path, read_csv, read_series, to_filename, write_series, write_text_if_changed)

LIMIT = int(os.environ.get("LIMIT", "0"))
WORKERS = int(os.environ.get("WORKERS", "4"))
OVERLAP_DAYS = 20
TOL = 0.002
KS_START = "20000101"
H = os.path.join(PSD, "data", "history")
_lock = threading.Lock()


# ── 출처별 받기 → {날짜: 종가}, {날짜: 배당} ───────────────────
def daum(code, limit):
    d = http_json(f"https://finance.daum.net/api/charts/A{code}/days?limit={limit}&adjusted=false",
                  {"Referer": f"https://finance.daum.net/quotes/A{code}"})
    if not d:
        return None
    return {x["date"]: fmt(x["tradePrice"]) for x in d.get("data", []) if x.get("tradePrice")}


def naver(code, start):
    end = (now_kst() + timedelta(days=1)).strftime("%Y%m%d")
    d = http_json(f"https://api.stock.naver.com/chart/domestic/item/{code}/day"
                  f"?startDateTime={start}0000&endDateTime={end}0000",
                  {"Referer": "https://m.stock.naver.com/"})
    if d is None:
        return None
    infos = d.get("priceInfos") if isinstance(d, dict) else d
    out = {}
    for x in infos or []:
        day, c = x.get("localDate"), x.get("closePrice")
        if day and c:
            out[f"{day[:4]}-{day[4:6]}-{day[6:]}"] = fmt(c)
    return out


def yahoo(ticker, period1):
    """→ (가격, 배당, 분할있음) 또는 None"""
    sym = ticker.replace(".", "-")
    far = int(time.time()) + 86400 * 2
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                  f"?period1={period1}&period2={far}&interval=1d&events=div%7Csplit")
    try:
        res = d["chart"]["result"][0]
    except (TypeError, KeyError, IndexError):
        return None
    got = (res.get("meta") or {}).get("dataGranularity")
    if got and got != "1d":
        raise ValueError(f"{ticker} 간격 {got}")
    off = (res.get("meta") or {}).get("gmtoffset", 0) or 0
    day = lambda t: datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d")
    px = {}
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for t, c in zip(res.get("timestamp") or [], closes):
        if c is not None and c > 0:
            px[day(t)] = fmt(round(c, 6))
    ev = res.get("events") or {}
    dv = defaultdict(float)
    for v in (ev.get("dividends") or {}).values():
        if v.get("amount"):
            dv[day(v["date"])] += v["amount"]
    return px, {k: fmt(round(v, 6)) for k, v in dv.items()}, bool(ev.get("splits"))


# ── 종목 하나 갱신 ───────────────────────────────────────────
def mismatch(old, new):
    """겹치는 날(파일 마지막 날 제외) 중 값이 TOL 넘게 다른 날짜"""
    if not old:
        return []
    last = max(old)
    bad = []
    for d, v in new.items():
        if d in old and d != last:
            a, b = float(old[d]), float(v)
            if a > 0 and abs(b / a - 1) > TOL:
                bad.append(d)
    return bad


def since(old, days=OVERLAP_DAYS):
    last = datetime.strptime(max(old), "%Y-%m-%d")
    return last - timedelta(days=days)


def update(row, mode, patches):
    key, kind = row["키"], row["구분"]
    mk, code = key.split(":", 1)
    path = price_path(key)
    old = read_series(path)
    full = mode == "full" or not old
    note = ""

    if mk == "K" and kind == "E":
        new = daum(code, 5000 if full else 15)
        if new is None:
            return key, "없음"
        if not full and mismatch(old, new):
            new, note = daum(code, 5000) or {}, "다시받음"
        merged = dict(old)
        merged.update(new)              # 다음 이전(2006-07 전) KRX 구간은 남긴다 — 실제 종가라 소급 변경이 없다
        changed = write_series(path, "날짜,종가", merged)
    elif mk == "K":
        new = naver(code, KS_START if full else since(old).strftime("%Y%m%d"))
        if new is None:
            return key, "없음"
        if not full and mismatch(old, new):
            new, full, note = naver(code, KS_START) or {}, True, "분할 소급 → 다시받음"
        if full:
            if not new:
                return key, "빈 응답"
            changed = write_series(path, "날짜,종가", new)
        else:
            merged = dict(old)
            merged.update(new)
            changed = write_series(path, "날짜,종가", merged)
    else:
        p1 = 0 if full else int(since(old).replace(tzinfo=timezone.utc).timestamp())
        got = yahoo(code, p1)
        if got is None:
            return key, "없음"
        new, dv, split = got
        if not full and (split or mismatch(old, new)):
            got = yahoo(code, 0)
            if got is None:
                return key, "없음"
            new, dv, _ = got
            full, note = True, "분할 → 다시받음"
        dpath = div_path(key)
        if full:
            if not new:
                return key, "빈 응답"
            changed = write_series(path, "날짜,종가", new)
            divs = dv
        else:
            merged = dict(old)
            merged.update(new)
            changed = write_series(path, "날짜,종가", merged)
            divs = read_series(dpath)
            cut = min(new) if new else None
            if cut:                          # 받은 구간의 배당은 새 값으로 바꾼다 (정정·취소 반영)
                divs = {d: v for d, v in divs.items() if d < cut}
            divs.update(dv)
        divs = apply_patch(code, divs, read_series(path), patches.get(code, []))
        if divs:
            write_series(dpath, "날짜,금액", divs)
        elif os.path.exists(dpath):
            os.remove(dpath)
    return key, (note or ("갱신" if changed else "같음"))


def apply_patch(ticker, divs, px, rows):
    """us_dividends_patch.csv (월 단위) — 교체: 그달 배당을 그 금액 하나로, 추가: 그달에 없을 때만 그달 마지막 거래일로"""
    if not rows:
        return divs
    divs = dict(divs)
    for r in rows:
        m, v = r["월"], fmt(float(r["주당배당USD"]))
        inm = sorted(d for d in divs if d.startswith(m))
        if r["조치"] == "교체":
            if inm:
                for d in inm:
                    del divs[d]
                divs[inm[0]] = v
        elif not inm:
            days = sorted(d for d in px if d.startswith(m))
            if days:
                divs[days[-1]] = v
    return divs


def prices(mode, markets):
    rows = [r for r in load_universe() if r["키"].split(":")[0] in markets]
    if LIMIT:
        rows = rows[:LIMIT]
    patches = defaultdict(list)
    for r in read_csv(os.path.join(H, "us_dividends_patch.csv")):
        patches[r["티커"]].append(r)
    t0, stat, fails, refetched = time.time(), defaultdict(int), [], []
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(update, r, mode, patches): r["키"] for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            key = futs[f]
            try:
                _, st = f.result()
            except Exception as e:
                st = "오류"
                fails.append(f"{key}({type(e).__name__})")
            stat[st] += 1
            if st in ("없음", "빈 응답"):
                fails.append(f"{key}({st})")
            elif "다시받음" in st:
                refetched.append(key)
            if i % 200 == 0:
                log(f"  {i}/{len(rows)} {time.time()-t0:.0f}s")
    log(f"가격 {mode} {''.join(markets)}: {len(rows)}종목 {dict(stat)} {time.time()-t0:.0f}s")
    if refetched:
        log("  처음부터 다시 받음: " + " ".join(sorted(refetched)))
    if fails:
        log("  실패: " + " ".join(fails[:40]) + (" …" if len(fails) > 40 else ""))
    return len(fails), len(rows)


# ── 환율 ───────────────────────────────────────────────────
def fx(mode):
    path = os.path.join(SITE, "fx.csv")
    old = read_series(path)
    today = now_kst().date()
    years = range(1990, today.year + 1) if (mode == "full" or not old) else \
        sorted({(today - timedelta(days=OVERLAP_DAYS)).year, today.year})
    got, bad = dict(old), []
    for y in years:
        try:
            xml = http(f"http://www.smbs.biz/ExRate/StdExRate_xml.jsp?arr_value=USD_{y}-01-01_{y}-12-31", enc="euc-kr")
        except Exception as e:
            bad.append(f"{y}({type(e).__name__})")
            continue
        for yy, mm, dd, val in re.findall(r"label='(\d\d)\.(\d\d)\.(\d\d)' value='([\d.]+)'", xml or ""):
            full = (1900 if int(yy) >= 80 else 2000) + int(yy)
            got[f"{full}-{mm}-{dd}"] = f"{float(val):.2f}"
        time.sleep(0.3)
    if len(got) < len(old):
        raise SystemExit("환율 행 수가 줄어 저장하지 않습니다")
    write_series(path, "날짜,매매기준율", got)
    log(f"환율: {len(got)}일 {min(got)} ~ {max(got)}" + (f" · 실패 {' '.join(bad)}" if bad else ""))
    return len(bad)


# ── 국내 배당 (세이브로 → 배당락일) ───────────────────────────
def ex_date(record, days):
    """기준일(YYYYMMDD) → 배당락일. days 는 그 종목 거래일(정렬됨)."""
    r = f"{record[:4]}-{record[4:6]}-{record[6:]}"
    if not days:
        return None
    if r <= days[-1]:
        i = bisect.bisect_right(days, r) - 1          # 기준일 이전 마지막 거래일
        return days[i - 1] if i >= 1 else None
    # 아직 오지 않은 기준일 — 주말만 건너뛰어 어림한 배당락일이 지났으면 마지막 거래일 기준으로 둔다
    d = date.fromisoformat(r)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat() if d.isoformat() <= days[-1] else None


def month_series(path, col="종가"):
    out = {}
    for r in read_csv(path):
        try:
            out[r["월"]] = float(r[col])
        except (KeyError, ValueError):
            pass
    return out


def kr_dividends():
    uni = [r for r in load_universe() if r["키"].startswith("K:")]
    etf = defaultdict(list)
    for r in read_csv(os.path.join(H, "kr_etf_distributions.csv")):
        if float(r["주당금액"] or 0) > 0:
            etf[r["종목코드"]].append((r["기준일"], float(r["주당금액"])))
    stk = defaultdict(list)
    for r in read_csv(os.path.join(H, "kr_stock_dividends.csv")):
        if float(r["주당금액"] or 0) > 0:
            stk[r["종목코드"]].append((r["기준일"], float(r["주당금액"])))
    n = skipped = 0
    for row in uni:
        key = row["키"]
        code = key[2:]
        days = sorted(read_series(price_path(key)))
        out = defaultdict(float)
        if row["구분"] == "E":
            for rec, a in etf.get(code, []):
                d = ex_date(rec, days)
                if d:
                    out[d] += a
                else:
                    skipped += 1
        elif stk.get(code):
            kx = month_series(os.path.join(H, "kr_stock_krx", f"{code}.csv"))
            nv = month_series(os.path.join(H, "kr_stock", f"{code}.csv"))
            common = sorted(m for m in kx if m in nv and nv[m] > 0)
            if not common:
                skipped += len(stk[code])
                continue
            fac = {m: kx[m] / nv[m] for m in common}
            for rec, a in stk[code]:
                m = f"{rec[:4]}-{rec[4:6]}"
                prev = [x for x in common if x <= m]
                f = fac[m] if m in fac else (fac[prev[-1]] if prev else fac[common[0]])
                d = ex_date(rec, days)
                if d:
                    out[d] += a / f
                else:
                    skipped += 1
        path = div_path(key)
        if out:
            write_series(path, "날짜,금액", {d: fmt(round(v, 6)) for d, v in out.items()})
            n += 1
        elif os.path.exists(path):
            os.remove(path)
    log(f"국내 배당: {n}종목 · 가격 기간 밖이라 뺀 건 {skipped}")


# ── 목록 ───────────────────────────────────────────────────
def index():
    rows = []
    for r in load_universe():
        px = read_series(price_path(r["키"]))
        if not px:
            continue
        ds = sorted(px)
        rows.append([r["키"], r["이름"], r["구분"], int(ds[0].replace("-", "")), int(ds[-1].replace("-", "")),
                     1 if os.path.exists(div_path(r["키"])) else 0])
    fxs = read_series(os.path.join(SITE, "fx.csv"))
    payload = {"updated": now_kst().strftime("%Y-%m-%d %H:%M"),
               "fx": [min(fxs), max(fxs)] if fxs else None,
               "cols": ["키", "이름", "구분(E=ETF,S=주식)", "첫날", "마지막날", "배당파일"],
               "rows": rows}
    body = json.dumps({k: v for k, v in payload.items() if k != "updated"}, ensure_ascii=False, separators=(",", ":"))
    path = os.path.join(SITE, "index.json")
    if os.path.exists(path):           # 목록이 그대로면 갱신 시각만 바꾸느라 파일을 건드리지 않는다
        try:
            prev = json.load(open(path, encoding="utf-8"))
            prev.pop("updated", None)
            if json.dumps(prev, ensure_ascii=False, separators=(",", ":")) == body:
                log("index.json 변경 없음")
                return
        except ValueError:
            pass
    write_text_if_changed(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    log(f"index.json: {len(rows)}종목")


def main():
    args = sys.argv[1:]
    mode = args.pop(0) if args and args[0] in ("inc", "full") else "inc"
    parts = args or ["K", "U", "fx", "div", "index"]
    bad = total = 0
    mk = [p for p in parts if p in ("K", "U")]
    if mk:
        b, t = prices(mode, mk)
        bad, total = bad + b, total + t
    if "fx" in parts:
        fx(mode)
    if "div" in parts:
        kr_dividends()
    if "index" in parts:
        index()
    if total and bad > total * 0.1:
        log(f"실패 {bad}/{total} — 10% 넘음")
        sys.exit(1)


if __name__ == "__main__":
    main()
