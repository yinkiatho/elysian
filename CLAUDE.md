# Claude Code Configuration - Elysian

## Behavioral Rules (Always Enforced)

- Do what has been asked; nothing more, nothing less
- NEVER create files unless they're absolutely necessary for achieving your goal
- ALWAYS prefer editing an existing file to creating a new one
- NEVER proactively create documentation files (*.md) or README files unless explicitly requested
- NEVER save working files, text/mds, or tests to the root folder
- Never continuously check status after spawning a swarm — wait for results
- ALWAYS read a file before editing it
- NEVER commit secrets, credentials, or .env files

## File Organization

- NEVER save to root folder — use the directories below
- Use `/src` for source code files
- Use `/tests` for test files
- Use `/docs` for documentation and markdown files
- Use `/config` for configuration files
- Use `/scripts` for utility scripts
- Use `/examples` for example code

---

## Elysian System Architecture

Elysian is a **single‑asyncio‑event‑loop, event‑driven, multi‑venue cryptocurrency trading system**. These guidelines describe the current design. You may propose changes, but any deviation must be justified by a performance benchmark or a concrete scalability requirement.

### Core Design

- **Single event loop** – all async I/O, feeds, and strategy hooks run in one loop. No threading, no `new_event_loop()` in threads.
- **Six‑stage pipeline** (immutable in production, changeable in design):
  1. Market data (WebSocket → feed managers)
  2. EventBus (typed, immutable events to subscribers)
  3. Strategy (`compute_weights`)
  4. Risk (portfolio optimisation)
  5. Execution (order intent → exchange REST)
  6. Exchange (Binance/Aster REST + user‑data WS)

  Stages 3→5 use **direct method calls** (not EventBus) because they are sequential. Only `RebalanceCompleteEvent` goes on the bus after execution.

- **Two‑bus separation** – each strategy has a shared bus (market data) and a private bus (account events). Prevents cross‑strategy fill contamination.
- **ShadowBook as authoritative ledger** – per‑strategy source of truth for positions, cash, locked funds, PnL. `Portfolio` is a read‑only aggregator.
- **RebalanceFSM** – the only entry point to the Stage 3→5 pipeline. FSM states: `IDLE → COMPUTING → VALIDATING → EXECUTING → COOLDOWN → IDLE`.

### Invariants (Must Never Break)

- **No threading** – never use `ThreadPoolExecutor`, `threading.Thread`, or create a second event loop.
- **ShadowBook is per‑strategy truth** – always read `self._shadow_book` for a strategy’s own state, not `self.portfolio`.
- **FSM controls rebalance cycles** – never call `optimizer.validate()` or `execution_engine.execute()` directly from a strategy.
- **`compute_weights()` is pure** – no I/O, no EventBus publishes, no side effects. Returns `{}` or `None` to skip a cycle.
- **Sub‑account exchange per strategy** – never share API keys or instantiate an exchange connector manually.
- **`EventBus.publish()` is awaited** – never fire‑and‑forget inside a strategy hook (exceptions: exchange connectors for DB writes).
- **No `asyncio.create_task()` inside strategy hooks** – breaks backpressure. Use `self.run_heavy()` for CPU‑heavy work.
- **No stablecoin keys in weight dicts** – return `{}` for all‑cash, not `{"USDT": 1.0}`.

### Modification Guidelines

- You may **relax** an invariant if a benchmark shows the current design limits throughput or latency. Include the benchmark command and results in the PR.
- You may **add** new event types, feed managers, or exchange connectors following the existing patterns (see checklists in `docs/architecture.md`).
- You may **replace** the EventBus with a streaming broker (e.g., Redis Streams) if you preserve the backpressure semantics and event immutability.
- When in doubt, preserve the **single‑loop, no‑shared‑mutable‑state** property – it eliminates entire classes of concurrency bugs.


### 1. Current Six-Stage Pipeline

Every market signal travels exactly this path: 

```
Stage 1  Market Data     WebSocket feeds → KlineClientManager / OBClientManager workers
Stage 2  EventBus        Typed, immutable event dispatch to all subscribers
Stage 3  Strategy        on_kline / on_orderbook_update → compute_weights(**ctx)
Stage 4  Risk            PortfolioOptimizer.validate() → ValidatedWeights
Stage 5  Execution       ExecutionEngine.execute() → OrderIntent → Exchange REST
Stage 6  Exchange        BinanceSpotExchange / AsterSpotExchange REST + user-data WS
```

**Rule**: Stages 3→5 use direct method calls (not EventBus) because they are inherently sequential. Only `RebalanceCompleteEvent` is published to the bus post-execution for observability. Never re-route this chain through the EventBus.

### 2. Event-First Design

- All inter-component notifications are **frozen dataclasses** (`@dataclass(frozen=True)`) defined in `core/events.py`. Never pass mutable dicts between components.
- Events are the sole mechanism for notifying downstream consumers of state changes. No polling, no direct callbacks outside the EventBus.
- The canonical event types are: `KlineEvent`, `OrderBookUpdateEvent`, `OrderUpdateEvent`, `BalanceUpdateEvent`, `RebalanceCompleteEvent`, `LifecycleEvent`, `RebalanceCycleEvent`. Adding a new event type requires adding it to `EventType` enum **and** the frozen dataclass in `events.py`.
- `EventBus.publish()` is always `await`ed — this is intentional backpressure. Never fire-and-forget.

### 3. Two-Bus Architecture (Shared vs Private)

Every strategy uses **two separate EventBus instances**:

| Bus | Carries | Subscribed by |
|-----|---------|---------------|
| `shared_event_bus` | `KLINE`, `ORDERBOOK_UPDATE` | All strategies, ShadowBook (`_on_kline`) |
| `private_event_bus` | `ORDER_UPDATE`, `BALANCE_UPDATE`, `REBALANCE_COMPLETE` | One strategy only, its ShadowBook |

**Rule**: Never subscribe a strategy to account events (`ORDER_UPDATE`, `BALANCE_UPDATE`) on the shared bus. Cross-strategy fill contamination will corrupt all ShadowBooks. The private bus is created in `_create_sub_account_exchange()` and injected by the runner — never create it inside a strategy.

### 4. ShadowBook is the Authoritative Ledger (Not Portfolio)

- Each strategy owns exactly one `ShadowBook`. It is the source of truth for that strategy's positions, cash, weights, PnL, and outstanding orders.
- `Portfolio` is a **read-only aggregator** — it sums ShadowBook NAVs for monitoring only. Never write to Portfolio directly.
- Strategies must read `self._shadow_book` (not `self.portfolio`) when computing weights:
  ```python
  # CORRECT
  my_qty = self._shadow_book.position("ETHUSDT").quantity
  my_cash = self._shadow_book.free_cash

  # WRONG — Portfolio is an aggregate, not this strategy's ledger
  my_qty = self.portfolio.positions.get("ETHUSDT")
  ```
- `free_cash` (not `cash`) is the correct field for computing how much capital is available — it excludes locked LIMIT BUY reservations.

### 5. RebalanceFSM Controls All Rebalance Cycles

- `RebalanceFSM` is the only permitted entry point to the Stage 3→5 pipeline. Never call `optimizer.validate()` or `execution_engine.execute()` directly from strategy code.
- Trigger a cycle with `await self.request_rebalance(**ctx)`. It returns `False` silently if the FSM is busy — this is by design. Do not retry in a tight loop.
- FSM states: `IDLE → COMPUTING → VALIDATING → EXECUTING → COOLDOWN → IDLE`. A suspended FSM (`SUSPENDED`) blocks all new cycles until `resume_rebalancing()` is called.
- `compute_weights(**ctx)` must be **pure**: no exchange calls, no EventBus publishes, no mutable state mutations. It returns `{}` or `None` to skip a cycle.

### 6. Sub-Account Mode (Always Active)

- Every strategy gets a dedicated exchange connector with its own API keys, resolved from env vars as `{VENUE}_API_KEY_{strategy_id}` / `{VENUE}_API_SECRET_{strategy_id}`.
- The sub-account exchange is created in `StrategyRunner._create_sub_account_exchange()`. Never instantiate `BinanceSpotExchange` directly inside a strategy.
- Sub-account `strategy_id` values must be unique integers across all loaded strategies. Reusing an ID corrupts ShadowBook routing.

### 7. Concurrency Model (Single Event Loop)

- **One asyncio event loop governs everything.** No `ThreadPoolExecutor` for I/O, no `new_event_loop()` in threads.
- Each `KlineClientManager` / `OBClientManager` runs: 1 reader task + up to 8 worker tasks, all in the same loop.
- `EventBus.publish()` is awaited sequentially per subscriber — this provides natural backpressure. If a strategy hook is slow, the feed worker waits. Do not spawn `asyncio.create_task()` inside hooks to escape this; it breaks backpressure.
- CPU-heavy strategy calculations must be offloaded via `self.run_heavy(fn, *args)` (uses `ProcessPoolExecutor`). `fn` must be a top-level picklable function — not a lambda, method, or closure.
- The only permitted `asyncio.create_task()` calls are in `StrategyRunner` for feed coroutines and in exchange connectors for fire-and-forget DB writes (`_record_trade`).

### 8. Configuration Hierarchy

Config priority (highest wins):
```
strategy risk: section (strategy_NNN.yaml)
  ↓ overrides
venue_configs["{asset_type}_{venue}"] (trading_config.yaml)
  ↓ overrides
global risk: section (trading_config.yaml)
```

- `cfg.effective_risk_for(strategy_id=N)` returns the fully-merged `RiskConfig` for strategy N.
- Never hardcode risk parameters in strategy code. Read from `self.strategy_config.params` or `self.cfg`.
- Strategy YAML `strategy_id` must match the env var suffix: `BINANCE_API_KEY_{strategy_id}`.

### 9. Adding a New Strategy (Checklist)

1. Subclass `SpotStrategy` in `elysian_core/strategy/`.
2. Create `elysian_core/config/strategies/strategy_NNN_<name>.yaml` with a unique `strategy_id`.
3. Add env vars `{VENUE}_API_KEY_{strategy_id}` and `{VENUE}_API_SECRET_{strategy_id}` to `.env`.
4. Register the YAML path in `run_strategy.py` `strategy_config_yamls` list.
5. Implement `compute_weights(**ctx)` as a pure function — no side effects.
6. All initialization that needs `self.cfg` or `self.strategy_config` goes in `on_start()`, not `__init__()`.
7. Use bounded collections (`deque(maxlen=N)`, `NumpySeries(maxlen=N)`) for all rolling state.
8. Gate rebalance triggers: check `time.monotonic() - self._last_rebalance_ts > self._rebalance_interval` before calling `request_rebalance()`.

### 10. Adding a New Exchange Connector (Checklist)

1. Create `NewExchangeKlineFeed`, `NewExchangeOrderBookFeed` subclassing `AbstractDataFeed`.
2. Create `NewExchangeKlineClientManager`, `NewExchangeOrderBookClientManager` subclassing `KlineClientManager` / `OrderBookClientManager`.
3. Create `NewExchangeUserDataClientManager` with `register()`, `set_event_bus()`, `start()`, `stop()`.
4. Worker coroutine must: update feed state first (sync), then `await self._event_bus.publish(...)` (async). Order matters — strategy hooks see consistent state.
5. Implement exponential backoff reconnection: start at 1s, double, cap at 60s.
6. Create `NewExchangeSpotExchange(SpotExchangeConnector)` with all abstract methods.
7. Add `Venue.NEW_EXCHANGE` to `core/enums.py`.
8. Register in `StrategyRunner._exchange_connector_callables` and `_setup_exchanges()`.

### 11. What NOT to Do

| Anti-pattern | Why it breaks Elysian |
|---|---|
| `await event_bus.publish()` inside `compute_weights()` | compute_weights must be pure; EventBus is not reentrant during FSM execution |
| Calling `optimizer.validate()` directly in a strategy | Bypasses FSM state machine; cooldown and error recovery break |
| Subscribing to `ORDER_UPDATE` on the shared bus | All strategies receive all fills; ShadowBook corruption |
| `threading.Thread` or `loop.run_in_executor` for I/O | Breaks single-loop invariant; use `asyncio.create_task` or `run_heavy` |
| Reading `self.portfolio` for per-strategy state | Portfolio is an aggregate; use `self._shadow_book` |
| `asyncio.create_task()` inside `on_kline` | Escapes backpressure; can cause unbounded queue growth |
| Mutable objects in frozen event dataclasses | Downstream subscribers mutate shared state; always use `copy.deepcopy` if you need a snapshot |

---

## Build & Test

```bash
# Build
npm run build

# Test
npm test

# Lint
npm run lint
```

- ALWAYS run tests after making code changes
- ALWAYS verify build succeeds before committing

## Security Rules

- NEVER hardcode API keys, secrets, or credentials in source files
- NEVER commit .env files or any file containing secrets
- Always validate user input at system boundaries
- Always sanitize file paths to prevent directory traversal
- Run `npx @claude-flow/cli@latest security scan` after security-related changes

## Concurrency: 1 MESSAGE = ALL RELATED OPERATIONS

- All operations MUST be concurrent/parallel in a single message
- Use Claude Code's Task tool for spawning agents, not just MCP
- ALWAYS batch ALL todos in ONE TodoWrite call (5-10+ minimum)
- ALWAYS spawn ALL agents in ONE message with full instructions via Task tool
- ALWAYS batch ALL file reads/writes/edits in ONE message
- ALWAYS batch ALL Bash commands in ONE message

## Swarm Orchestration

- MUST initialize the swarm using CLI tools when starting complex tasks
- MUST spawn concurrent agents using Claude Code's Task tool
- Never use CLI tools alone for execution — Task tool agents do the actual work
- MUST call CLI tools AND Task tool in ONE message for complex work

### 3-Tier Model Routing (ADR-026)

| Tier | Handler | Latency | Cost | Use Cases |
|------|---------|---------|------|-----------|
| **1** | Agent Booster (WASM) | <1ms | $0 | Simple transforms (var→const, add types) — Skip LLM |
| **2** | Haiku | ~500ms | $0.0002 | Simple tasks, low complexity (<30%) |
| **3** | Sonnet/Opus | 2-5s | $0.003-0.015 | Complex reasoning, architecture, security (>30%) |

- Always check for `[AGENT_BOOSTER_AVAILABLE]` or `[TASK_MODEL_RECOMMENDATION]` before spawning agents
- Use Edit tool directly when `[AGENT_BOOSTER_AVAILABLE]`

## Swarm Configuration & Anti-Drift

- ALWAYS use hierarchical topology for coding swarms
- Keep maxAgents at 6-8 for tight coordination
- Use specialized strategy for clear role boundaries
- Use `raft` consensus for hive-mind (leader maintains authoritative state)
- Run frequent checkpoints via `post-task` hooks
- Keep shared memory namespace for all agents

```bash
npx @claude-flow/cli@latest swarm init --topology hierarchical --max-agents 8 --strategy specialized
```

## Swarm Execution Rules

- ALWAYS use `run_in_background: true` for all agent Task calls
- ALWAYS put ALL agent Task calls in ONE message for parallel execution
- After spawning, STOP — do NOT add more tool calls or check status
- Never poll TaskOutput or check swarm status — trust agents to return
- When agent results arrive, review ALL results before proceeding

## V3 CLI Commands

### Core Commands

| Command | Subcommands | Description |
|---------|-------------|-------------|
| `init` | 4 | Project initialization |
| `agent` | 8 | Agent lifecycle management |
| `swarm` | 6 | Multi-agent swarm coordination |
| `memory` | 11 | AgentDB memory with HNSW search |
| `task` | 6 | Task creation and lifecycle |
| `session` | 7 | Session state management |
| `hooks` | 17 | Self-learning hooks + 12 workers |
| `hive-mind` | 6 | Byzantine fault-tolerant consensus |

### Quick CLI Examples

```bash
npx @claude-flow/cli@latest init --wizard
npx @claude-flow/cli@latest agent spawn -t coder --name my-coder
npx @claude-flow/cli@latest swarm init --v3-mode
npx @claude-flow/cli@latest memory search --query "authentication patterns"
npx @claude-flow/cli@latest doctor --fix
```

## Available Agents (60+ Types)

### Core Development
`coder`, `reviewer`, `tester`, `planner`, `researcher`

### Specialized
`security-architect`, `security-auditor`, `memory-specialist`, `performance-engineer`

### Swarm Coordination
`hierarchical-coordinator`, `mesh-coordinator`, `adaptive-coordinator`

### GitHub & Repository
`pr-manager`, `code-review-swarm`, `issue-tracker`, `release-manager`

### SPARC Methodology
`sparc-coord`, `sparc-coder`, `sp   ecification`, `pseudocode`, `architecture`

## Memory Commands Reference

```bash
# Store (REQUIRED: --key, --value; OPTIONAL: --namespace, --ttl, --tags)
npx @claude-flow/cli@latest memory store --key "pattern-auth" --value "JWT with refresh" --namespace patterns

# Search (REQUIRED: --query; OPTIONAL: --namespace, --limit, --threshold)
npx @claude-flow/cli@latest memory search --query "authentication patterns"

# List (OPTIONAL: --namespace, --limit)
npx @claude-flow/cli@latest memory list --namespace patterns --limit 10

# Retrieve (REQUIRED: --key; OPTIONAL: --namespace)
npx @claude-flow/cli@latest memory retrieve --key "pattern-auth" --namespace patterns
```

## Quick Setup

```bash
claude mcp add claude-flow -- npx -y @claude-flow/cli@latest
npx @claude-flow/cli@latest daemon start
npx @claude-flow/cli@latest doctor --fix
```

## Claude Code vs CLI Tools

- Claude Code's Task tool handles ALL execution: agents, file ops, code generation, git
- CLI tools handle coordination via Bash: swarm init, memory, hooks, routing
- NEVER use CLI tools as a substitute for Task tool agents

## Support

- Documentation: https://github.com/ruvnet/claude-flow
- Issues: https://github.com/ruvnet/claude-flow/issues

## Persistent Elysian Swarm (Always Active)

This repo runs a persistent 7-agent quant swarm. Every session starts with this swarm loaded. Coordinate all work through these roles:

| Agent | Subagent Type | Role | Memory Namespace |
|-------|--------------|------|-----------------|
| **system-quant-architect** | `system-architect-quant` | Lead — pipeline design, FSM, signal contracts, cross-cutting integrity | `elysian-arch` |
| **quant-strategist** | `quant-strategist` | Strategy layer, signal generation, weight construction | `elysian-strat` |
| **quant-developer** | `quant-developer` | Infrastructure, exchange connectivity, async data pipelines | `elysian-dev` |
| **coder** | `coder` | Implementation, refactoring, clean production code | `elysian-code` |
| **quant-analyst** | `quant-analyst` | Code quality, performance analysis, portfolio analytics | `elysian-analysis` |
| **tester** | `tester` | TDD, unit/integration tests, verification | `elysian-test` |
| **architect** | `architect` | High-level system design, ADRs, cross-domain decisions | `elysian-arch` |

### Swarm Coordination

- **Lead**: `system-quant-architect` — architectural decisions and cross-agent coordination
- **Topology**: hierarchical-mesh
- **Shared namespace**: `elysian-swarm`
- Manifest: `.claude/swarm-manifest.json`
- When spawning agents for complex tasks, use ALL relevant agents in ONE message

### Persistent Memory Namespaces

```bash
npx @claude-flow/cli@latest memory search --query "<query>" --namespace elysian-swarm
# Per-agent: elysian-arch | elysian-strat | elysian-dev | elysian-code | elysian-analysis | elysian-test
```
