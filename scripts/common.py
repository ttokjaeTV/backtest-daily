"""backtest-daily 공용 도구 — 경로, HTTP, CSV 읽기·쓰기, 파일명 규칙."""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
SITE = os.environ.get("SITE", "site")                                 # 게시용 데이터 (gh-pages 브랜치 내용)
PSD = os.environ.get("PSD", os.path.join("..", "portfolio-sheet-data"))  # portfolio-sheet-data 체크아웃
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"

RESERVED = {"CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


def to_filename(code):
    """portfolio-sheet-data/scripts/winsafe.py 와 같은 규칙 (Windows 예약어 티커 뒤에 '_')."""
    s = str(code).replace("/", "_")
    base, dot, rest = s.partition(".")
    if base.upper() in RESERVED:
        base += "_"
    return base + dot + rest


def now_kst():
    return datetime.now(KST)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http(url, headers=None, tries=4, timeout=40, enc="utf-8"):
    """GET → 문자열. 404 는 None. 429 는 길게 쉰다."""
    last = None
    h = {"User-Agent": UA}
    h.update(headers or {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read().decode(enc, "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            time.sleep((10 if e.code == 429 else 1.5) * (i + 1))
        except Exception as e:            # 연결 끊김·타임아웃
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def http_json(url, headers=None, **kw):
    t = http(url, headers, **kw)
    return None if t is None else json.loads(t)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_series(path):
    """날짜,값 두 열 파일 → {날짜: 문자열 값}"""
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) >= 2 and row[1]:
                out[row[0]] = row[1]
    return out


def fmt(v):
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.6f}".rstrip("0").rstrip(".")


def write_text_if_changed(path, text):
    """내용이 같으면 파일을 건드리지 않는다. 바뀌었으면 True."""
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            if f.read() == text:
                return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)
    return True


def write_series(path, header, data):
    """{날짜: 값} → 날짜순 CSV (LF)."""
    lines = [header] + [f"{d},{data[d]}" for d in sorted(data)]
    return write_text_if_changed(path, "\n".join(lines) + "\n")


def write_rows(path, header, rows):
    lines = [",".join(header)] + [",".join(str(x) for x in r) for r in rows]
    return write_text_if_changed(path, "\n".join(lines) + "\n")


# ── 유니버스 (일별 대상 종목) ───────────────────────────────
UNIVERSE = "universe.csv"
UNIVERSE_COLS = ["키", "이름", "구분", "기준", "값", "첫선정일"]


def load_universe():
    return read_csv(os.path.join(SITE, UNIVERSE))


def price_path(key):
    mk, code = key.split(":", 1)
    return os.path.join(SITE, mk, to_filename(code) + ".csv")


def div_path(key):
    mk, code = key.split(":", 1)
    return os.path.join(SITE, "div", mk, to_filename(code) + ".csv")
