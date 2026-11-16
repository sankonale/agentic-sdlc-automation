# agentic-sdlc: requirement in, reviewable engineering outcome out

A working prototype of an agentic software engineering system. It turns a requirement into a normalized
spec, a task DAG, design artifacts (architecture + ADRs, OpenAPI contract, SQL schema), production-style code or a
patch, unit/integration/regression tests, documentation, a risk register, a validation report and a final
engineering summary. Agents run independently across many steps, and humans approve at three gates.

| Deliverable | Where |
|---|---|
| Working prototype | `agentic_sdlc/` (`python -m agentic_sdlc ...`) |
| Architecture overview | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| Example scenarios (greenfield / brownfield / ambiguous) | [`docs/SCENARIOS.md`](docs/SCENARIOS.md) + committed outputs in `sample_runs/` |
| Setup instructions | this file |
| Testing approach and limitations | [`docs/TESTING.md`](docs/TESTING.md) |
| Mandatory URL shortener use case | `sample_runs/01_greenfield_url_shortener/` |

## Setup

Requires Python 3.10+ (tested on 3.13) and `git` (used only for the patch-apply check).

```bash
cd agentic-sdlc
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q                       # 39 tests of the agentic system
```

PowerShell (Windows):

```powershell
cd agentic-sdlc
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest tests -q
```

No API key is needed. The default `offline` provider is deterministic. `--llm anthropic` (with
`pip install anthropic` and `ANTHROPIC_API_KEY` set) adds LLM augmentation behind the same guardrails.

## Run it

```bash
# every mandated scenario + recovery demos, with a results table at the end
python -m agentic_sdlc demo --out runs

# the mandatory use case, interactively (you approve at Gate 1, 2 and 3)
python -m agentic_sdlc run -r "Build a scalable URL shortener service with APIs, persistence, and analytics."

# same, unattended under the explicit approval policy
python -m agentic_sdlc run -f examples/requirements/greenfield_url_shortener.txt --auto-approve --name greenfield

# brownfield: change an existing repo (never modified; you get a patch)
python -m agentic_sdlc run -f examples/requirements/brownfield_expiry_bugfix.txt \
    --repo examples/legacy_shortener --auto-approve --name brownfield

# ambiguous: blocks at Gate 1 -> then answer the questions, or accept the defaults
python -m agentic_sdlc run -f examples/requirements/ambiguous.txt --auto-approve --name amb
python -m agentic_sdlc run -f examples/requirements/ambiguous.txt --auto-approve --answers examples/answers_ambiguous.yaml --name amb
python -m agentic_sdlc run -f examples/requirements/ambiguous.txt --auto-approve --assume-defaults --name amb

# show error handling and recovery
python -m agentic_sdlc run -f examples/requirements/greenfield_url_shortener.txt --auto-approve \
    --inject-fault transient:impl_analytics --inject-fault defect:impl_core --name recovery
```

PowerShell: use the same commands on one line, or replace the trailing `\` with a backtick `` ` ``.

### What a run produces (`runs/<name>/`)

| File | Content |
|---|---|
| `ENGINEERING_SUMMARY.md` | plan + rationale, impact analysis, artifacts, validation, risks/trade-offs, gate decisions, assumptions, limitations |
| `deliverable/` | promoted artifacts (only if Gate 3 approved) |
| `trace.jsonl` | every event: task start/finish, waves, retries, findings, rework, gates |
| `spec.json`, `plan.json`, `state.json`, `blackboard.json`, `summary.json` | machine-readable state for review or resume |
| `CLARIFICATION_REQUEST.md` | only when Gate 1 blocks |

### Try the generated service

```bash
cd runs/greenfield/deliverable/service
pip install -r requirements.txt && python -m pytest -q
SHORTENER_API_KEYS=dev-key-123 uvicorn app.main:app --port 8000
curl -s -X POST localhost:8000/api/v1/links -H 'X-API-Key: dev-key-123' \
     -H 'Content-Type: application/json' -d '{"url":"https://example.com"}'
```

```powershell
cd runs\greenfield\deliverable\service
$env:SHORTENER_API_KEYS = "dev-key-123"; uvicorn app.main:app --port 8000
Invoke-RestMethod -Method Post http://localhost:8000/api/v1/links -Headers @{"X-API-Key"="dev-key-123"} `
  -ContentType "application/json" -Body '{"url":"https://example.com"}'
```

## Repository layout

```
agentic_sdlc/
  pipeline.py        stages, gates, repair loop, promotion
  orchestrator.py    DAG engine: waves, retries, skip propagation, rework, checkpoints
  approval.py        Gate 1/2/3: interactive human or explicit policy
  blackboard.py      shared context with provenance
  guardrails.py      sandboxed workspace, content/secret scan, budgets, repo fingerprint
  llm.py             offline (default) / Anthropic provider
  agents/            requirement_analyst, planner, codebase_analyst, design (architect, api, data),
                     generators (code, tests, change), docs_risk, validator, reporter
  packs/             url_shortener (templates parameterized by spec), legacy_expiry (brownfield recipe)
examples/            requirement texts, answers file, legacy_shortener (brownfield target)
sample_runs/         committed outputs of `demo`
tests/               tests of the agentic system
docs/                ARCHITECTURE, SCENARIOS, TESTING
```
