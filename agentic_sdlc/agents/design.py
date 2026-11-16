"""Design agents: Architect, ApiDesigner, DataModeler.

Their outputs are machine-readable (dicts on the blackboard) *and* human
readable (files in staging). Downstream agents consume the machine-readable
form - e.g. the Validator checks the implemented API against `api_contract`,
and the generated repository loads the schema the DataModeler wrote.
"""
from __future__ import annotations

import json

import yaml

from ..models import Artifact, Task
from .base import Agent, AgentContext


def _require_pack(ctx: AgentContext, spec: dict, who: str) -> None:
    if spec["domain"] != "url_shortener":
        raise RuntimeError(f"{who}: no design pack for domain '{spec['domain']}' and LLM design is not enabled "
                           "in offline mode; refusing to emit an unrelated design")


class Architect(Agent):
    name = "architect"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        if task.inputs.get("mode") == "change":
            return self._change_design(task, ctx)
        spec = ctx.blackboard.require("spec")
        _require_pack(ctx, spec, self.name)
        p = spec["parameters"]
        components = [
            {"name": "API tier (FastAPI, stateless)", "responsibility": "HTTP parsing, auth, rate limiting, error mapping",
             "module": "app/api.py"},
            {"name": "ShortenerService", "responsibility": "All business rules: validation, code allocation, expiry",
             "module": "app/service.py"},
            {"name": "CodeGenerator", "responsibility": "Random base62 codes; alias validation", "module": "app/codegen.py"},
            {"name": "LinkRepository / ClickRepository", "responsibility": "Persistence ports; SQLite adapter",
             "module": "app/repository.py"},
            {"name": "TTLCache", "responsibility": "Read-through LRU cache on redirect hot path", "module": "app/cache.py"},
            {"name": "AnalyticsRecorder", "responsibility": "Privacy-preserving click capture, write-behind batching",
             "module": "app/analytics.py"},
            {"name": "TokenBucketLimiter", "responsibility": "Abuse protection on link creation",
             "module": "app/rate_limit.py"},
        ]
        decisions = [
            {"id": "ADR-001", "title": "Random base62 codes instead of a global counter",
             "decision": f"{p['code_length']}-char random codes (62^{p['code_length']} space); uniqueness enforced by "
                         "the store's primary key with bounded retry.",
             "why": "No coordination between instances; codes are not enumerable.",
             "trade_off": "Needs collision retry; slightly longer codes than counter+base62."},
            {"id": "ADR-002", "title": f"HTTP {p['redirect_status']} redirects",
             "decision": f"Redirect with {p['redirect_status']}.",
             "why": "302 keeps every click visible to analytics and allows deactivation to take effect."
             if p["redirect_status"] == 302 else "301 lets browsers/CDNs cache redirects (lower load).",
             "trade_off": "Higher redirect load than 301 (no browser caching)."
             if p["redirect_status"] == 302 else "Repeat clicks are invisible to analytics; deactivation is unreliable."},
            {"id": "ADR-003", "title": "Ports-and-adapters persistence",
             "decision": "Service depends on repository protocols; SQLite adapter shipped, PostgreSQL in production.",
             "why": "Swap storage without touching business logic; fast hermetic tests.",
             "trade_off": "SQLite is single-writer: production must use the PostgreSQL adapter."},
            {"id": "ADR-004", "title": "Write-behind analytics",
             "decision": "Redirect path appends to a buffer; batches flushed to the click store.",
             "why": "Redirect latency independent of analytics writes.",
             "trade_off": "Events buffered in memory can be lost on crash (bounded by batch size); "
                          "production uses Kafka for durability."},
            {"id": "ADR-005", "title": "Read-through cache with short TTL",
             "decision": "Per-instance LRU (TTL 300 s) in front of the store; Redis tier at scale.",
             "why": "Redirects are ~100:1 read-heavy; cache hits avoid the database.",
             "trade_off": "A deactivated link may still redirect on other instances for up to the cache TTL."},
        ]
        scaling = [
            f"Target {p['target_rps']} redirects/s per instance; scale API pods horizontally (stateless).",
            "PostgreSQL primary for writes + read replicas; links table is small (~200 B/row).",
            "Redis cluster as shared cache; CDN in front of redirect domain if 301 is ever adopted.",
            "Clicks: Kafka topic partitioned by code -> stream aggregation -> ClickHouse/BigQuery.",
            "Shard links by hash(code) if > 1B links (code is the only lookup key).",
        ]
        arch = {"components": components, "decisions": decisions, "scaling": scaling,
                "request_flows": {
                    "create": "client -> API(auth, rate limit) -> Service.validate_url -> CodeGenerator -> "
                              "Repository.create (PK uniqueness) -> 201",
                    "redirect": "client -> API -> Service.resolve -> Cache (hit) | Repository.get (miss -> fill) -> "
                                "expiry/active check -> Analytics.record (buffer) -> 30x",
                    "stats": "client -> API -> Service.stats -> Analytics.flush -> ClickRepository.stats -> 200"}}
        ctx.blackboard.put("architecture", arch, producer=task.id)

        md = ["# Architecture: URL Shortener", "",
              "```mermaid", "flowchart LR",
              "  C[Client] -->|POST /api/v1/links| API[API tier\\nFastAPI, stateless]",
              "  C -->|GET /code| API",
              "  API --> RL[Rate limiter] & S[ShortenerService]",
              "  S --> CG[CodeGenerator]", "  S --> CA[(TTL cache / Redis)]",
              "  S --> R[(Links store\\nSQLite dev / PostgreSQL prod)]",
              "  S --> AN[AnalyticsRecorder\\nwrite-behind buffer] --> CK[(Click events)]", "```", "",
              "## Components", "", "| Component | Responsibility | Module |", "|---|---|---|",
              *[f"| {c['name']} | {c['responsibility']} | `{c['module']}` |" for c in components],
              "", "## Request flows", "", *[f"- **{k}**: {v}" for k, v in arch["request_flows"].items()],
              "", "## Architecture decisions", ""]
        for d in decisions:
            md += [f"### {d['id']}: {d['title']}", f"- Decision: {d['decision']}", f"- Why: {d['why']}",
                   f"- Trade-off: {d['trade_off']}", ""]
        md += ["## Scaling path", "", *[f"- {s}" for s in scaling], ""]
        return [self.write(ctx, task, "service/docs/ARCHITECTURE.md", "\n".join(md), "doc", "architecture + ADRs")]

    def _change_design(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        impact = ctx.blackboard.require("impact")
        spec = ctx.blackboard.require("spec")
        plan = []
        if "expiry" in impact["concepts"]:
            plan += [
                {"module": "shortener.store", "change": "store records (url, expires_at); add delete(); keep "
                 "get(code)->url|None semantics; add get_record(code)"},
                {"module": "shortener.core", "change": "shorten(url, ttl_seconds=None); injectable clock; "
                 "resolve() returns None for expired; new status(code) -> active|expired|missing"},
                {"module": "shortener.web", "change": "accept optional ttl_seconds in POST body; 410 Gone for expired"},
            ]
        if "collision" in impact["concepts"]:
            plan.append({"module": "shortener.core", "change": "on hash collision with a *different* URL, re-hash "
                         "with a counter salt until a free or matching slot is found (bounded); never overwrite"})
        design = {"plan": plan, "compatibility": [
            "Shortener() and Shortener(store) constructors unchanged",
            "shorten(url) without ttl keeps idempotent behaviour for the same URL",
            "resolve(code) still returns str | None",
            "POST /shorten without ttl_seconds behaves exactly as before"],
            "assumptions": spec["assumptions"]}
        ctx.blackboard.put("change_design", design, producer=task.id)
        md = ["# Change design", "", "| Module | Change |", "|---|---|",
              *[f"| `{p['module']}` | {p['change']} |" for p in plan],
              "", "## Backward-compatibility contract", *[f"- {c}" for c in design["compatibility"]], ""]
        return [self.write(ctx, task, "analysis/CHANGE_DESIGN.md", "\n".join(md), "doc")]


class ApiDesigner(Agent):
    name = "api_designer"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        spec = ctx.blackboard.require("spec")
        _require_pack(ctx, spec, self.name)
        p = spec["parameters"]
        err = {"$ref": "#/components/schemas/ErrorResponse"}

        def resp(desc: str, schema: dict | None = None) -> dict:
            r: dict = {"description": desc}
            if schema:
                r["content"] = {"application/json": {"schema": schema}}
            return r

        errors = {str(s): resp(d, err) for s, d in
                  [(401, "Missing/invalid API key"), (404, "Unknown code"), (409, "Alias already in use"),
                   (410, "Link expired or deactivated"), (422, "Invalid input"), (429, "Rate limited")]}
        code_param = [{"name": "code", "in": "path", "required": True, "schema": {"type": "string"}}]
        security = [{"ApiKey": []}] if p.get("require_api_key") else []
        contract = {
            "openapi": "3.1.0",
            "info": {"title": "URL Shortener", "version": "1.0.0"},
            "paths": {
                "/api/v1/links": {"post": {
                    "operationId": "createLink", "security": security,
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/CreateLinkRequest"}}}},
                    "responses": {"201": resp("Created", {"$ref": "#/components/schemas/LinkResponse"}),
                                  **{k: errors[k] for k in ("401", "409", "422", "429")}}}},
                "/api/v1/links/{code}": {
                    "get": {"operationId": "getLink", "parameters": code_param,
                            "responses": {"200": resp("Link", {"$ref": "#/components/schemas/LinkResponse"}),
                                          "404": errors["404"]}},
                    "delete": {"operationId": "deactivateLink", "parameters": code_param, "security": security,
                               "responses": {"204": resp("Deactivated"), "401": errors["401"],
                                             "404": errors["404"]}}},
                "/api/v1/links/{code}/stats": {"get": {
                    "operationId": "getLinkStats", "parameters": code_param,
                    "responses": {"200": resp("Stats", {"$ref": "#/components/schemas/LinkStats"}),
                                  "404": errors["404"]}}},
                "/{code}": {"get": {
                    "operationId": "redirect", "parameters": code_param,
                    "responses": {str(p["redirect_status"]): {"description": "Redirect",
                                                               "headers": {"Location": {"schema": {"type": "string"}}}},
                                  "404": errors["404"], "410": errors["410"]}}},
                "/healthz": {"get": {"operationId": "healthz", "responses": {"200": resp("OK")}}},
            },
            "components": {
                "securitySchemes": {"ApiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
                "schemas": {
                    "CreateLinkRequest": {"type": "object", "required": ["url"], "properties": {
                        "url": {"type": "string", "format": "uri", "maxLength": 2048},
                        "custom_alias": {"type": "string", "pattern": "^[A-Za-z0-9_-]{3,32}$"},
                        "ttl_days": {"type": "integer", "minimum": 1, "maximum": 3650}}},
                    "LinkResponse": {"type": "object",
                                     "required": ["code", "short_url", "long_url", "created_at", "is_active"],
                                     "properties": {"code": {"type": "string"}, "short_url": {"type": "string"},
                                                    "long_url": {"type": "string"},
                                                    "created_at": {"type": "string", "format": "date-time"},
                                                    "expires_at": {"type": ["string", "null"], "format": "date-time"},
                                                    "is_active": {"type": "boolean"}}},
                    "LinkStats": {"type": "object", "properties": {
                        "code": {"type": "string"}, "total_clicks": {"type": "integer"},
                        "unique_visitors": {"type": "integer"},
                        "last_click_at": {"type": ["string", "null"], "format": "date-time"},
                        "clicks_by_day": {"type": "array", "items": {"type": "object", "properties": {
                            "day": {"type": "string", "format": "date"}, "clicks": {"type": "integer"}}}},
                        "top_referrers": {"type": "array", "items": {"type": "object", "properties": {
                            "referrer": {"type": "string"}, "clicks": {"type": "integer"}}}},
                        "by_user_agent": {"type": "object", "additionalProperties": {"type": "integer"}}}},
                    "ErrorResponse": {"type": "object", "required": ["error"], "properties": {"error": {
                        "type": "object", "required": ["code", "message"],
                        "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}},
                }},
        }
        ctx.blackboard.put("api_contract", contract, producer=task.id)
        return [self.write(ctx, task, "service/openapi.yaml", yaml.safe_dump(contract, sort_keys=False), "contract",
                           "OpenAPI 3.1 contract (source of truth for conformance check)")]


class DataModeler(Agent):
    name = "data_modeler"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        tables = {
            "links": [("code", "TEXT PRIMARY KEY", "short code or custom alias; PK enforces uniqueness atomically"),
                      ("long_url", "TEXT NOT NULL", "destination"),
                      ("created_at", "REAL NOT NULL", "unix epoch seconds"),
                      ("expires_at", "REAL", "NULL = never expires"),
                      ("is_active", "INTEGER NOT NULL DEFAULT 1", "soft delete flag")],
            "click_events": [("id", "INTEGER PRIMARY KEY AUTOINCREMENT", ""),
                             ("code", "TEXT NOT NULL REFERENCES links(code)", ""),
                             ("ts", "REAL NOT NULL", "click time"),
                             ("referrer_host", "TEXT", "host only, never full referrer URL"),
                             ("ua_family", "TEXT NOT NULL", "chrome/firefox/safari/edge/bot/other"),
                             ("visitor_hash", "TEXT NOT NULL", "salted SHA-256 of client IP (no raw IPs)")],
        }
        indexes = ["CREATE INDEX IF NOT EXISTS idx_links_expires_at ON links(expires_at);",
                   "CREATE INDEX IF NOT EXISTS idx_click_events_code_ts ON click_events(code, ts);"]
        sql = ["-- Generated by agentic-sdlc DataModeler. SQLite dialect; PostgreSQL notes below.", ""]
        for t, cols in tables.items():
            sql.append(f"CREATE TABLE IF NOT EXISTS {t} (")
            sql.append(",\n".join(f"    {c} {typ}" for c, typ, _ in cols))
            sql.append(");")
            sql.append("")
        sql += indexes
        sql += ["", "-- PostgreSQL production notes:",
                "--   links.code VARCHAR(32) PRIMARY KEY; created_at/expires_at TIMESTAMPTZ",
                "--   click_events: PARTITION BY RANGE (ts) monthly; drop partitions older than retention (90 days)",
                "--   or stream clicks to Kafka -> ClickHouse and keep only rollups in PostgreSQL", ""]
        model = {"tables": {t: [{"column": c, "type": typ, "note": n} for c, typ, n in cols]
                            for t, cols in tables.items()}, "indexes": indexes}
        ctx.blackboard.put("data_schema", model, producer=task.id)
        return [self.write(ctx, task, "service/app/schema.sql", "\n".join(sql), "schema",
                           "DDL loaded by the repository at startup"),
                self.write(ctx, task, "service/docs/data_model.json", json.dumps(model, indent=2), "schema")]
