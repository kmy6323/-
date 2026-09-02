"""
MACD GC + 이격도 조건 시그널 스캐너

Pine Script "MACD GC + 이격도 조건 시그널" 로직을 Python으로 이식.

조건 (원본 인디케이터와 동일):
  1) MACD(5,20,5) 골든크로스: macdLine이 signalLine을 상향 돌파하는 순간
  2) 이격도(종가 / 15일 SMA * 100)가 "직전 봉 기준" 최근 10봉 중 최저값이 85 이하였던 적이 있을 것
     -> Pine: ta.lowest(disparity, 10)[1] <= 85

의존성:
  pip install finance-datareader pandas numpy requests

텔레그램 알림:
  환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 를 설정하면 스캔 완료 후
  결과를 텔레그램 메시지로 전송한다.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import requests

# ── 파라미터 (Pine Script와 1:1 동일) ──────────────────────
FAST_LEN = 5
SLOW_LEN = 20
SIG_LEN = 5
DISP_LEN = 15
LOOKBACK = 10
DISP_THRESHOLD = 85.0


def calc_signal(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV 데이터프레임에 MACD/이격도/시그널 컬럼을 추가해서 반환."""
    df = df.copy()
    close = df["Close"]

    fast_ma = close.ewm(span=FAST_LEN, adjust=False).mean()
    slow_ma = close.ewm(span=SLOW_LEN, adjust=False).mean()
    macd_line = fast_ma - slow_ma
    signal_line = macd_line.ewm(span=SIG_LEN, adjust=False).mean()

    ma15 = close.rolling(DISP_LEN).mean()
    disparity = close / ma15 * 100

    # ta.crossover(macdLine, signalLine)
    golden_cross = (macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1))

    # ta.lowest(disparity, lookback)[1] <= threshold
    # : 직전 봉을 기준으로 그 이전 10봉(자기 자신 포함) 중 이격도 최저값
    disp_condition = disparity.shift(1).rolling(LOOKBACK).min() <= DISP_THRESHOLD

    buy_signal = golden_cross & disp_condition

    df["MACD"] = macd_line
    df["Signal"] = signal_line
    df["Disparity"] = disparity
    df["GoldenCross"] = golden_cross
    df["DispCondition"] = disp_condition
    df["BuySignal"] = buy_signal
    return df


def get_kr_universe(markets=("KOSPI", "KOSDAQ")) -> pd.DataFrame:
    """국내 지정 시장의 전종목 코드/이름 리스트."""
    frames = [fdr.StockListing(m) for m in markets]
    df = pd.concat(frames, ignore_index=True)
    return df[["Code", "Name"]].drop_duplicates(subset="Code")


# 하위호환 alias
get_universe = get_kr_universe


def get_sp500_universe() -> pd.DataFrame:
    """S&P500 종목 코드/이름 리스트 (Code/Name 컬럼으로 통일)."""
    df = fdr.StockListing("S&P500")[["Symbol", "Name"]]
    return df.rename(columns={"Symbol": "Code"}).drop_duplicates(subset="Code")


def get_us_universe_like_agentt(
    min_price: float = 30.0,
    min_dollar_volume: float = 21_200_000.0,
    cache_path: str = "us_universe_agentt.csv",
    force_refresh: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """Agent T(y_pattern_scanner.py)와 동일한 산출 방식으로 미국 종목 유니버스 생성.

    NYSE + NASDAQ + AMEX 상장 보통주(Common Stock) 중
      1차: 주가 >= min_price
      2차: 최근 20거래일 평균 거래대금(종가*거래량) >= min_dollar_volume (기본 300억원 ≈ $21.2M)

    2차 필터는 종목마다 개별 시세 조회가 필요해 시간이 오래 걸리므로
    결과를 cache_path(CSV)에 저장하고, 이후에는 캐시를 재사용한다.
    (Agent T의 y_pattern_scanner.py/1,280개 종목 리스트를 그대로 읽어오는 게 아니라
    동일한 산출 로직을 이 파일 안에서 독립적으로 재현한 것 — 기존 파일은 참조/수정하지 않음)
    """
    if not force_refresh and os.path.exists(cache_path):
        cached = pd.read_csv(cache_path, dtype={"Code": str})
        if verbose:
            print(f"캐시 사용: {cache_path} ({len(cached)}개 종목). 새로 산출하려면 force_refresh=True")
        return cached

    candidates = []
    for exch in ("nyse", "nasdaq", "amex"):
        url = f"https://api.nasdaq.com/api/screener/stocks?download=true&exchange={exch}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        for row in data["data"]["rows"]:
            name = row.get("name", "")
            symbol = row.get("symbol", "")
            lastsale = row.get("lastsale", "")
            if "Common Stock" not in name or not symbol:
                continue
            try:
                price = float(lastsale.replace("$", "").replace(",", ""))
            except ValueError:
                continue
            if price < min_price:
                continue
            candidates.append({"Code": symbol, "Name": name})

    cand_df = pd.DataFrame(candidates).drop_duplicates(subset="Code").reset_index(drop=True)
    if verbose:
        print(f"1차 필터(보통주 + 주가>=${min_price:.0f}): {len(cand_df)}개 → 거래대금 2차 필터 시작 (시간 소요)")

    end = datetime.today()
    start = end - timedelta(days=40)
    survivors = []
    for i, row in cand_df.iterrows():
        code = row["Code"]
        try:
            hist = fdr.DataReader(code, start, end)
            if len(hist) < 5:
                continue
            dollar_vol = (hist["Close"] * hist["Volume"]).tail(20).mean()
            if dollar_vol >= min_dollar_volume:
                survivors.append({"Code": code, "Name": row["Name"]})
        except Exception:
            pass
        if verbose and (i + 1) % 200 == 0:
            print(f"  ... {i + 1}/{len(cand_df)} 진행, 생존 {len(survivors)}개")

    out = pd.DataFrame(survivors)
    out.to_csv(cache_path, index=False, encoding="utf-8-sig")
    if verbose:
        print(f"2차 필터(거래대금>=${min_dollar_volume:,.0f}) 완료: {len(out)}개, 캐시 저장: {cache_path}")
    return out


def check_one(code: str, name: str = "", days: int = 120) -> dict | None:
    """단일 종목의 최신 봉 기준 시그널 여부 확인. 시그널이면 dict, 아니면 None."""
    end = datetime.today()
    start = end - timedelta(days=days)
    df = fdr.DataReader(code, start, end)
    if len(df) < DISP_LEN + LOOKBACK + 5:
        return None

    df = calc_signal(df)
    last = df.iloc[-1]
    if not bool(last["BuySignal"]):
        return None

    return {
        "Code": code,
        "Name": name,
        "Date": df.index[-1].strftime("%Y-%m-%d"),
        "Close": last["Close"],
        "MACD": round(float(last["MACD"]), 2),
        "Signal": round(float(last["Signal"]), 2),
        "Disparity": round(float(last["Disparity"]), 2),
    }


def scan(codes: pd.DataFrame | None = None, days: int = 120, sleep_sec: float = 0.05,
          verbose: bool = True) -> pd.DataFrame:
    """전종목(또는 지정한 codes) 스캔 후 시그널 발생 종목 리스트 반환."""
    universe = codes if codes is not None else get_universe()

    results = []
    total = len(universe)
    for i, row in universe.iterrows():
        code, name = row["Code"], row["Name"]
        try:
            hit = check_one(code, name, days=days)
            if hit:
                results.append(hit)
                if verbose:
                    print(f"[BUY] {name}({code})  종가={hit['Close']}  이격도={hit['Disparity']:.2f}")
        except Exception as e:
            if verbose:
                print(f"[ERROR] {name}({code}): {e}")
        time.sleep(sleep_sec)

        if verbose and (i + 1) % 200 == 0:
            print(f"  ... {i + 1}/{total} 진행")

    return pd.DataFrame(results)


TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MSG_LIMIT = 4096


MARKET_LABELS = {
    "us_agentt": "🇺🇸 미국(NYSE/NASDAQ/AMEX)",
    "sp500": "🇺🇸 S&P500",
    "kr": "🇰🇷 국내(KOSPI+KOSDAQ)",
}


def format_signal_message(result_df: pd.DataFrame, market_label: str = "") -> str:
    """스캔 결과를 텔레그램 메시지 형식으로 포맷팅."""
    prefix = f"[{market_label}] " if market_label else ""
    if result_df.empty:
        return f"{prefix}오늘 발생한 시그널이 없습니다."

    today = datetime.today().strftime("%Y-%m-%d")
    lines = [f"📈 {prefix}MACD GC + 이격도 시그널 ({today})", f"총 {len(result_df)}개 종목", ""]
    for _, row in result_df.iterrows():
        lines.append(
            f"• {row['Name']} ({row['Code']})\n"
            f"  날짜: {row['Date']} / 종가: {row['Close']} / 이격도: {row['Disparity']}"
        )
    message = "\n".join(lines)
    if len(message) > TELEGRAM_MSG_LIMIT:
        message = message[: TELEGRAM_MSG_LIMIT - 20] + "\n... (이하 생략)"
    return message


def send_telegram_message(text: str) -> None:
    """환경변수(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)로 지정된 텔레그램 챗방에 메시지 전송."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[텔레그램] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정, 전송 생략")
        return

    url = TELEGRAM_API_URL.format(token=token)
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[텔레그램] 전송 실패: {e}")


def main():
    parser = argparse.ArgumentParser(description="MACD GC + 이격도 조건 시그널 스캐너")
    parser.add_argument("--codes", nargs="*", help="특정 종목코드만 스캔 (예: --codes AAPL TSLA)")
    parser.add_argument(
        "--universe",
        choices=["us_agentt", "sp500", "kr"],
        default="us_agentt",
        help="스캔 대상 유니버스. us_agentt=Agent T와 동일 방식(NYSE+NASDAQ+AMEX, 주가$30+/거래대금$21.2M+, 기본값), "
             "sp500=S&P500, kr=KOSPI+KOSDAQ",
    )
    parser.add_argument("--refresh-universe", action="store_true",
                         help="us_agentt 유니버스 캐시를 무시하고 새로 산출")
    parser.add_argument("--days", type=int, default=120, help="조회 기간(일). 기본 120")
    parser.add_argument("--sleep", type=float, default=0.05, help="종목간 대기시간(초). 기본 0.05")
    args = parser.parse_args()

    if args.codes:
        codes_df = pd.DataFrame({"Code": args.codes, "Name": args.codes})
    elif args.universe == "us_agentt":
        codes_df = get_us_universe_like_agentt(force_refresh=args.refresh_universe)
    elif args.universe == "sp500":
        codes_df = get_sp500_universe()
    else:
        codes_df = get_kr_universe()

    result_df = scan(codes=codes_df, days=args.days, sleep_sec=args.sleep)

    print(f"\n총 {len(result_df)}개 종목 시그널 발생")
    if not result_df.empty:
        print(result_df.to_string(index=False))

    market_label = "지정 종목" if args.codes else MARKET_LABELS[args.universe]
    send_telegram_message(format_signal_message(result_df, market_label))


if __name__ == "__main__":
    main()
