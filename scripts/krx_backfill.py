#!/usr/bin/env python3
"""
국내 ETF 일별 실제 종가 중 다음(Daum)이 주지 않는 오래된 구간(2006-07 이전)을 KRX 로 채운다.

다음 일봉은 최근 5,000거래일까지만 준다. KODEX 200(2002-10 상장)처럼 그보다 오래된 ETF 는
앞부분이 비므로, KRX 개별종목 시세(pykrx get_etf_ohlcv_by_date)로 한 번 받아 site/K/{코드}.csv 에 합친다.
실제 종가라 이후 바뀌지 않고, build_daily.py 는 이 구간을 지우지 않는다.

KRX 는 로그인이 필요하다 — portfolio-sheet-data/scripts/krx_auth.py 의 자격증명 파일을 쓰므로
로컬에서만 돌린다(Actions 에는 KRX 계정이 없다). 새로 선정된 오래된 ETF 가 생기면 다시 돌린다.

사용: python scripts/krx_backfill.py      (DRY=1 이면 대상만 출력)
"""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PSD, fmt, load_universe, log, price_path, read_csv, read_series, write_series  # noqa: E402


def main():
    first_month = {r["종목코드"]: r["첫월"] for r in read_csv(os.path.join(PSD, "data", "history", "kr_etf_krx_list.csv"))}
    todo = []
    for r in load_universe():
        if not (r["키"].startswith("K:") and r["구분"] == "E"):
            continue
        code = r["키"][2:]
        px = read_series(price_path(r["키"]))
        fm = first_month.get(code)
        if px and fm and fm < min(px)[:7]:
            todo.append((code, fm, min(px)))
    log(f"KRX 로 앞부분을 채울 ETF {len(todo)}개: " + " ".join(f"{c}({fm}~{d})" for c, fm, d in todo))
    if not todo or os.environ.get("DRY") == "1":
        return
    sys.path.insert(0, os.path.join(PSD, "scripts"))
    import krx_auth
    if not krx_auth.login(verbose=False):
        raise SystemExit("KRX 로그인 실패")
    with contextlib.redirect_stdout(io.StringIO()):     # pykrx 가 아이디를 출력하므로 막는다
        from pykrx import stock
    for code, fm, first in todo:
        start = fm.replace("-", "") + "01"
        end = first.replace("-", "")
        with contextlib.redirect_stdout(io.StringIO()):
            df = stock.get_etf_ohlcv_by_date(start, end, code)
        old = read_series(price_path(f"K:{code}"))
        add = {d.strftime("%Y-%m-%d"): fmt(v) for d, v in df["종가"].items() if v and d.strftime("%Y-%m-%d") < first}
        old.update(add)
        write_series(price_path(f"K:{code}"), "날짜,종가", old)
        log(f"  {code}: {len(add)}일 추가 ({min(add) if add else '-'} ~)")


if __name__ == "__main__":
    main()
