# 똑재TV 백테스트 일별 데이터 (주요 종목)

[백테스트 계산기](https://ttokjaetv.github.io/backtest/)의 일별 정밀 계산용 데이터다. 전 종목 월별 데이터는 계속 [portfolio-sheet-data](https://github.com/ttokjaeTV/portfolio-sheet-data)에 있고, 여기는 시총·순자산·거래대금이 큰 종목만 일별로 둔다.

- 주소: `https://ttokjaetv.github.io/backtest-daily/` (계산기와 같은 사이트라 바로 읽힌다)
- 브랜치: `main` = 스크립트·워크플로, `gh-pages` = 데이터. **gh-pages 는 매번 커밋 하나로 덮어쓴다**(기록 없음) — 매일 1,500개 파일이 바뀌어도 저장소가 커지지 않게.

## 대상 종목 (`universe.csv`, 분기마다 다시 고름 · 한 번 들어오면 빼지 않음)

| 구분 | 기준 |
|---|---|
| 국내 주식 | 시가총액 2조 원 이상 또는 60거래일 평균 거래대금 300억 원 이상 |
| 국내 ETF | 순자산 1,000억 원 이상 또는 60거래일 평균 거래대금 100억 원 이상 |
| 미국 주식 | 시가총액 500억 달러 이상 또는 3개월 평균 거래대금 10억 달러 이상 |
| 미국 ETF | 순자산 50억 달러 이상 또는 3개월 평균 거래대금 1억 달러 이상 |

## 파일 (gh-pages)

| 경로 | 내용 | 출처 |
|---|---|---|
| `K/{코드}.csv` | 날짜,종가 — 국내 ETF는 **그날 실제 종가**, 국내 주식은 분할 보정 종가(2000~) | ETF: 다음 `adjusted=false`(2006-07~) + KRX(그 이전), 주식: 네이버 |
| `U/{티커}.csv` | 날짜,종가 (USD, 분할만 보정·배당 미반영) | 야후 |
| `div/K/{코드}.csv` | 날짜(배당락일),금액 — 가격과 같은 단위 | 세이브로(portfolio-sheet-data) |
| `div/U/{티커}.csv` | 날짜(배당락일),금액 | 야후 + `us_dividends_patch.csv` |
| `fx.csv` | 날짜,매매기준율 (1990-03~) | 서울외국환중개 |
| `index.json` | 종목 목록 `[키, 이름, E/S, 첫날, 마지막날, 배당파일]` | |

- ★ 네이버 국내 ETF 가격은 분배금 소급 보정값이라 쓰지 않는다(분배금을 또 더하면 이중 계산). 다음 `adjusted=false` 는 KRX 실제 종가와 일치한다(KODEX 200 2014-12-30 24,670 · 2020-12-30 39,500).
- 배당락일 = 기준일 이전 마지막 거래일의 바로 앞 거래일 (T+2 결제, 12-31 기준일 포함).
- 국내 주식 배당은 세이브로 '당시 1주' 금액을 그달 KRX 실제 종가 ÷ 네이버 보정 종가로 나눠 단위를 맞춘다 (portfolio-sheet-data `build_backtest_data.py` 와 같은 방식).

## 갱신 (`.github/workflows/update.yml`)

| 언제(KST) | 하는 일 |
|---|---|
| 평일 19:27 | 국내 최근 20일 + 국내 배당 + 환율 |
| 화~토 07:47 | 미국 최근 20일(배당·분할 포함) + 환율 |
| 일 04:17 | 전 종목 처음부터 다시 받기 + 검증 |
| 1·4·7·10월 3일 11:37 | 대상 종목 다시 고르기 |

- 겹치는 날 값이 0.2% 넘게 다르면(분할 소급) 그 종목만 처음부터 다시 받는다.
- 실패하면 이슈가 열리고 gh-pages 는 바뀌지 않는다.

## 로컬에서

```bash
python scripts/select_universe.py          # 대상 고르기 (DRY=1: 개수만)
python scripts/build_daily.py full         # 전부 받기 (inc: 최근분만, 부분만: K U fx div index)
python scripts/krx_backfill.py             # 2006-07 이전 국내 ETF 구간 (KRX 로그인 필요, 로컬 전용)
python scripts/verify.py                   # 월별 자료와 대조
```

- `site/` = gh-pages 내용, 옆 폴더 `../portfolio-sheet-data` 를 읽는다 (`SITE`·`PSD` 환경변수로 바꿀 수 있음).
- 새로 선정된 국내 ETF 중 2006-07 이전 상장 종목이 있으면 `krx_backfill.py` 를 로컬에서 다시 돌린다 (Actions 에는 KRX 계정이 없다).
