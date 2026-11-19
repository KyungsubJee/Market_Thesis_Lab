"""Supported nonstandard holding profiles and verified Korean security identifiers."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Instrument:
    name: str
    country: str
    kind: str
    market_proxy: str
    exposure_proxy: str
    comparison: str
    macro_scope: str


KOREAN_MARKET = "KOSPI"
INSTRUMENTS = {
    "BOIL": Instrument("ProShares Ultra Bloomberg Natural Gas", "US", "commodity_leveraged", "SPY", "UNG",
                       "UNG is a separate unlevered natural gas futures fund and not the formal benchmark.",
                       "US natural gas supply, demand, storage, weather, LNG and futures roll."),
    "KOLD": Instrument("ProShares UltraShort Bloomberg Natural Gas", "US", "commodity_leveraged", "SPY", "UNG",
                       "UNG is a separate unlevered natural gas futures fund and not the formal benchmark.",
                       "US natural gas supply, demand, storage, weather, LNG and futures roll."),
    "SOXS": Instrument("Direxion Daily Semiconductor Bear 3X ETF", "US", "sector_leveraged", "SPY", "SOXX",
                       "SOXX is an unlevered US semiconductor ETF market-price proxy, not official index/NAV.",
                       "US macro rates and semiconductor demand and valuation."),
    "005930": Instrument("삼성전자", "KR", "equity", KOREAN_MARKET, "091160",
                         "KODEX 반도체 is a Korean semiconductor equity ETF proxy.",
                         "Korean equities, global semiconductor demand, memory cycles and FX sensitivity."),
    "000660": Instrument("SK하이닉스", "KR", "equity", KOREAN_MARKET, "091160",
                         "KODEX 반도체 is a Korean semiconductor equity ETF proxy.",
                         "Korean equities, global semiconductor demand, memory cycles and FX sensitivity."),
    "498400": Instrument("KODEX 200타겟위클리커버드콜", "KR", "covered_call", KOREAN_MARKET, "069500",
                         "069500 is a Korean-listed KOSPI 200 equity ETF without covered calls, not the formal fund index.",
                         "Korean equity trend, option premia and volatility."),
    "486290": Instrument("TIGER 미국나스닥100타겟데일리커버드콜", "KR", "covered_call", KOREAN_MARKET, "133690",
                         "133690 is a Korean-listed KRW Nasdaq-100 equity ETF without covered calls. Both trade in Seoul; "
                         "returns do not measure contemporaneous US regular-session Nasdaq-100.",
                         "Nasdaq demand, FX exposure, option premia and volatility."),
}
KNOWN_NAMES = {
    "KO": "The Coca-Cola Company",
    "META": "Meta Platforms, Inc.",
    "NVDA": "NVIDIA Corporation",
    "SOXL": "Direxion Daily Semiconductor Bull 3X ETF",
}


def canonical_holding(value):
    text = " ".join(value.strip().upper().split())
    if re.fullmatch(r"\d{6}\.KS", text):
        return text.removesuffix(".KS")
    compact = text.replace(" ", "")
    for ticker, instrument in INSTRUMENTS.items():
        if instrument.country == "KR" and compact == instrument.name.upper().replace(" ", ""):
            return ticker
    return text.replace(".", "-")


def profile(ticker):
    return INSTRUMENTS.get(ticker)


def holding_label(ticker, sources=()):
    instrument = profile(ticker)
    name = instrument.name if instrument else KNOWN_NAMES.get(ticker)
    if not name:
        prefix = f"{ticker.lower()}-"
        name = next((
            item["issuer_name"].strip() for item in sources
            if item.get("id", "").lower().startswith(prefix) and isinstance(item.get("issuer_name"), str)
            and item["issuer_name"].strip()
        ), None)
    return f"{ticker} ({name})" if name and name.casefold() != ticker.casefold() else ticker


def listing_label(ticker, listing_name=None):
    if not isinstance(listing_name, str):
        return ticker
    name = re.sub(r"(?:\s+-\s+|\s+)Common Stock$", "", listing_name.strip(), flags=re.I).strip()
    return f"{ticker} ({name})" if name and name.casefold() != ticker.casefold() else ticker


def required_terms(ticker):
    instrument = INSTRUMENTS[ticker]
    return f"{ticker}-kr-periodic-results" if instrument.kind == "equity" else f"{ticker.lower()}-fund-terms"
