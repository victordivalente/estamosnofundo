#!/usr/bin/env python3
"""Fetch free public series and compute classic Bitcoin cycle indicators.

Writes data/latest.json and bakes the SIM/NÃO answer into index.html.
No API keys. Educational prototype only — not investment advice.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "latest.json"
HTML_PATH = ROOT / "index.html"

BRT = timezone(timedelta(hours=-3))
USER_AGENT = "OSL-btc-indicadores-prototype/1.0 (educational; local build)"
GENESIS = date(2009, 1, 3)

# SIM only with a clear majority in the bottom zone. Fail-safe is NÃO.
FUNDO_SHARE = 0.70
FUNDO_MIN = 5
MIN_OK = 6

# Timeouts and retries for public APIs
TIMEOUT = 45
RETRIES = 3


def now_brt() -> datetime:
    return datetime.now(BRT)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get_json(url: str, *, retries: int = RETRIES) -> object:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_err = exc
            log(f"  tentativa {attempt}/{retries} falhou: {exc}")
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"Falha ao obter {url}: {last_err}")


# ---------------------------------------------------------------------------
# Series helpers
# ---------------------------------------------------------------------------

def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = 0.0
    for i, v in enumerate(values):
        window += v
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """EMA with SMA seed over the first `period` observations."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(period, len(values)):
        seed = values[i] * k + seed * (1.0 - k)
        out[i] = seed
    return out


def weekly_closes(dates: list[date], values: list[float]) -> tuple[list[date], list[float]]:
    """Last daily close of each UTC week ending Sunday."""
    weeks: dict[date, float] = {}
    order: list[date] = []
    for d, v in zip(dates, values):
        end = d + timedelta(days=(6 - d.weekday()))
        if end not in weeks:
            order.append(end)
        weeks[end] = v
    return order, [weeks[d] for d in order]


def wilder_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    def rsi_from(avg_g: float, avg_l: float) -> float:
        if avg_l == 0 and avg_g == 0:
            return 50.0
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))
    out[period] = rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = rsi_from(avg_gain, avg_loss)
    return out


def ols_loglog(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least squares of log10(y) = a * log10(x) + b. Returns (a, b)."""
    n = len(xs)
    if n < 2:
        raise ValueError("série insuficiente para regressão")
    lx = [math.log10(x) for x in xs]
    ly = [math.log10(y) for y in ys]
    mean_x = sum(lx) / n
    mean_y = sum(ly) / n
    var_x = sum((x - mean_x) ** 2 for x in lx)
    if var_x == 0:
        raise ValueError("variância nula na regressão")
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(lx, ly))
    a = cov / var_x
    b = mean_y - a * mean_x
    return a, b


def zone(value: float, fundo_max: float, topo_min: float, *, fundo_inclusive: bool = True) -> str:
    if value <= fundo_max if fundo_inclusive else value < fundo_max:
        return "fundo"
    if value >= topo_min:
        return "topo"
    return "neutro"


def indicator(
    *,
    id: str,
    name: str,
    value: float | None,
    display: str | None,
    zone_name: str | None,
    thresholds: str,
    formula: str,
    source: str,
    unit: str | None = None,
    extra: dict | None = None,
    error: str | None = None,
) -> dict:
    item = {
        "id": id,
        "name": name,
        "value": value,
        "display": display,
        "zone": zone_name,
        "thresholds": thresholds,
        "formula": formula,
        "source": source,
        "unit": unit,
        "error": error,
    }
    if extra:
        item["extra"] = extra
    return item


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_coinmetrics() -> list[dict]:
    params = {
        "assets": "btc",
        "metrics": "PriceUSD,CapMrktCurUSD,CapMVRVCur,HashRate,IssTotUSD",
        "frequency": "1d",
        "page_size": "10000",
        "paging_from": "start",
    }
    url = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?" + urllib.parse.urlencode(params)
    rows: list[dict] = []
    pages = 0
    while url:
        pages += 1
        if pages > 10:
            raise RuntimeError("paginação CoinMetrics excedeu o limite de segurança")
        log(f"  CoinMetrics página {pages}")
        payload = http_get_json(url)
        if not isinstance(payload, dict):
            raise RuntimeError("resposta CoinMetrics inesperada")
        batch = payload.get("data") or []
        rows.extend(batch)
        url = payload.get("next_page_url")
        if url:
            time.sleep(0.25)
    return rows


def fetch_blockchain_chart(name: str, timespan: str = "all") -> list[tuple[date, float]]:
    url = (
        f"https://api.blockchain.info/charts/{name}"
        f"?timespan={timespan}&format=json&sampled=false"
    )
    payload = http_get_json(url)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RuntimeError(f"blockchain.info {name}: status inesperado")
    out: list[tuple[date, float]] = []
    for point in payload.get("values") or []:
        d = datetime.fromtimestamp(int(point["x"]), timezone.utc).date()
        out.append((d, float(point["y"])))
    return out


def fetch_fear_greed() -> dict:
    payload = http_get_json("https://api.alternative.me/fng/?limit=1&format=json")
    if not isinstance(payload, dict) or not payload.get("data"):
        raise RuntimeError("alternative.me: resposta inesperada")
    row = payload["data"][0]
    ts = int(row["timestamp"])
    return {
        "value": float(row["value"]),
        "classification": row.get("value_classification"),
        "date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
    }


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def parse_cm(rows: list[dict]) -> dict:
    dates: list[date] = []
    price: list[float] = []
    mcap: list[float] = []
    mvrv: list[float] = []
    hashrate: list[tuple[date, float]] = []
    issuance: list[tuple[date, float]] = []
    for row in rows:
        d = datetime.fromisoformat(row["time"].replace("Z", "+00:00")[:19] + "+00:00").date()
        hr = row.get("HashRate")
        if hr not in (None, ""):
            hashrate.append((d, float(hr)))
        iss = row.get("IssTotUSD")
        if iss not in (None, ""):
            issuance.append((d, float(iss)))
        p = row.get("PriceUSD")
        cap = row.get("CapMrktCurUSD")
        mv = row.get("CapMVRVCur")
        if p in (None, "") or cap in (None, "") or mv in (None, ""):
            continue
        pf, cf, vf = float(p), float(cap), float(mv)
        if pf <= 0 or cf <= 0 or vf <= 0:
            continue
        dates.append(d)
        price.append(pf)
        mcap.append(cf)
        mvrv.append(vf)
    return {
        "dates": dates,
        "price": price,
        "mcap": mcap,
        "mvrv": mvrv,
        "hashrate": hashrate,
        "issuance": issuance,
    }


def compute_all(cm: dict, revenue: list[tuple[date, float]] | None, fng: dict | None) -> list[dict]:
    dates: list[date] = cm["dates"]
    price: list[float] = cm["price"]
    mcap: list[float] = cm["mcap"]
    mvrv: list[float] = cm["mvrv"]
    out: list[dict] = []

    last_i = len(price) - 1
    last_date = dates[last_i]
    last_price = price[last_i]
    ath_i = max(range(len(price)), key=lambda i: price[i])
    ath_price = price[ath_i]
    ath_date = dates[ath_i]

    # 1. MVRV Z-Score
    try:
        realized = [cap / ratio for cap, ratio in zip(mcap, mvrv)]
        sigma = statistics.pstdev(mcap)
        if sigma <= 0:
            raise ValueError("desvio padrão do market cap é zero")
        z_series = [(cap - rv) / sigma for cap, rv in zip(mcap, realized)]
        z = z_series[last_i]
        out.append(indicator(
            id="mvrv_z",
            name="MVRV Z-Score",
            value=round(z, 4),
            display=f"{z:.2f}",
            zone_name=zone(z, 0.0, 7.0),
            thresholds="fundo ≤ 0; neutro entre 0 e 7; topo ≥ 7",
            formula="(valor de mercado − valor realizado) / desvio padrão populacional do valor de mercado, na série diária completa",
            source="CoinMetrics community API (CapMrktCurUSD, CapMVRVCur). Valor realizado = market cap / MVRV.",
            extra={
                "mvrv": round(mvrv[last_i], 4),
                "realized_price_usd": round(last_price / mvrv[last_i], 2),
            },
        ))
    except Exception as exc:
        out.append(indicator(id="mvrv_z", name="MVRV Z-Score", value=None, display=None, zone_name=None,
                             thresholds="fundo ≤ 0; topo ≥ 7", formula="(MV − RV) / σ(MV)", source="CoinMetrics community API",
                             error=str(exc)))

    # 2. NUPL
    try:
        nupl = 1.0 - 1.0 / mvrv[last_i]
        out.append(indicator(
            id="nupl",
            name="NUPL",
            value=round(nupl, 4),
            display=f"{nupl:.3f}",
            zone_name=zone(nupl, 0.0, 0.75, fundo_inclusive=False),  # fundo se < 0
            thresholds="fundo < 0 (capitulação); neutro de 0 a 0,75; topo ≥ 0,75 (euforia)",
            formula="(valor de mercado − valor realizado) / valor de mercado = 1 − 1/MVRV",
            source="CoinMetrics community API (CapMVRVCur)",
        ))
        # Fix: zone() with fundo_inclusive=False and fundo_max=0 means value < 0 is fundo. Good.
        # But zone() if value <= fundo_max if inclusive else value < fundo_max — yes < 0.
        # nupl == 0 would be neutro. Good.
    except Exception as exc:
        out.append(indicator(id="nupl", name="NUPL", value=None, display=None, zone_name=None,
                             thresholds="fundo < 0; topo ≥ 0,75", formula="1 − 1/MVRV", source="CoinMetrics community API",
                             error=str(exc)))

    # 3. Pi Cycle (Swift top + Dončić bottom)
    try:
        sma111 = sma(price, 111)
        sma350 = sma(price, 350)
        sma471 = sma(price, 471)
        ema150 = ema(price, 150)
        s111, s350 = sma111[last_i], sma350[last_i]
        s471, e150 = sma471[last_i], ema150[last_i]
        if None in (s111, s350, s471, e150):
            raise ValueError("médias móveis ainda não maduras")
        top_line = 2.0 * s350
        bottom_line = 0.745 * s471
        top_on = s111 >= top_line
        bottom_on = e150 < bottom_line
        if top_on:
            zname = "topo"
        elif bottom_on:
            zname = "fundo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="pi_cycle",
            name="Pi Cycle (topo e fundo)",
            value=round(s111 / top_line, 4),
            display=("sinal de topo" if top_on else "sinal de fundo" if bottom_on else "sem cruzamento"),
            zone_name=zname,
            thresholds="topo se 111-SMA ≥ 2 × 350-SMA; fundo se 150-EMA < 0,745 × 471-SMA; senão neutro",
            formula="Topo (Philip Swift, 2019): 111-SMA vs 2×350-SMA. Fundo (Dončić, 2021): 150-EMA vs 0,745×471-SMA. EMA com semente SMA.",
            source="Calculado sobre PriceUSD da CoinMetrics community API",
            extra={
                "sma_111": round(s111, 2),
                "sma_350_x2": round(top_line, 2),
                "ratio_top": round(s111 / top_line, 4),
                "ema_150": round(e150, 2),
                "sma_471_x0745": round(bottom_line, 2),
                "ratio_bottom": round(e150 / bottom_line, 4),
                "top_signal": top_on,
                "bottom_signal": bottom_on,
            },
        ))
    except Exception as exc:
        out.append(indicator(id="pi_cycle", name="Pi Cycle (topo e fundo)", value=None, display=None, zone_name=None,
                             thresholds="topo se 111-SMA ≥ 2×350-SMA; fundo se 150-EMA < 0,745×471-SMA",
                             formula="ver PROPOSTA.md", source="CoinMetrics PriceUSD", error=str(exc)))

    # 4. 200-week MA
    try:
        w_dates, w_closes = weekly_closes(dates, price)
        w_sma = sma(w_closes, 200)
        if w_sma[-1] is None:
            raise ValueError("ainda não há 200 semanas de fechamento")
        wma = w_sma[-1]
        ratio = last_price / wma
        if ratio <= 1.0:
            zname = "fundo"
        elif ratio >= 3.0:
            zname = "topo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="ma_200w",
            name="Média móvel de 200 semanas",
            value=round(ratio, 4),
            display=f"preço / MM200s = {ratio:.2f}",
            zone_name=zname,
            thresholds="fundo se preço ≤ MM200s; neutro até 3×; topo se preço ≥ 3 × MM200s",
            formula="SMA de 200 fechamentos semanais (semana UTC terminada no domingo). Razão = preço do último dia / MM200s.",
            source="Calculado sobre PriceUSD da CoinMetrics community API",
            extra={"ma_200w_usd": round(wma, 2), "weeks": len(w_closes)},
        ))
    except Exception as exc:
        out.append(indicator(id="ma_200w", name="Média móvel de 200 semanas", value=None, display=None, zone_name=None,
                             thresholds="fundo se preço ≤ MM200s; topo se ≥ 3×", formula="SMA 200 semanas",
                             source="CoinMetrics PriceUSD", error=str(exc)))

    # 5. Mayer Multiple
    try:
        sma200 = sma(price, 200)
        if sma200[last_i] is None:
            raise ValueError("ainda não há 200 dias")
        mm = last_price / sma200[last_i]
        if mm < 1.0:
            zname = "fundo"
        elif mm >= 2.4:
            zname = "topo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="mayer",
            name="Mayer Multiple",
            value=round(mm, 4),
            display=f"{mm:.2f}",
            zone_name=zname,
            thresholds="fundo < 1,0; neutro de 1,0 a 2,4; topo ≥ 2,4",
            formula="preço / SMA de 200 dias",
            source="Calculado sobre PriceUSD da CoinMetrics community API",
            extra={"sma_200_usd": round(sma200[last_i], 2)},
        ))
    except Exception as exc:
        out.append(indicator(id="mayer", name="Mayer Multiple", value=None, display=None, zone_name=None,
                             thresholds="fundo < 1,0; topo ≥ 2,4", formula="preço / SMA 200d",
                             source="CoinMetrics PriceUSD", error=str(exc)))

    # 6. Puell Multiple
    try:
        if revenue:
            rev_map = {d: v for d, v in revenue if v is not None and v > 0}
            aligned_dates = [d for d in dates if d in rev_map]
            aligned = [rev_map[d] for d in aligned_dates]
            src = "blockchain.info charts API, miners-revenue (subsídio + taxas, USD)"
            note_src = "receita dos mineradores"
        else:
            rev_map = {d: v for d, v in cm["issuance"] if v > 0}
            aligned_dates = [d for d in dates if d in rev_map]
            aligned = [rev_map[d] for d in aligned_dates]
            src = "CoinMetrics community IssTotUSD (somente subsídio, sem taxas) — reserva, pois miners-revenue não respondeu"
            note_src = "emissão em USD (sem taxas)"
        ma365 = sma(aligned, 365)
        if not aligned or ma365[-1] in (None, 0):
            raise ValueError("série de receita insuficiente")
        puell = aligned[-1] / ma365[-1]
        if puell <= 0.5:
            zname = "fundo"
        elif puell >= 4.0:
            zname = "topo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="puell",
            name="Puell Multiple",
            value=round(puell, 4),
            display=f"{puell:.2f}",
            zone_name=zname,
            thresholds="fundo ≤ 0,5; neutro entre 0,5 e 4; topo ≥ 4",
            formula=f"último dia de {note_src} / SMA de 365 dias da mesma série",
            source=src,
            extra={"as_of": aligned_dates[-1].isoformat(), "daily_usd": round(aligned[-1], 2)},
        ))
    except Exception as exc:
        out.append(indicator(id="puell", name="Puell Multiple", value=None, display=None, zone_name=None,
                             thresholds="fundo ≤ 0,5; topo ≥ 4", formula="receita diária / SMA 365d",
                             source="blockchain.info miners-revenue (preferencial)", error=str(exc)))

    # 7. Logarithmic regression (rainbow-style, our own fit)
    try:
        xs: list[float] = []
        ys: list[float] = []
        for d, p in zip(dates, price):
            days = (d - GENESIS).days
            if days < 1 or p <= 0:
                continue
            xs.append(float(days))
            ys.append(p)
        a, b = ols_loglog(xs, ys)
        days_now = float((last_date - GENESIS).days)
        fitted = 10 ** (a * math.log10(days_now) + b)
        log_res = [math.log10(y) - (a * math.log10(x) + b) for x, y in zip(xs, ys)]
        sigma_res = statistics.pstdev(log_res)
        if sigma_res <= 0:
            raise ValueError("resíduos nulos")
        zres = (math.log10(last_price) - (a * math.log10(days_now) + b)) / sigma_res
        if zres <= -1.5:
            zname = "fundo"
        elif zres >= 2.0:
            zname = "topo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="log_regression",
            name="Regressão logarítmica",
            value=round(zres, 4),
            display=f"z residual = {zres:.2f}",
            zone_name=zname,
            thresholds="fundo se z ≤ −1,5; neutro entre −1,5 e 2,0; topo se z ≥ 2,0",
            formula="ajuste log10(preço) = a · log10(dias desde 2009-01-03) + b, por mínimos quadrados na série completa com preço > 0. z = resíduo / desvio padrão populacional dos resíduos.",
            source="Calculado sobre PriceUSD da CoinMetrics community API. Não é o gráfico Rainbow do BlockchainCenter (coeficientes próprios).",
            extra={
                "a": round(a, 6),
                "b": round(b, 6),
                "fitted_usd": round(fitted, 2),
                "price_over_fitted": round(last_price / fitted, 4),
            },
        ))
    except Exception as exc:
        out.append(indicator(id="log_regression", name="Regressão logarítmica", value=None, display=None, zone_name=None,
                             thresholds="fundo se z ≤ −1,5; topo se z ≥ 2,0", formula="log-log vs dias desde o genesis",
                             source="CoinMetrics PriceUSD", error=str(exc)))

    # 8. Weekly RSI
    try:
        w_dates, w_closes = weekly_closes(dates, price)
        rsi_s = wilder_rsi(w_closes, 14)
        rsi = rsi_s[-1]
        if rsi is None:
            raise ValueError("RSI semanal ainda não maduro")
        if rsi <= 30:
            zname = "fundo"
        elif rsi >= 70:
            zname = "topo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="rsi_weekly",
            name="RSI semanal (14)",
            value=round(rsi, 2),
            display=f"{rsi:.1f}",
            zone_name=zname,
            thresholds="fundo ≤ 30; neutro entre 30 e 70; topo ≥ 70",
            formula="RSI de Wilder (14) sobre os fechamentos semanais (semana UTC terminada no domingo)",
            source="Calculado sobre PriceUSD da CoinMetrics community API",
            extra={"week_ending": w_dates[-1].isoformat(), "weekly_close_usd": round(w_closes[-1], 2)},
        ))
    except Exception as exc:
        out.append(indicator(id="rsi_weekly", name="RSI semanal (14)", value=None, display=None, zone_name=None,
                             thresholds="fundo ≤ 30; topo ≥ 70", formula="RSI Wilder 14 semanal",
                             source="CoinMetrics PriceUSD", error=str(exc)))

    # 9. Fear & Greed
    try:
        if not fng:
            raise ValueError("índice indisponível")
        fg = fng["value"]
        if fg <= 24:
            zname = "fundo"
        elif fg >= 75:
            zname = "topo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="fear_greed",
            name="Fear & Greed",
            value=int(fg) if fg == int(fg) else round(fg, 1),
            display=f"{int(fg)} ({fng.get('classification') or '—'})",
            zone_name=zname,
            thresholds="fundo ≤ 24 (medo extremo); neutro de 25 a 74; topo ≥ 75 (ganância extrema)",
            formula="valor publicado pelo Alternative.me Crypto Fear & Greed Index (0–100)",
            source="https://api.alternative.me/fng/ (sem chave)",
            extra={"classification": fng.get("classification"), "as_of": fng.get("date")},
        ))
    except Exception as exc:
        out.append(indicator(id="fear_greed", name="Fear & Greed", value=None, display=None, zone_name=None,
                             thresholds="fundo ≤ 24; topo ≥ 75", formula="índice 0–100 da Alternative.me",
                             source="https://api.alternative.me/fng/", error=str(exc)))

    # 10. Drawdown from ATH
    try:
        dd = last_price / ath_price - 1.0
        dd_pct = dd * 100.0
        if dd <= -0.70:
            zname = "fundo"
        elif dd > -0.20:
            zname = "topo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="drawdown_ath",
            name="Drawdown desde o ATH",
            value=round(dd, 4),
            display=f"{dd_pct:.1f}%",
            zone_name=zname,
            thresholds="fundo ≤ −70%; neutro entre −70% e −20%; topo > −20%",
            formula="preço atual / máxima histórica da série − 1",
            source="Máxima e preço atual sobre PriceUSD da CoinMetrics community API",
            extra={"ath_usd": round(ath_price, 2), "ath_date": ath_date.isoformat()},
        ))
    except Exception as exc:
        out.append(indicator(id="drawdown_ath", name="Drawdown desde o ATH", value=None, display=None, zone_name=None,
                             thresholds="fundo ≤ −70%; topo > −20%", formula="preço/ATH − 1",
                             source="CoinMetrics PriceUSD", error=str(exc)))

    # 11. Hash Ribbons
    try:
        hr_map = {d: v for d, v in cm["hashrate"]}
        hr_dates = [d for d in dates if d in hr_map]
        hr_vals = [hr_map[d] for d in hr_dates]
        # Prefer full hashrate series (includes early days without price) for MA stability
        full_dates = [d for d, _ in cm["hashrate"]]
        full_vals = [v for _, v in cm["hashrate"]]
        ma30 = sma(full_vals, 30)
        ma60 = sma(full_vals, 60)
        if ma30[-1] is None or ma60[-1] is None:
            raise ValueError("médias de hashrate ainda não maduras")
        cap_now = ma30[-1] < ma60[-1]
        # Recovery cross in the last 42 days
        recent_cross = False
        lookback = min(42, len(full_vals) - 1)
        for j in range(len(full_vals) - lookback, len(full_vals)):
            if ma30[j] is None or ma60[j] is None or ma30[j - 1] is None or ma60[j - 1] is None:
                continue
            if ma30[j - 1] < ma60[j - 1] and ma30[j] >= ma60[j]:
                recent_cross = True
                break
        if cap_now or recent_cross:
            zname = "fundo"
        else:
            zname = "neutro"
        out.append(indicator(
            id="hash_ribbons",
            name="Hash Ribbons",
            value=round(ma30[-1] / ma60[-1], 4),
            display=("capitulação (30 < 60)" if cap_now else "cruzamento de recuperação nos últimos 42 dias" if recent_cross else "sem capitulação"),
            zone_name=zname,
            thresholds="fundo se SMA-30 do hashrate < SMA-60 (capitulação) ou se houve cruzamento de recuperação nos últimos 42 dias; caso contrário neutro. Este indicador não vota topo.",
            formula="Hash Ribbons (Charles Edwards): SMA 30 vs SMA 60 do hashrate diário. Sinal de compra clássico = cruzamento da 30 para cima da 60 após capitulação.",
            source="CoinMetrics community API (HashRate, TH/s)",
            extra={
                "ma30": round(ma30[-1], 2),
                "ma60": round(ma60[-1], 2),
                "capitulation": cap_now,
                "recovery_cross_42d": recent_cross,
                "as_of": full_dates[-1].isoformat(),
            },
        ))
    except Exception as exc:
        out.append(indicator(id="hash_ribbons", name="Hash Ribbons", value=None, display=None, zone_name=None,
                             thresholds="fundo em capitulação ou cruzamento recente; senão neutro",
                             formula="SMA 30 vs SMA 60 do hashrate", source="CoinMetrics HashRate", error=str(exc)))

    # Attach shared price context on the first successful item is not needed; caller stores it.
    _ = (last_date, last_price, ath_date, ath_price, hr_dates if False else None)
    return out


def decide(indicators: list[dict]) -> dict:
    ok = [i for i in indicators if i.get("zone") and not i.get("error")]
    failed = [i for i in indicators if i.get("error") or not i.get("zone")]
    n_fundo = sum(1 for i in ok if i["zone"] == "fundo")
    n_neutro = sum(1 for i in ok if i["zone"] == "neutro")
    n_topo = sum(1 for i in ok if i["zone"] == "topo")
    n_ok = len(ok)
    need_share = math.ceil(FUNDO_SHARE * n_ok) if n_ok else 0
    need = max(FUNDO_MIN, need_share) if n_ok else FUNDO_MIN
    reasons: list[str] = []
    if n_ok < MIN_OK:
        answer = "NÃO"
        reasons.append(f"apenas {n_ok} indicadores calculados (mínimo {MIN_OK}); a regra cai em NÃO")
    elif n_fundo >= need:
        answer = "SIM"
        reasons.append(f"{n_fundo} de {n_ok} indicadores na zona de fundo (mínimo {need}, que é o maior entre {FUNDO_MIN} e {int(FUNDO_SHARE*100)}% de {n_ok})")
    else:
        answer = "NÃO"
        reasons.append(f"{n_fundo} de {n_ok} indicadores na zona de fundo; a regra exige pelo menos {need}")
    return {
        "answer": answer,
        "fundo": n_fundo,
        "neutro": n_neutro,
        "topo": n_topo,
        "ok": n_ok,
        "failed": len(failed),
        "need": need,
        "reasons": reasons,
        "failed_ids": [i["id"] for i in failed],
    }


def bake_html(answer: str) -> None:
    if not HTML_PATH.exists():
        log(f"aviso: {HTML_PATH} ainda não existe; JSON gerado assim mesmo")
        return
    html = HTML_PATH.read_text(encoding="utf-8")
    new, n = re.subn(
        r'(<p id="answer">)(.*?)(</p>)',
        lambda m: m.group(1) + answer + m.group(3),
        html,
        count=1,
        flags=re.S,
    )
    if n != 1:
        log("aviso: não achei o marcador <p id=\"answer\"> em index.html")
        return
    if new != html:
        HTML_PATH.write_text(new, encoding="utf-8")
        log(f"index.html atualizado com a resposta {answer}")
    else:
        log(f"index.html já estava com a resposta {answer}")


def sanity_flags(price: float, indicators: list[dict]) -> list[str]:
    flags: list[str] = []
    if not (1000 <= price <= 1_000_000):
        flags.append(f"preço fora da faixa plausível: {price}")
    for item in indicators:
        if item.get("error"):
            flags.append(f"{item['id']}: {item['error']}")
            continue
        if item["id"] == "fear_greed" and item["value"] is not None:
            if not (0 <= float(item["value"]) <= 100):
                flags.append(f"Fear & Greed fora de 0–100: {item['value']}")
        if item["id"] == "mvrv_z" and item.get("extra"):
            mvrv = item["extra"].get("mvrv")
            if mvrv is not None and not (0.2 <= mvrv <= 15):
                flags.append(f"MVRV fora da faixa habitual: {mvrv}")
        if item["id"] == "rsi_weekly" and item["value"] is not None:
            if not (0 <= float(item["value"]) <= 100):
                flags.append(f"RSI fora de 0–100: {item['value']}")
    return flags


def main() -> int:
    log("Buscando fontes públicas (sem chave)...")
    cm_rows = fetch_coinmetrics()
    cm = parse_cm(cm_rows)
    if len(cm["price"]) < 500:
        raise SystemExit(f"série de preços CoinMetrics insuficiente: {len(cm['price'])} dias")
    last_date = cm["dates"][-1]
    last_price = cm["price"][-1]
    ath_i = max(range(len(cm["price"])), key=lambda i: cm["price"][i])
    log(f"  CoinMetrics: {len(cm['price'])} dias com preço, último {last_date.isoformat()} = US$ {last_price:,.2f}")
    log(f"  ATH da série: {cm['dates'][ath_i].isoformat()} = US$ {cm['price'][ath_i]:,.2f}")

    revenue = None
    try:
        log("  blockchain.info miners-revenue...")
        revenue = fetch_blockchain_chart("miners-revenue")
        log(f"  miners-revenue: {len(revenue)} dias, último {revenue[-1][0].isoformat()} = US$ {revenue[-1][1]:,.0f}")
    except Exception as exc:
        log(f"  miners-revenue indisponível ({exc}); Puell usará IssTotUSD se possível")

    fng = None
    try:
        log("  alternative.me Fear & Greed...")
        fng = fetch_fear_greed()
        log(f"  Fear & Greed: {fng['value']} ({fng['classification']}) em {fng['date']}")
    except Exception as exc:
        log(f"  Fear & Greed indisponível ({exc})")

    indicators = compute_all(cm, revenue, fng)
    decision = decide(indicators)
    flags = sanity_flags(last_price, indicators)

    payload = {
        "generated_at": now_brt().isoformat(timespec="seconds"),
        "as_of": last_date.isoformat(),
        "timezone_note": "generated_at está em America/Sao_Paulo (UTC-3). as_of é a data UTC da última observação diária da CoinMetrics.",
        "answer": decision["answer"],
        "rule": {
            "sim_requires_fundo_share": FUNDO_SHARE,
            "sim_requires_fundo_count": FUNDO_MIN,
            "min_indicators_or_default_nao": MIN_OK,
            "need_this_run": decision["need"],
            "reasons": decision["reasons"],
            "text": (
                "SIM somente se pelo menos 70% dos indicadores calculados com sucesso "
                "estiverem na zona de fundo e esse número for no mínimo 5. "
                "Se menos de 6 indicadores forem calculados, a resposta é NÃO."
            ),
        },
        "summary": {
            "fundo": decision["fundo"],
            "neutro": decision["neutro"],
            "topo": decision["topo"],
            "ok": decision["ok"],
            "failed": decision["failed"],
            "failed_ids": decision["failed_ids"],
        },
        "price": {
            "usd": round(last_price, 2),
            "date": last_date.isoformat(),
            "source": "CoinMetrics community API, métrica PriceUSD (fim do dia UTC)",
            "ath_usd": round(cm["price"][ath_i], 2),
            "ath_date": cm["dates"][ath_i].isoformat(),
            "n_days": len(cm["price"]),
        },
        "indicators": indicators,
        "sanity_flags": flags,
        "disclaimer": "Conteúdo educacional. Não é recomendação de investimento nem opinião legal.",
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"Escrevi {DATA_PATH}")
    bake_html(decision["answer"])

    log("")
    log(f"Resposta: {decision['answer']}")
    log(f"Placar: {decision['fundo']} fundo / {decision['neutro']} neutro / {decision['topo']} topo  "
        f"(ok={decision['ok']}, falhas={decision['failed']}, mínimo para SIM={decision['need']})")
    for reason in decision["reasons"]:
        log(f"  {reason}")
    log("")
    log(f"{'indicador':<32} {'zona':<8} valor")
    for item in indicators:
        z = item.get("zone") or "erro"
        val = item.get("display") or item.get("error") or "—"
        log(f"  {item['name']:<30} {z:<8} {val}")
    if flags:
        log("")
        log("Alertas de sanidade:")
        for f in flags:
            log(f"  - {f}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        log(f"ERRO: {exc}")
        raise
