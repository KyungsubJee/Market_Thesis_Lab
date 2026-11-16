"""Run the checkout's native LangGraph and roles with an isolated Codex transport."""

from collections import Counter
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import re
import sys
from threading import Lock
from time import perf_counter
from types import ModuleType
from uuid import uuid4
from zoneinfo import ZoneInfo

from framework_validation.live_runner import CodexModel, STRING, array_schema, object_schema

from .frozen import FrozenData


FACTORIES = {
    "create_market_analyst": "Market Analyst",
    "create_sentiment_analyst": "Sentiment Analyst",
    "create_news_analyst": "News Analyst",
    "create_fundamentals_analyst": "Fundamentals Analyst",
    "create_bull_researcher": "Bull Researcher",
    "create_bear_researcher": "Bear Researcher",
    "create_research_manager": "Research Manager",
    "create_trader": "Trader",
    "create_aggressive_debator": "Aggressive Analyst",
    "create_conservative_debator": "Conservative Analyst",
    "create_neutral_debator": "Neutral Analyst",
    "create_portfolio_manager": "Portfolio Manager",
}
DEPENDENCIES = Path(__file__).with_name(".deps")


def module(name, path=None, **members):
    value = ModuleType(name)
    if path is not None:
        value.__path__ = [str(path)]
    value.__dict__.update(members)
    sys.modules[name] = value


def load_native(checkout, frozen):
    source = checkout / "tradingagents"
    init = (source / "__init__.py").read_text(encoding="utf-8")
    version = re.search(r'__version__\s*=\s*"([^"]+)"', init)[1]
    if "tradingagents.agents" in sys.modules:
        raise RuntimeError("Start a new benchmark process to isolate TradingAgents data providers")
    if DEPENDENCIES.exists():
        sys.path.insert(0, str(DEPENDENCIES))
    os.environ.update(LANGCHAIN_TRACING_V2="false", LANGSMITH_TRACING="false")
    module("tradingagents", source, __version__=version)
    module("tradingagents.graph", source / "graph")
    module("tradingagents.dataflows", source / "dataflows")
    module("tradingagents.dataflows.vendors", source / "dataflows" / "vendors")
    module("tradingagents.dataflows.vendors.yahoo", source / "dataflows" / "vendors" / "yahoo")
    module("tradingagents.dataflows.router", route_to_vendor=frozen.route,
           no_data_available=lambda error: str(error), vendor_unavailable=lambda _tool, error: str(error))
    module("tradingagents.dataflows.vendors.yahoo.snapshot", build_verified_market_snapshot=frozen.market_snapshot)
    module("tradingagents.dataflows.vendors.yahoo.fundamentals", get_company_profile=lambda *_: {})
    module("tradingagents.agents.post_screen", jev_screen=lambda *_: None)
    module("tradingagents.dataflows.vendors.stocktwits",
           fetch_stocktwits_messages=lambda *_a, **_k: frozen.social_missing("StockTwits"))
    module("tradingagents.dataflows.vendors.reddit",
           DEFAULT_SUBREDDITS=("stocks",), CRYPTO_SUBREDDITS=("CryptoCurrency",),
           subreddits_for=lambda *_: ("stocks",),
           fetch_reddit_posts=lambda *_a, **_k: frozen.social_missing("Reddit"))
    setup = importlib.import_module("tradingagents.graph.setup")
    propagation = importlib.import_module("tradingagents.graph.propagation")
    conditional = importlib.import_module("tradingagents.graph.conditional_logic")
    return version, setup, propagation, conditional


def strict_schema(value):
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    schema = {key: strict_schema(item) for key, item in value.items() if key != "default"}
    if schema.get("type") == "object" and "properties" in schema:
        schema.update(required=list(schema["properties"]), additionalProperties=False)
    return schema


def native_messages(prompt):
    if isinstance(prompt, str):
        return [{"type": "human", "content": prompt}]
    if hasattr(prompt, "to_messages"):
        prompt = prompt.to_messages()
    return [
        item if isinstance(item, dict) else item.model_dump(exclude_none=True)
        for item in prompt
    ]


def dummy_value(schema, root):
    if "$ref" in schema:
        return dummy_value(root["$defs"][schema["$ref"].split("/")[-1]], root)
    if "anyOf" in schema:
        return None if any(item.get("type") == "null" for item in schema["anyOf"]) else dummy_value(schema["anyOf"][0], root)
    if "enum" in schema:
        return "Hold" if "Hold" in schema["enum"] else schema["enum"][0]
    if schema["type"] == "object":
        return {key: dummy_value(value, root) for key, value in schema["properties"].items()}
    return {"number": 0, "integer": 0, "string": "Offline graph test.", "boolean": False}[schema["type"]]


class Recorder:
    def __init__(self, model, timeout, max_calls, fixture, *, dry=False):
        self.dry = dry
        self.transport = None if dry else CodexModel(model, timeout, max_calls)
        self.max_calls = max_calls
        self.cutoff = fixture["as_of"]
        self.trade_date = datetime.fromisoformat(self.cutoff).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        self.calls = []
        self.lock = Lock()

    def invoke(self, role, prompt, *, tools=(), native_schema=None):
        from langchain_core.messages import AIMessage
        from langchain_core.utils.function_calling import convert_to_openai_tool

        messages = native_messages(prompt)
        specifications = [convert_to_openai_tool(tool)["function"] for tool in tools]
        if tools:
            names = [tool.name for tool in tools]
            action = object_schema(name={"type": "string", "enum": names}, args_json=STRING)
            schema = object_schema(content=STRING, tool_calls=array_schema(action))
            guidance = (
                "Simulate only the native graph's listed frozen tool calls in tool_calls, with each args_json an "
                "encoded JSON object. For a report return an empty tool_calls list and report in content. "
                "Querying the same tool cannot recover data marked absent."
            )
        elif native_schema is not None:
            schema = strict_schema(native_schema.model_json_schema())
            guidance = "Return the native role's structured output."
        else:
            schema = object_schema(content=STRING)
            guidance = "Return the native role's response in content."
        text = (
            f"Run this TradingAgents role on the fixed SOXL research snapshot through {self.cutoff}. "
            "Use the native role instructions below. Do not browse, open files, execute tools or bring in facts from memory. "
            "Only the Python graph runs any frozen tools requested. Cite frozen evidence IDs for factual claims, "
            "and disclose missing data. Keep native English rating/action labels while writing explanatory prose in Korean. "
            + guidance + "\n" + json.dumps({"messages": messages, "frozen_tools": specifications}, ensure_ascii=False)
        )
        record = {
            "role": role, "phase": "tool_enabled" if tools else "structured" if native_schema else "plain",
            "prompt": text, "prompt_chars": len(text), "native_messages": messages,
            "tool_specs": specifications, "usage": None,
        }
        with self.lock:
            if len(self.calls) >= self.max_calls:
                raise RuntimeError(f"Benchmark model-call budget exhausted ({self.max_calls})")
            record["call_id"] = len(self.calls) + 1
            self.calls.append(record)
            print(f"call {record['call_id']} {role} ({record['phase']})", file=sys.stderr, flush=True)
        started = perf_counter()
        try:
            response = self.mock(role, messages, tools, schema) if self.dry else self.transport.invoke(text, schema, record)
            record["response"] = response
            if native_schema is not None:
                return native_schema.model_validate(response)
            requests = []
            for request in response.get("tool_calls", []):
                args = json.loads(request["args_json"])
                if request["name"] not in names or not isinstance(args, dict):
                    raise ValueError("Invalid frozen tool invocation")
                requests.append({"name": request["name"], "args": args, "id": uuid4().hex, "type": "tool_call"})
            record["requested_tools"] = requests
            return AIMessage(content=response["content"], tool_calls=requests)
        except Exception as exc:
            record["error"] = str(exc)
            raise
        finally:
            record["seconds"] = round(perf_counter() - started, 3)

    def mock(self, role, messages, tools, schema):
        if tools:
            if not any(message.get("type") == "tool" for message in messages):
                name = {
                    "Market Analyst": "get_verified_market_snapshot", "News Analyst": "get_global_news",
                    "Fundamentals Analyst": "get_fundamentals",
                }[role]
                return {"content": "", "tool_calls": [{"name": name, "args_json": json.dumps({"curr_date": self.trade_date})}]}
            return {"content": "Offline graph test report.", "tool_calls": []}
        return dummy_value(schema, schema)


class RoleModel:
    def __init__(self, recorder, role):
        self.recorder = recorder
        self.role = role

    def invoke(self, prompt):
        return self.recorder.invoke(self.role, prompt)

    __call__ = invoke

    def bind_tools(self, tools):
        from langchain_core.runnables import RunnableLambda
        return RunnableLambda(lambda prompt: self.recorder.invoke(self.role, prompt, tools=tools))

    def with_structured_output(self, schema):
        from langchain_core.runnables import RunnableLambda
        return RunnableLambda(lambda prompt: self.recorder.invoke(self.role, prompt, native_schema=schema))


def run_native(checkout, recorder, frozen, *, max_tool_rounds):
    version, setup, propagation, conditional = load_native(checkout, frozen)
    for name, role in FACTORIES.items():
        original = getattr(setup, name)
        setattr(setup, name, lambda _llm, original=original, role=role: original(RoleModel(recorder, role)))

    context = (
        "Instrument: held SOXL, a daily +3x leveraged semiconductor ETF. No position size or cash is known. "
        "Assess a short tactical horizon without treating multi-day performance as three times the cumulative index return. "
        "SOXX and SPY are market-price ETF proxies. ETF holdings, SEC prospectus, rates, completed bars and "
        "window alignment are available through frozen tools. Do not infer official tracking error, current-date NAV, "
        "corporate EPS, consensus, news or social sentiment. No trading is executed."
    )
    initial = propagation.Propagator().create_initial_state(
        "SOXL", recorder.trade_date, asset_type="etf", instrument_context=context,
        portfolio_context="SOXL is held. No size or cash supplied; do not assign a position size.",
    )
    graph = setup.GraphSetup(None, None, conditional.ConditionalLogic(1, 1), max_tool_rounds)
    result = graph.setup_graph().compile().invoke(initial, config={"recursion_limit": 100})
    final = {key: value for key, value in result.items() if key != "messages"}
    return version, final


def summarize(calls):
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    usage = {}
    for key in keys:
        values = [(call.get("usage") or {}).get(key) for call in calls]
        usage[key] = sum(values) if all(value is not None for value in values) else None
    durations = [call.get("seconds") for call in calls]
    return {
        "model_calls": len(calls),
        "role_calls": dict(Counter(call["role"] for call in calls)),
        "input_chars": sum(call["prompt_chars"] for call in calls),
        "summed_model_seconds": round(sum(durations), 3) if all(value is not None for value in durations) else None,
        "calls_with_usage": sum(call["usage"] is not None for call in calls),
        "usage": usage,
        "billed_usd": None,
    }
