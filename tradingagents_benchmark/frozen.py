"""Provide TradingAgents data tools from the benchmark's frozen evidence only."""

import json
from threading import Lock


MARKET_IDS = (
    "soxl-stock-price-window", "us-sp500-price-window", "soxl-sector-price-window", "soxl-etf-comparison",
)
FUND_IDS = ("soxl-etf-issuer-terms", "soxl-sec-etf-prospectus", "soxl-etf-holdings", "soxl-soxx-proxy-description")
MACRO_IDS = ("us-fred-dgs10", "us-fred-dff", "us-fred-cpiaucsl", "us-fred-unrate")
MACRO_ALIASES = {
    "10y_treasury": MACRO_IDS[0], "fed_funds_rate": MACRO_IDS[1],
    "cpi": MACRO_IDS[2], "unemployment": MACRO_IDS[3],
    **{key.removeprefix("us-fred-"): key for key in MACRO_IDS},
}


class FrozenData:
    def __init__(self, fixture):
        self.evidence = {item["id"]: item for item in fixture["evidence"]}
        self.calls = []
        self.lock = Lock()

    def cards(self, ids, through=None):
        fields = ("id", "source_url", "text", "metrics", "observation_date", "available_at")
        return [
            {field: item[field] for field in fields if field in item}
            for key in ids if (item := self.evidence.get(key))
            and (not through or (item.get("observation_date") or item["available_at"][:10]) <= through)
        ]

    def result(self, tool, args, cards, note, *, extra=None):
        value = {"note": note, "evidence": cards, **(extra or {})}
        with self.lock:
            self.calls.append({"tool": tool, "args": args, "evidence_ids": [item["id"] for item in cards], "result": value})
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def market_snapshot(self, symbol, end, lookback):
        if symbol.upper() != "SOXL":
            return self.result("get_verified_market_snapshot", (symbol, end, lookback), [],
                               "This verified-market snapshot is available only for SOXL.")
        cards = self.cards(MARKET_IDS, end)
        observations = self.evidence.get(MARKET_IDS[0], {}).get("observations", [])
        bars = [row for row in observations if row["session_date"] <= end]
        return self.result(
            "get_verified_market_snapshot", (symbol, end, lookback), cards,
            "Frozen USD market-price data. SOXX is a nonleveraged ETF proxy, not the official index or NAV.",
            extra={"latest_bar": bars[-1] if bars else None,
                   "recent_session_closes": [[row["session_date"], row["close"]] for row in bars[-max(1, lookback):]]},
        )

    def social_missing(self, platform):
        return self.result(platform, (), [], f"{platform} posts were not captured. This is missing evidence, not neutral sentiment.")

    def route(self, tool, *args):
        if tool == "get_stock_data":
            symbol, start, end = args
            if symbol.upper() != "SOXL":
                return self.result(tool, args, [], "Raw stock-data tool is scoped to SOXL.")
            observations = self.evidence.get(MARKET_IDS[0], {}).get("observations", [])
            bars = [
                [row[key] for key in ("session_date", "open", "high", "low", "close", "volume")]
                for row in observations if start <= row["session_date"] <= end
            ]
            return self.result(
                tool, args, self.cards(MARKET_IDS[:1], end), "Frozen SOXL regular-session OHLCV, no dividends.",
                extra={"columns": ["session_date", "open", "high", "low", "close", "volume"], "rows": bars},
            )
        if tool == "get_indicators":
            symbol, indicator, end, _ = args
            if symbol.upper() != "SOXL":
                return self.result(tool, args, [], "Technical-indicator tool is scoped to SOXL.")
            return self.result(
                tool, args, self.cards(MARKET_IDS, end),
                f"Requested indicator {indicator!r} is not a separate verified series in this snapshot. "
                "Only the existing frozen price/volume metrics are supplied.",
            )
        if tool == "get_fundamentals":
            return self.result(
                tool, args, self.cards(FUND_IDS, args[1]),
                "SOXL is a leveraged ETF. These are the terms, holdings, proxy definition and prospectus; not corporate XBRL.",
            )
        if tool == "get_news":
            _, start, end = args
            cards = [item for item in self.cards(FUND_IDS, end) if item.get("observation_date", "") >= start]
            return self.result(tool, args, cards, "No article feed was captured. These are dated fund disclosures only.")
        if tool == "get_global_news":
            return self.result(tool, args, self.cards(MACRO_IDS, args[0]), "No global news articles were captured. Observed macro only.")
        if tool == "get_macro_indicators":
            indicator, end, _ = args
            key = MACRO_ALIASES.get(indicator.casefold())
            return self.result(tool, args, self.cards((key,), end) if key else [],
                               "Frozen latest macro observation only; no release-vintage series was supplied.")
        return self.result(tool, args, [], f"{tool} is unavailable in this frozen ETF snapshot.")
