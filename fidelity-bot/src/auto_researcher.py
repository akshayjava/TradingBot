"""Karpathy-style autonomous research agent for portfolio optimization.

Inspired by Andrej Karpathy's approach to LLM-driven self-directed research:
  - The LLM drives the research agenda, not the programmer
  - Iterative loop: Plan → Research → Synthesize → Evaluate
  - Extended thinking at each phase for deeper reasoning
  - Evidence-grounded signals with explicit confidence scores

The research loop has three distinct Claude calls:
  1. _plan_research()      — reviews the portfolio, selects priority tickers,
                             generates specific research questions
  2. _research_ticker()    — for each priority ticker, answers its questions
                             using training knowledge + portfolio context
  3. _synthesize_report()  — integrates all findings into per-ticker signals
                             and a portfolio-level research report

Note: Claude uses its training knowledge (up to August 2025) for fundamental
and macro analysis.  For real-time quotes or breaking news, wire in a market
data tool and pass results as additional context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic

from src.config import Config
from src.data_store import DataStore

MODEL = "claude-opus-4-6"

# ── Prompt templates ──────────────────────────────────────────────────────────

_PLAN_SYSTEM = """You are a systematic investment research director with expertise in:
- Fundamental analysis (earnings quality, balance-sheet health, competitive moats)
- Macro / sector rotation dynamics
- Technical momentum signals derived from price and allocation trends
- Behavioural biases that distort portfolio construction

Your job is to build a focused, prioritised research agenda for a portfolio.
Think step-by-step: what is most important to understand for each position?
What could go wrong?  What tail-risks are under-appreciated?"""

_PLAN_PROMPT = """Below is the current portfolio snapshot.  Your task is to produce a
**research plan** in JSON.

Rules:
- Select at most {max_tickers} tickers worth deep research (prioritise by position
  size, recent drift, and potential risk/opportunity asymmetry).
- For each ticker, generate 2–4 specific, answerable research questions.
- Questions should address: fundamentals, macro/sector context, risk factors,
  and whether the current allocation is appropriate.
- Add a "macro_questions" list (3–5 questions) covering cross-portfolio themes
  (e.g. interest-rate sensitivity, sector concentration, correlation clusters).

Portfolio snapshot:
{snapshot_json}

Respond with **only** a JSON object matching this schema exactly:
{{
  "priority_tickers": [
    {{
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "current_value": 12500.00,
      "pct_of_portfolio": 18.3,
      "research_priority": "high|medium|low",
      "questions": ["...", "..."]
    }}
  ],
  "macro_questions": ["...", "...", "..."]
}}
"""

_RESEARCH_SYSTEM = """You are a senior equity research analyst.  You produce rigorous,
evidence-based analysis of individual securities and sectors.  You cite specific
numbers, trends, and risks.  You are honest about uncertainty and your knowledge
cut-off (August 2025).  You separate facts from inferences clearly."""

_RESEARCH_TICKER_PROMPT = """Research the following security in the context of this portfolio.

**Ticker:** {ticker} — {name}
**Current portfolio weight:** {pct_of_portfolio:.1f}%  (${current_value:,.2f})
**Unrealised gain/loss:** {unrealised_gain}

**Portfolio context (for correlation / concentration awareness):**
{portfolio_summary}

**Research questions to answer:**
{questions_list}

Provide a thorough, structured answer to each question.  Then give:
- **Signal:** one of [buy_more | hold | trim | sell]
- **Confidence:** 0.0–1.0
- **Key facts:** bullet list of the 3–5 most important findings
- **Risk factors:** bullet list of the top 3 risks to this position
- **Rationale:** 2–3 sentence summary justifying the signal

Format your answer as a JSON object:
{{
  "ticker": "{ticker}",
  "question_answers": [{{"question": "...", "answer": "..."}}],
  "signal": "hold",
  "confidence": 0.75,
  "key_facts": ["...", "..."],
  "risk_factors": ["...", "..."],
  "rationale": "..."
}}
"""

_MACRO_RESEARCH_PROMPT = """Answer the following macro / cross-portfolio research questions
given this portfolio snapshot.

**Portfolio snapshot:**
{snapshot_json}

**Macro research questions:**
{questions_list}

Provide concise, evidence-based answers.  Then summarise:
- **Portfolio themes:** 3–5 dominant investment themes you observe
- **Macro tailwinds:** top 2–3 macro factors working in this portfolio's favour
- **Macro headwinds:** top 2–3 macro risks to overall portfolio value

Format as JSON:
{{
  "question_answers": [{{"question": "...", "answer": "..."}}],
  "portfolio_themes": ["...", "..."],
  "macro_tailwinds": ["...", "..."],
  "macro_headwinds": ["...", "..."]
}}
"""

_SYNTHESIS_SYSTEM = """You are a portfolio strategist synthesising a multi-analyst research
package into clear, actionable recommendations.  You balance individual-position
signals with portfolio-level constraints (concentration, correlation, tax
efficiency).  You produce specific, prioritised action items."""

_SYNTHESIS_PROMPT = """Synthesise the following research findings into a final portfolio
research report.

**Ticker research results:**
{ticker_research_json}

**Macro research results:**
{macro_research_json}

**Current portfolio snapshot:**
{snapshot_json}

Produce a synthesis that:
1. Ranks the top 3 opportunities and top 3 risks across the whole portfolio
2. For each researched ticker, confirms or adjusts its signal given cross-portfolio context
3. Suggests up to 5 specific rebalancing actions ordered by priority

Format as JSON:
{{
  "top_opportunities": ["...", "...", "..."],
  "top_risks": ["...", "...", "..."],
  "ticker_final_signals": [
    {{"ticker": "...", "signal": "hold", "confidence": 0.8,
      "adjusted_rationale": "...", "priority": "high|medium|low"}}
  ],
  "rebalancing_actions": [
    {{"rank": 1, "ticker": "...", "action": "trim|buy_more|sell|hold",
      "rationale": "...", "suggested_delta_pct": -3.5}}
  ],
  "executive_summary": "3–4 sentence overview of the portfolio's research findings"
}}
"""


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class TickerResearch:
    ticker: str
    name: str
    current_value: float
    pct_of_portfolio: float
    signal: str                     # buy_more | hold | trim | sell
    confidence: float               # 0.0–1.0
    key_facts: list[str]
    risk_factors: list[str]
    rationale: str
    question_answers: list[dict]
    thinking: str | None = None


@dataclass
class MacroResearch:
    question_answers: list[dict]
    portfolio_themes: list[str]
    macro_tailwinds: list[str]
    macro_headwinds: list[str]
    thinking: str | None = None


@dataclass
class RebalancingAction:
    rank: int
    ticker: str
    action: str                     # trim | buy_more | sell | hold
    rationale: str
    suggested_delta_pct: float      # negative = reduce, positive = increase


@dataclass
class ResearchReport:
    generated_at: str
    tickers_researched: list[str]
    ticker_research: dict[str, TickerResearch]
    macro_research: MacroResearch
    top_opportunities: list[str]
    top_risks: list[str]
    ticker_final_signals: list[dict]
    rebalancing_actions: list[RebalancingAction]
    executive_summary: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0


# ── AutoResearcher ────────────────────────────────────────────────────────────

class AutoResearcher:
    """Runs a Karpathy-style autonomous research loop over the portfolio.

    Phase 1 — Planning  : Claude reads the snapshot and builds a research agenda.
    Phase 2 — Research  : Per-ticker deep-dives + macro cross-portfolio analysis.
    Phase 3 — Synthesis : Integrates all findings into actionable signals.
    """

    def __init__(self, cfg: Config, store: DataStore) -> None:
        self._cfg = cfg
        self._store = store
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._total_in = 0
        self._total_out = 0

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _call(
        self,
        system: str,
        user_message: str,
        max_tokens: int = 4096,
        use_thinking: bool = True,
    ) -> tuple[str, str | None]:
        """Make a single Claude call. Returns (text, thinking)."""
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
        }
        if use_thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        text = ""
        thinking = ""

        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        thinking += event.delta.thinking
                    elif event.delta.type == "text_delta":
                        text += event.delta.text

            final = stream.get_final_message()
            self._total_in += final.usage.input_tokens
            self._total_out += final.usage.output_tokens

        return text, thinking or None

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Extract JSON from a response that may be wrapped in a markdown code block."""
        s = raw.strip()
        if "```" in s:
            parts = s.split("```")
            # take the first code block content
            for part in parts[1:]:
                content = part.lstrip("json").strip()
                if content:
                    s = content
                    break
        return json.loads(s)

    @staticmethod
    def _truncate_snapshot(payload: dict, max_holdings: int = 30) -> dict:
        result = dict(payload)
        if "holdings" in result and len(result["holdings"]) > max_holdings:
            result["holdings"] = sorted(
                result["holdings"],
                key=lambda h: float(h.get("institution_value", 0) or 0),
                reverse=True,
            )[:max_holdings]
        if "transactions" in result:
            result["transactions"] = result["transactions"][:20]
        return result

    def _build_portfolio_summary(self, snapshot: dict) -> str:
        """Short text summary of the portfolio for context injection."""
        total = float(snapshot.get("total_value", 0) or 0)
        holdings = snapshot.get("holdings", [])
        lines = [f"Total value: ${total:,.2f}  |  {len(holdings)} holdings"]
        top5 = sorted(holdings, key=lambda h: float(h.get("institution_value", 0) or 0), reverse=True)[:5]
        for h in top5:
            val = float(h.get("institution_value", 0) or 0)
            pct = val / total * 100 if total else 0
            lines.append(f"  {h.get('ticker', '?'):6s}  {pct:5.1f}%  ${val:,.0f}")
        return "\n".join(lines)

    # ── Phase 1: Plan ─────────────────────────────────────────────────────────

    def _plan_research(self, snapshot: dict, max_tickers: int) -> dict:
        """Ask Claude which tickers to research and what questions to ask."""
        prompt = _PLAN_PROMPT.format(
            max_tickers=max_tickers,
            snapshot_json=json.dumps(snapshot, indent=2),
        )
        text, _ = self._call(_PLAN_SYSTEM, prompt, max_tokens=2048, use_thinking=True)
        return self._parse_json(text)

    # ── Phase 2: Research ─────────────────────────────────────────────────────

    def _research_ticker(
        self,
        ticker_plan: dict,
        snapshot: dict,
    ) -> TickerResearch:
        """Deep-dive on a single ticker, answering its planned questions."""
        ticker = ticker_plan["ticker"]
        name = ticker_plan.get("name", ticker)
        current_value = float(ticker_plan.get("current_value", 0))
        pct = float(ticker_plan.get("pct_of_portfolio", 0))

        # Find unrealised gain from snapshot holdings
        unrealised_gain = "N/A"
        for h in snapshot.get("holdings", []):
            if h.get("ticker") == ticker:
                ug = h.get("unrealised_gain")
                ug_pct = h.get("unrealised_gain_pct")
                if ug is not None:
                    unrealised_gain = f"${float(ug):+,.2f}"
                    if ug_pct is not None:
                        unrealised_gain += f" ({float(ug_pct):+.2f}%)"
                break

        questions_list = "\n".join(
            f"{i+1}. {q}" for i, q in enumerate(ticker_plan.get("questions", []))
        )

        prompt = _RESEARCH_TICKER_PROMPT.format(
            ticker=ticker,
            name=name,
            pct_of_portfolio=pct,
            current_value=current_value,
            unrealised_gain=unrealised_gain,
            portfolio_summary=self._build_portfolio_summary(snapshot),
            questions_list=questions_list,
        )

        text, thinking = self._call(_RESEARCH_SYSTEM, prompt, max_tokens=3072, use_thinking=True)

        try:
            data = self._parse_json(text)
        except (json.JSONDecodeError, ValueError):
            # Fallback if JSON parsing fails
            data = {
                "ticker": ticker,
                "question_answers": [],
                "signal": "hold",
                "confidence": 0.5,
                "key_facts": [text[:200]],
                "risk_factors": ["Could not parse structured response"],
                "rationale": "Research completed but response was not structured.",
            }

        return TickerResearch(
            ticker=ticker,
            name=name,
            current_value=current_value,
            pct_of_portfolio=pct,
            signal=data.get("signal", "hold"),
            confidence=float(data.get("confidence", 0.5)),
            key_facts=data.get("key_facts", []),
            risk_factors=data.get("risk_factors", []),
            rationale=data.get("rationale", ""),
            question_answers=data.get("question_answers", []),
            thinking=thinking,
        )

    def _research_macro(self, macro_questions: list[str], snapshot: dict) -> MacroResearch:
        """Answer cross-portfolio macro questions."""
        questions_list = "\n".join(f"{i+1}. {q}" for i, q in enumerate(macro_questions))
        prompt = _MACRO_RESEARCH_PROMPT.format(
            snapshot_json=json.dumps(snapshot, indent=2),
            questions_list=questions_list,
        )
        text, thinking = self._call(_RESEARCH_SYSTEM, prompt, max_tokens=2048, use_thinking=True)

        try:
            data = self._parse_json(text)
        except (json.JSONDecodeError, ValueError):
            data = {
                "question_answers": [],
                "portfolio_themes": [],
                "macro_tailwinds": [],
                "macro_headwinds": [text[:200]],
            }

        return MacroResearch(
            question_answers=data.get("question_answers", []),
            portfolio_themes=data.get("portfolio_themes", []),
            macro_tailwinds=data.get("macro_tailwinds", []),
            macro_headwinds=data.get("macro_headwinds", []),
            thinking=thinking,
        )

    # ── Phase 3: Synthesise ───────────────────────────────────────────────────

    def _synthesize(
        self,
        ticker_research: dict[str, TickerResearch],
        macro_research: MacroResearch,
        snapshot: dict,
    ) -> dict:
        """Integrate all findings into a final portfolio-level report."""
        # Compact representation of per-ticker findings
        ticker_json = json.dumps(
            [
                {
                    "ticker": r.ticker,
                    "signal": r.signal,
                    "confidence": r.confidence,
                    "key_facts": r.key_facts,
                    "risk_factors": r.risk_factors,
                    "rationale": r.rationale,
                }
                for r in ticker_research.values()
            ],
            indent=2,
        )
        macro_json = json.dumps(
            {
                "portfolio_themes": macro_research.portfolio_themes,
                "macro_tailwinds": macro_research.macro_tailwinds,
                "macro_headwinds": macro_research.macro_headwinds,
            },
            indent=2,
        )

        prompt = _SYNTHESIS_PROMPT.format(
            ticker_research_json=ticker_json,
            macro_research_json=macro_json,
            snapshot_json=json.dumps(snapshot, indent=2),
        )
        text, _ = self._call(_SYNTHESIS_SYSTEM, prompt, max_tokens=3072, use_thinking=True)

        try:
            return self._parse_json(text)
        except (json.JSONDecodeError, ValueError):
            return {
                "top_opportunities": [],
                "top_risks": [],
                "ticker_final_signals": [],
                "rebalancing_actions": [],
                "executive_summary": "Synthesis parsing failed — see raw ticker research for findings.",
            }

    # ── Public API ────────────────────────────────────────────────────────────

    def conduct_research(self, max_tickers: int = 5) -> ResearchReport:
        """Run the full three-phase research loop on the latest portfolio snapshot.

        Args:
            max_tickers: Maximum number of individual positions to research in depth.
                         Lower = cheaper / faster; higher = more thorough.

        Returns:
            ResearchReport with per-ticker signals and portfolio-level synthesis.
        """
        self._total_in = 0
        self._total_out = 0

        snapshot = self._store.get_latest_snapshot_payload()
        if not snapshot:
            raise ValueError("No portfolio snapshot found. Run `portfolio-bot fetch` first.")

        snapshot = self._truncate_snapshot(snapshot)

        # ── Phase 1: Plan ──────────────────────────────────────────────────────
        plan = self._plan_research(snapshot, max_tickers=max_tickers)
        priority_tickers: list[dict] = plan.get("priority_tickers", [])
        macro_questions: list[str] = plan.get("macro_questions", [])

        # ── Phase 2: Research ──────────────────────────────────────────────────
        ticker_research: dict[str, TickerResearch] = {}
        for ticker_plan in priority_tickers:
            research = self._research_ticker(ticker_plan, snapshot)
            ticker_research[research.ticker] = research

        macro_research = self._research_macro(macro_questions, snapshot) if macro_questions else MacroResearch(
            question_answers=[], portfolio_themes=[], macro_tailwinds=[], macro_headwinds=[]
        )

        # ── Phase 3: Synthesise ────────────────────────────────────────────────
        synthesis = self._synthesize(ticker_research, macro_research, snapshot)

        rebalancing_actions = [
            RebalancingAction(
                rank=a.get("rank", i + 1),
                ticker=a.get("ticker", ""),
                action=a.get("action", "hold"),
                rationale=a.get("rationale", ""),
                suggested_delta_pct=float(a.get("suggested_delta_pct", 0)),
            )
            for i, a in enumerate(synthesis.get("rebalancing_actions", []))
        ]

        return ResearchReport(
            generated_at=datetime.utcnow().isoformat(),
            tickers_researched=list(ticker_research.keys()),
            ticker_research=ticker_research,
            macro_research=macro_research,
            top_opportunities=synthesis.get("top_opportunities", []),
            top_risks=synthesis.get("top_risks", []),
            ticker_final_signals=synthesis.get("ticker_final_signals", []),
            rebalancing_actions=rebalancing_actions,
            executive_summary=synthesis.get("executive_summary", ""),
            total_input_tokens=self._total_in,
            total_output_tokens=self._total_out,
        )

    def format_report_markdown(self, report: ResearchReport) -> str:
        """Render a ResearchReport as a markdown string suitable for Slack/email."""
        lines: list[str] = []

        lines.append(f"# Portfolio Research Report")
        lines.append(f"*Generated: {report.generated_at} UTC*\n")

        lines.append(f"## Executive Summary\n{report.executive_summary}\n")

        if report.top_opportunities:
            lines.append("## Top Opportunities")
            for opp in report.top_opportunities:
                lines.append(f"- {opp}")
            lines.append("")

        if report.top_risks:
            lines.append("## Top Risks")
            for risk in report.top_risks:
                lines.append(f"- {risk}")
            lines.append("")

        if report.rebalancing_actions:
            lines.append("## Rebalancing Actions (Prioritised)")
            for a in sorted(report.rebalancing_actions, key=lambda x: x.rank):
                direction = "▲" if a.suggested_delta_pct > 0 else "▼" if a.suggested_delta_pct < 0 else "–"
                lines.append(
                    f"{a.rank}. **{a.ticker}** — {a.action.upper()} "
                    f"{direction}{abs(a.suggested_delta_pct):.1f}pp  \n"
                    f"   _{a.rationale}_"
                )
            lines.append("")

        if report.ticker_research:
            lines.append("## Per-Ticker Research")
            for ticker, r in report.ticker_research.items():
                confidence_bar = "●" * round(r.confidence * 5) + "○" * (5 - round(r.confidence * 5))
                signal_emoji = {
                    "buy_more": "🟢 BUY MORE",
                    "hold": "🟡 HOLD",
                    "trim": "🟠 TRIM",
                    "sell": "🔴 SELL",
                }.get(r.signal, r.signal.upper())
                lines.append(f"### {ticker} — {r.name}")
                lines.append(f"Signal: **{signal_emoji}**  |  Confidence: {confidence_bar} ({r.confidence:.0%})\n")
                lines.append(f"*{r.rationale}*\n")
                if r.key_facts:
                    lines.append("**Key facts:**")
                    for f_ in r.key_facts:
                        lines.append(f"- {f_}")
                if r.risk_factors:
                    lines.append("\n**Risks:**")
                    for rf in r.risk_factors:
                        lines.append(f"- {rf}")
                lines.append("")

        if report.macro_research.portfolio_themes:
            lines.append("## Macro Context")
            lines.append("**Portfolio themes:**")
            for t in report.macro_research.portfolio_themes:
                lines.append(f"- {t}")
            if report.macro_research.macro_tailwinds:
                lines.append("\n**Tailwinds:**")
                for t in report.macro_research.macro_tailwinds:
                    lines.append(f"- {t}")
            if report.macro_research.macro_headwinds:
                lines.append("\n**Headwinds:**")
                for h in report.macro_research.macro_headwinds:
                    lines.append(f"- {h}")
            lines.append("")

        lines.append(
            f"*Tokens — input: {report.total_input_tokens:,}  "
            f"output: {report.total_output_tokens:,}*"
        )

        return "\n".join(lines)
