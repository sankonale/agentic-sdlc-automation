"""RequirementAnalyst: raw text -> normalized engineering problem (Spec).

Steps
  1. classify the scenario (greenfield / brownfield / tests-docs)
  2. detect the domain (matches a skill pack or "generic")
  3. extract goals and map them to a requirement catalog (FR/NFR ids)
  4. detect ambiguity: vague qualifiers ("fast", "scalable", "stuff"),
     unquantified NFRs, and decisions the text leaves open (redirect type,
     expiry, auth) - each becomes a question with a default assumption
  5. score ambiguity so the G1 gate can decide whether a human must answer
"""
from __future__ import annotations

import re
from typing import Any

from ..models import Ambiguity, ScenarioType, Spec
from ..packs import url_shortener as us
from .base import Agent, AgentContext

_BROWNFIELD = re.compile(r"\b(existing|legacy|current(ly)?|refactor\w*|bug|fix\w*|regression|enhanc\w*|"
                         r"migrat\w*|our (service|code|repo)|extend)\b", re.I)
_TESTS_DOCS = re.compile(r"\b(test coverage|add tests|write tests|documentation|document the|docstrings?)\b", re.I)
_VAGUE_HIGH = re.compile(r"\b(stuff|things|something|whatever|etc\.?|and so on|some kind of)\b", re.I)
_NUMBER = re.compile(r"\d")

_WEIGHT = {"high": 0.3, "medium": 0.15, "low": 0.05}


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", text, re.I) for w in words)


class RequirementAnalyst(Agent):
    name = "requirement_analyst"

    # -------------------------------------------------------------- public
    def analyze(self, raw: str, ctx: AgentContext, target_repo: str | None = None) -> Spec:
        text = " ".join(raw.split())
        scenario = self._scenario(text, target_repo)
        domain = us.DOMAIN if (any(k in text.lower() for k in us.KEYWORDS)
                               or (_has(text, "link") and _has(text, "short", "redirect", "click"))) else "generic"
        goals = self._goals(text)
        ambiguities = self._ambiguities(text, scenario, domain, target_repo)
        params = dict(us.DEFAULT_PARAMETERS) if domain == us.DOMAIN else {}
        self._extract_explicit_params(text, params)

        functional, nfr, out_of_scope = self._catalog(text, scenario, domain, params)
        entities = self._entities(text, domain)

        # optional LLM augmentation (untrusted -> schema-checked, merged, never replaces heuristics)
        extra = ctx.llm.complete_json(
            "You are a senior requirements engineer.",
            f"List up to 5 additional ambiguities in this requirement as JSON "
            f'[{{"term","category","question","default_assumption","severity"}}]: {raw}')
        if isinstance(extra, list):
            for e in extra[:5]:
                if isinstance(e, dict) and {"term", "question", "default_assumption"} <= set(e):
                    ambiguities.append(Ambiguity(str(e["term"]), str(e.get("category", "scope")),
                                                 str(e["question"]), str(e["default_assumption"]),
                                                 severity=str(e.get("severity", "medium"))))

        score = round(min(1.0, sum(_WEIGHT.get(a.severity, 0.1) for a in ambiguities)), 2)
        spec = Spec(raw=raw.strip(), title=self._title(text, scenario, domain), scenario=scenario, domain=domain,
                    goals=goals, functional=functional, non_functional=nfr, entities=entities,
                    out_of_scope=out_of_scope, ambiguities=ambiguities, assumptions=[], parameters=params,
                    ambiguity_score=score, target_repo=target_repo)
        ctx.tracer.emit("info", f"scenario={scenario.value} domain={domain} ambiguities={len(ambiguities)} "
                                f"score={score}")
        return spec

    def apply_answers(self, spec: Spec, answers: dict[str, Any]) -> Spec:
        """Resolve each ambiguity with the human's answer or its documented default."""
        assumptions = []
        for a in spec.ambiguities:
            key = a.parameter or a.term
            if key in answers:
                value, source = self._coerce(a, answers[key]), "answered"
            else:
                value, source = a.default_value, "default"
            if a.parameter:
                spec.parameters[a.parameter] = value
            label = a.default_assumption if source == "default" else f"{a.term}: {answers[key]}"
            assumptions.append(f"[{source}] {label}")
        spec.assumptions = assumptions
        if spec.domain == us.DOMAIN:   # re-render NFRs now that numbers are final
            spec.non_functional = [f"{k}: {v.format(**spec.parameters)}" for k, v in us.NON_FUNCTIONAL.items()]
        return spec

    # ------------------------------------------------------------ internals
    @staticmethod
    def _scenario(text: str, repo: str | None) -> ScenarioType:
        if _TESTS_DOCS.search(text) and not re.search(r"\b(build|add|implement)\b(?!.*tests)", text, re.I):
            return ScenarioType.TESTS_DOCS
        if repo or _BROWNFIELD.search(text):
            return ScenarioType.BROWNFIELD
        return ScenarioType.GREENFIELD

    @staticmethod
    def _goals(text: str) -> list[str]:
        parts = re.split(r"(?<=[.;])\s+|,\s*(?:and\s+)?|\s+and\s+(?=(?:also\s+)?(?:fix|add|make|support|"
                         r"track|provide|expose|handle|build)\b)", text)
        return [p.strip(" .") for p in parts if len(p.strip(" .")) > 3]

    @staticmethod
    def _title(text: str, scenario: ScenarioType, domain: str) -> str:
        if domain == us.DOMAIN and scenario == ScenarioType.GREENFIELD:
            return "Scalable URL shortener service"
        first = re.split(r"[.;]", text)[0]
        return first[:80]

    @staticmethod
    def _entities(text: str, domain: str) -> list[str]:
        if domain == us.DOMAIN:
            ents = ["Link(code, long_url, created_at, expires_at, is_active)",
                    "ClickEvent(code, ts, referrer_host, ua_family, visitor_hash)"]
            return ents
        return sorted({w.lower() for w in re.findall(r"\b[A-Z][a-z]{3,}\b", text)})

    @staticmethod
    def _extract_explicit_params(text: str, params: dict[str, Any]) -> None:
        if m := re.search(r"\b(30[12])\b", text):
            params["redirect_status"] = int(m.group(1))
        if m := re.search(r"(\d+)\s*(?:k|000)\s*(?:rps|req(?:uests)?/s)", text, re.I):
            params["target_rps"] = int(m.group(1)) * 1000
        if m := re.search(r"p99\D{0,10}(\d+)\s*ms", text, re.I):
            params["redirect_p99_ms"] = int(m.group(1))

    def _catalog(self, text: str, scenario: ScenarioType, domain: str,
                 params: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        if domain == us.DOMAIN and scenario == ScenarioType.GREENFIELD:
            fr = [f"{k}: {v}" for k, v in us.FUNCTIONAL.items()]
            nfr = [f"{k}: {v.format(**params)}" for k, v in us.NON_FUNCTIONAL.items()]
            oos = ["User accounts / UI dashboard", "Multi-region active-active replication (design only)",
                   "Malware/phishing URL reputation feeds (hook provided, no vendor integration)"]
            return fr, nfr, oos
        if scenario == ScenarioType.BROWNFIELD:
            fr = [f"CR-{i}: {g}" for i, g in enumerate(self._goals(text), 1)]
            nfr = ["Backward compatibility: existing public functions and HTTP routes keep their behaviour",
                   "All pre-existing tests must still pass (regression safety)",
                   "Change is delivered as a reviewable patch; the source repository is never modified in place"]
            return fr, nfr, ["Persistence migration to a database", "Changes outside impacted modules"]
        return [f"FR-{i}: {g}" for i, g in enumerate(self._goals(text), 1)], [], []

    def _ambiguities(self, text: str, scenario: ScenarioType, domain: str, repo: str | None) -> list[Ambiguity]:
        out: list[Ambiguity] = []
        for m in re.finditer(r"((?:[\w'.-]+\s+){0,3})" + _VAGUE_HIGH.pattern, text, re.I):
            phrase = m.group(0).strip(" .,")
            out.append(Ambiguity(phrase, "scope", f"'{phrase}' is open-ended. What exactly is in scope?",
                                 "Interpret using the domain's standard scope (see functional requirements)",
                                 parameter="scope", default_value="standard domain scope", severity="high"))
        if scenario == ScenarioType.BROWNFIELD and not repo:
            out.append(Ambiguity("target codebase", "scope", "Which repository/service should be changed?",
                                 "No repository given: treat as greenfield design for the described change",
                                 severity="high"))
        if domain != us.DOMAIN:
            if _has(text, "fast", "quick", "performant") and not _NUMBER.search(text):
                out.append(Ambiguity("fast", "performance", "What latency target (p95/p99) defines 'fast'?",
                                     "p99 < 200 ms for user-facing requests", severity="medium"))
            return out

        # ---- URL-shortener specific decision points ------------------------------------
        if scenario == ScenarioType.GREENFIELD:
            if _has(text, "scalab", "lots of traffic", "high traffic", "scale") and not re.search(r"\d+\s*k?\s*(rps|req)", text, re.I):
                out.append(Ambiguity("scalable", "scale", "What peak traffic must one instance / the fleet handle?",
                                     "Design for 1,000 redirects/s per instance; scale horizontally behind a "
                                     "load balancer", "target_rps", 1000, "medium"))
            if _has(text, "fast", "quick", "low latency") and "ms" not in text.lower():
                out.append(Ambiguity("fast", "performance", "What redirect latency target?",
                                     "Redirect p99 < 50 ms on cache hit", "redirect_p99_ms", 50, "medium"))
            if _has(text, "analytic", "track", "stats", "metrics"):
                vague = bool(re.search(r"(track|analytics?)\s+(stuff|things|something|data)", text, re.I))
                out.append(Ambiguity("analytics", "data", "Which analytics dimensions and what retention?",
                                     "Clicks, unique visitors (salted IP hash), referrer host, UA family, daily "
                                     "series; raw events retained 90 days", "analytics",
                                     us.DEFAULT_PARAMETERS["analytics"], "high" if vague else "medium"))
            if not re.search(r"\b30[12]\b|permanent|temporary", text, re.I):
                out.append(Ambiguity("redirect type", "interface",
                                     "301 (cacheable, cheaper) or 302 (every click observable)?",
                                     "302 so analytics sees every click", "redirect_status", 302, "medium"))
            if not _has(text, "expir", "ttl"):
                out.append(Ambiguity("link expiry", "data", "Should links expire by default?",
                                     "No default expiry; per-link optional ttl_days", "default_ttl_days", None, "low"))
            if not _has(text, "auth", "api key", "token", "public"):
                out.append(Ambiguity("authentication", "security", "Who may create/delete links?",
                                     "API key required for write endpoints; redirects are public",
                                     "require_api_key", True, "low" if not _has(text, "secure") else "medium"))
            if not _has(text, "postgres", "mysql", "dynamo", "redis", "sqlite", "cassandra"):
                out.append(Ambiguity("persistence technology", "data", "Which datastore is mandated?",
                                     "SQLite for dev/test, PostgreSQL in production behind the same repository "
                                     "interface", "storage", us.DEFAULT_PARAMETERS["storage"], "low"))
            out.append(Ambiguity("short code length", "interface", "Preferred short-code length?",
                                 "7 base62 chars (3.5e12 codes; collision retry)", "code_length", 7, "low"))
            out.append(Ambiguity("creation rate limit", "security", "Rate limit for link creation?",
                                 "60 creations/min per API key or client IP", "rate_limit_per_min", 60, "low"))
        else:
            if _has(text, "expir", "ttl") and not re.search(r"\d+\s*(day|hour|second|minute)", text, re.I):
                out.append(Ambiguity("expiry semantics", "data",
                                     "Default TTL? What happens when an active URL is shortened again?",
                                     "No default TTL (opt-in ttl_seconds); re-shortening an active URL returns "
                                     "the same code and keeps its expiry; an expired code can be reissued",
                                     "default_ttl_seconds", None, "medium"))
            if _has(text, "expir"):
                out.append(Ambiguity("expired response", "interface", "HTTP status for an expired code?",
                                     "410 Gone (distinguishable from 404 for unknown codes)",
                                     "expired_status", 410, "low"))
        return out

    @staticmethod
    def _coerce(a: Ambiguity, value: Any) -> Any:
        if isinstance(a.default_value, bool):
            return str(value).lower() in {"true", "yes", "y", "1"}
        if isinstance(a.default_value, int):
            m = re.search(r"\d+", str(value))
            return int(m.group()) if m else a.default_value
        if a.default_value is None and re.fullmatch(r"\s*\d+\s*", str(value)):
            return int(value)
        return value

    def run(self, task, ctx):  # not scheduled as a DAG task; invoked directly by the pipeline
        raise NotImplementedError
