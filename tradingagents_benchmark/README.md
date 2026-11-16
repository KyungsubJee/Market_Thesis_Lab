# TradingAgents frozen SOXL comparison

The adapter runs the checked-out TradingAgents graph with the same saved SOXL snapshot and Codex CLI model used by the prototype. Run these commands from the project root.

```powershell
python -m pip install --target tradingagents_benchmark/.deps "langgraph==1.2.20" "langchain-core==1.8.0"
python -B -m tradingagents_benchmark --evidence research_prototype/runs/soxl-full/evidence.json --output tradingagents_benchmark/runs/soxl-native-dry.json --dry-run
python -B -m tradingagents_benchmark --evidence research_prototype/runs/soxl-full/evidence.json --output tradingagents_benchmark/runs/soxl-native.json
```

The dry run uses synthetic model responses to exercise native LangGraph, injected tool state, four analyst branches, two investment debate turns, three risk debate turns, and the final portfolio decision. The real command requires a signed-in Codex CLI. An existing output is preserved. Runtime failures are recorded alongside any completed model and tool turns.

The comparison preserves the checkout's graph setup, native role factories and prompts, Pydantic decision models, and routing. The adapter bypasses package initialization and the outer trading runner, including model clients, memory settlement, checkpoints, and native report output. It wraps prompts for the frozen-only transport, translates native tool-call/structured-output bindings, and records every model request and response. Public evidence never refreshes during execution. Available facts are projected by tool, so equality means the same available evidence snapshot and cutoff, not identical role prompts or guaranteed retrieval of every record.

SOXL has no operating-company EPS in the capture. The tools expose ETF terms, holdings, price/volume and macro observations, and mark unavailable articles, social posts, predictions, corporate statements, and official NAV/index tracking. The adapter supplies the same short tactical horizon and existing holding without position size or cash. The result reports elapsed wall time separately from the sum of overlapping model-call times. Codex usage may omit fields, which remain null.
