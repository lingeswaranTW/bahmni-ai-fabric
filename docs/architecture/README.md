# bahmni-ai architecture documentation

Architecture of **bahmni-ai** (this repository) and how **standard-config** (`/Users/lingeswaransubramaniyam/Data/bahmni/standard-config`, read-only) drives it.

**Source-of-truth rule.** The code was read first (`src/bahmni_ai`, `pyproject.toml`, `Dockerfile`, `.github/workflows`, `tests`). The README and `docs/*.md` were then cross-checked against it. Where they disagree, the code wins and the mismatch is listed in [docs-vs-code-discrepancies.md](docs-vs-code-discrepancies.md). Anything not verifiable from the code is marked **Unverified**.

**Snapshot.** Branch `main`, last commit `69702c0`. The working tree has many uncommitted changes (new `providers/`, `prompts/`, `urlsafety.py`, moved `db/store.py`). This documentation describes the **working tree**, not the last commit. `uv run pytest` on this tree: 206 passed, 7 skipped.

## In one paragraph

bahmni-ai is a Python (FastAPI) service that gives Bahmni users a chat assistant. The browser never calls it directly: `bahmni-core` (inside OpenMRS) authenticates the user and proxies chat requests to it, passing the OpenMRS session id in the `X-OpenMRS-Session-Id` header. bahmni-ai re-validates that session against OpenMRS, runs an LLM agent (Pydantic AI) that can call tools on external **MCP servers**, and streams the answer back as Server-Sent Events. Which MCP servers exist, and which of their tools may be used by whom, is declared in `ai/mcp-servers.json` in **standard-config**, not in code. Every tool call is checked against OpenMRS privileges, writes need the user's confirmation, and every decision is audit-logged.

## End-to-end flow (one diagram)

Startup/config, front door, authentication, agent loop, tool policy, confirmation, storage and streaming back. Also available as [SVG](diagrams/00-end-to-end.svg) / [PNG](diagrams/00-end-to-end.png).

```mermaid
%%{init: {"flowchart": {"curve": "linear", "nodeSpacing": 45, "rankSpacing": 45}}}%%
flowchart TB
    subgraph BOOT["STARTUP - fail closed, read once"]
        direction LR
        cfg["standard-config<br/>ai/mcp-servers.json"] --> ld["load_config<br/>validate, expand MCP_* urls"]
        envv["container env<br/>Settings, MCP_* secrets"] --> ld
        ld --> cred["validate_credentials"] --> reg["McpRegistry, stores,<br/>PromptResolver, build_agent"]
    end

    subgraph FRONT["BAHMNI FRONT DOOR"]
        user(["1 Clinic user<br/>Bahmni UI"]) -->|"HTTPS + JSESSIONID"| proxy["2 bahmni-proxy"]
        proxy --> core["3 openmrs / bahmni-core AiServiceController<br/>authenticates, checks 'Use AI Chatbot',<br/>forwards session as X-OpenMRS-Session-Id"]
    end

    subgraph AI["BAHMNI-AI  FastAPI :8090 /bahmni-ai"]
        api["4 POST /chat or /chat/confirm"]
        auth{"5 SessionValidator<br/>session valid and<br/>gate privilege?"}
        reject["401 session_expired<br/>403 forbidden<br/>502 openmrs_unavailable"]
        load["6 ChatService: load + trim history"]
        open["7 open MCP servers<br/>per run, per user"]
        run["8 run agent<br/>instructions = PromptRenderer"]
        tool{"9 model calls a tool?"}
        vis{"10 tool visible?<br/>deny, overrides, allow"}
        val["11 validate arguments<br/>and constraints"]
        priv{"12 gate + tool<br/>privilege held?"}
        conf{"13 confirmation<br/>needed?"}
        fresh["14 fresh privilege check<br/>writes only"]
        exec["15 execute once<br/>timeout, never retried"]
        audit["16 audit log tool_audit<br/>result back to model"]
        denied["denied or hidden<br/>error result to model"]
        appr["ApprovalRequired:<br/>store PendingConfirmation, TTL 300 s<br/>SSE confirmation_required"]
        fin["17 final text<br/>save conversation, tool results redacted"]
        done["18 SSE done"]

        api --> auth
        auth -->|no| reject
        auth -->|yes| load --> open --> run --> tool
        tool -->|yes| vis
        vis -->|no| denied
        vis -->|yes| val --> priv
        priv -->|no| denied
        priv -->|yes| conf
        conf -->|"yes, not yet approved"| appr
        conf -->|no| fresh --> exec --> audit
        audit -->|"next model round"| run
        denied -->|"next model round"| run
        tool -->|"no, answer ready"| fin --> done
    end

    core ==>|"HTTP"| api
    appr ==> done
    done ==>|"SSE stream: text, activity,<br/>tool_*, confirmation_required, done"| ret["bahmni-core streams the reply<br/>back through bahmni-proxy"]
    reject ==>|"error JSON"| ret
    ret ==> user2(["Clinic user sees the answer<br/>word by word"])

    omrs1[("OpenMRS /session<br/>user, roles, privileges")]
    omrs2[("OpenMRS /session<br/>re-validate")]
    pg1[("PostgreSQL<br/>ai_conversation")]
    pg2[("PostgreSQL<br/>ai_pending_confirmation")]
    pg3[("PostgreSQL<br/>ai_conversation")]
    mcp1["MCP servers<br/>connect, discover tools"]
    mcp2["MCP servers<br/>tools/call"]
    llm["LLM provider<br/>Anthropic, OpenAI, Gemini,<br/>Ollama, compatible<br/>retry + fallback"]
    otel["OpenLIT / OTLP<br/>optional, scrubbed"]
    hub["OpenLIT prompt hub<br/>optional"]

    auth -.->|"cache miss"| omrs1
    load -.-> pg1
    open -.-> mcp1
    run <-.-> llm
    priv -.-> omrs2
    fresh -.-> omrs2
    exec -.-> mcp2
    appr -.-> pg2
    fin -.-> pg3
    run -.->|"spans, metrics"| otel
    reg -.->|"wires app"| api
    reg -.-> hub

    classDef ext fill:#eef3ff,stroke:#4a6fd1,color:#111;
    classDef store fill:#fff4e0,stroke:#d08a00,color:#111;
    classDef bad fill:#fde8e8,stroke:#c0392b,color:#111;
    classDef cfgc fill:#e9f7ef,stroke:#2e8b57,color:#111;
    class omrs1,omrs2,mcp1,mcp2,llm,otel,hub ext;
    class pg1,pg2,pg3 store;
    class reject,denied bad;
    class cfg,envv cfgc;
```

## Index

| Document | Contents |
|---|---|
| [01-overview.md](01-overview.md) | Inventory, system context (C4 L1), containers (C4 L2), runtime, cross-cutting concerns, observations |
| [02-modules.md](02-modules.md) | Each package with a component diagram (C4 L3), the module dependency graph, tests |
| [03-data-model.md](03-data-model.md) | Postgres tables, ER diagram, in-memory stores, lifecycles (state diagrams) |
| [04-api-and-flows.md](04-api-and-flows.md) | REST/SSE endpoints, events, error codes, sequence diagrams, flowcharts |
| [05-config-mapping.md](05-config-mapping.md) | standard-config areas mapped to the code that consumes them, and how they load |
| [06-deployment-and-cicd.md](06-deployment-and-cicd.md) | Docker, compose, OpenLIT stack, CI/CD pipeline |
| [docs-vs-code-discrepancies.md](docs-vs-code-discrepancies.md) | Where existing docs and code disagree |
| [diagrams/](diagrams/) | Every Mermaid diagram as a standalone `.mmd` file |

## Diagram index

| # | File | Kind | Shown in |
|---|---|---|---|
| 0 | `00-end-to-end.mmd` (+ `.svg`, `.png`) | **single end-to-end flow** | README (below) |
| 1 | `01-system-context.mmd` | C4 L1 | 01-overview |
| 2 | `02-container.mmd` | C4 L2 | 01-overview |
| 3 | `03a`..`03e-components-*.mmd` | C4 L3 (5 diagrams) | 02-modules |
| 4 | `04-module-dependencies.mmd` | package graph | 02-modules |
| 5 | `05-deployment.mmd` | deployment | 06-deployment-and-cicd |
| 6 | `06-er-model.mmd` | ER | 03-data-model |
| 7 | `07a`..`07g-seq-*.mmd` | sequences (7) | 04-api-and-flows |
| 8 | `08a`..`08d-flow-*.mmd` | flowcharts (4) | 04-api-and-flows, 03-data-model |
| 9 | `09a`..`09c-state-*.mmd` | state (3) | 03-data-model |
| 10 | `10-config-to-code.mmd` | config mapping | 05-config-mapping |
| 11 | `11-cicd-pipeline.mmd` | CI/CD | 06-deployment-and-cicd |

All 27 diagrams were rendered with `@mermaid-js/mermaid-cli` (Chrome headless) to validate syntax.

## Scope notes

- **bahmni-core, bahmni-docker, openmrs-distro-bahmni** are outside the two requested repos. They appear only where the contract with bahmni-ai matters. I confirmed `AiServiceController` and `AiServiceClient` exist in `bahmni-core` (uncommitted there too) and the `bahmni-ai` / `bahmni-ai-db` services exist in `bahmni-docker/bahmni-standard/docker-compose.yml`. Their internals are **Unverified** beyond what is quoted.
- **MCP servers** (documents, fhir) are external; their code is not in either repo. Their tool names are known only from `standard-config/ai/mcp-servers.json`.
- The standard-config areas other than `ai/` (apps, forms, reports, locale, ...) are consumed by OpenMRS/Bahmni apps, **not** by bahmni-ai. See [05-config-mapping.md](05-config-mapping.md).
