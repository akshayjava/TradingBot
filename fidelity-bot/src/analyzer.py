"""Portfolio analysis using the Anthropic Claude API with adaptive thinking."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anthropic

from src.config import Config
from src.data_store import DataStore

MODEL = "claude-opus-4-6"

# ── Prompt templates ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an aggressive, gains-focused portfolio analyst with access to
real-time Fidelity brokerage data pulled via Plaid.

Primary objective: **maximise portfolio gains**.

Your role is to:
1. Identify highest-conviction opportunities to grow portfolio value
2. Flag under-performing positions that are dragging on returns
3. Detect concentration risk that limits upside (not just protects downside)
4. Provide actionable, specific recommendations with clear expected-gain rationale

Guidelines:
- Lead with the biggest gain opportunities first, not just the biggest positions
- Cite tickers, dollar amounts, percentages, and expected return impact
- Call out lagging positions explicitly — dead weight costs opportunity
- Flag any single position >20 % that is underperforming the rest of portfolio
- Format with clear markdown headings
- Daily digest: under 600 words; be direct and actionable
"""

_DAILY_DIGEST_PROMPT = """Analyse the following portfolio snapshot and provide a gains-focused daily digest.

**Portfolio Snapshot (fetched {fetched_at}):**
{snapshot_json}

**Previous Snapshot for Comparison:**
{prev_snapshot_json}

Please provide:
1. **Performance vs Prior Day** – total value change ($, %), biggest winners and losers
2. **Gain Momentum** – which positions are accelerating vs decelerating returns
3. **Dead Weight** – positions with negative or flat returns dragging on portfolio gains
4. **Top Opportunities** – positions or sectors with the highest upside from here
5. **Recent Transactions** – notable buys/sells/dividends; assess if they were gains-optimal
6. **Action Items** – up to 3 specific, prioritised moves to maximise portfolio gains

Be direct: name the tickers, give the numbers, explain the gain impact.
"""

_QUERY_PROMPT = """You are a financial assistant with access to this portfolio data.

**Current Portfolio Snapshot:**
{snapshot_json}

**Historical Snapshots (last {history_count} pulls):**
{history_json}

User question: {question}

Answer the question thoroughly and specifically, citing relevant portfolio data.
"""

_DRIFT_ALERT_PROMPT = """Analyse the following portfolio drift and decide if it warrants an urgent alert.

**Current Holdings:**
{current_json}

**Previous Holdings:**
{previous_json}

**Drift Threshold:** {threshold_pct}%

For each position where absolute allocation has changed by more than the threshold:
- Identify what drifted and by how much
- Assess whether it is due to market moves vs trades
- Recommend if action is needed

Respond with a JSON object in this exact format:
{{
  "alert_required": true/false,
  "drifted_positions": [
    {{"ticker": "...", "current_pct": ..., "previous_pct": ..., "delta_pct": ..., "action": "..."}}
  ],
  "summary": "one-sentence summary"
}}
"""


_RESEARCH_DIGEST_PROMPT = """You are synthesising two sources of information into a single gains-focused
portfolio action plan.

**Source 1 — Live Portfolio Snapshot (fetched {fetched_at}):**
{snapshot_json}

**Source 2 — Previous Snapshot:**
{prev_snapshot_json}

**Source 3 — Auto-Research Report (fundamental + macro analysis):**
{research_report}

Your task: produce a unified **Gains-Optimised Daily Brief** that:

1. **Performance Summary** — today's P&L, best/worst movers
2. **Research-Backed Signals** — for each researched ticker, what does the
   combined price action + fundamental research say?  Is the current weight
   appropriate given the gain outlook?
3. **Top 3 Rebalancing Moves** — specific ticker, direction (buy/trim/sell),
   estimated gain impact, and urgency
4. **Macro Tailwinds / Headwinds** — from the research report, which macro
   factors are most relevant to today's portfolio gains?
5. **Watch List** — 2–3 unresearched positions that warrant attention next

Be specific, gains-first, and keep the total under 700 words.
"""


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class AnalysisResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    thinking: str | None = None


@dataclass
class DriftAnalysis:
    alert_required: bool
    drifted_positions: list[dict]
    summary: str
    raw_content: str


# ── Analyzer ──────────────────────────────────────────────────────────────────

class PortfolioAnalyzer:
    """Uses Claude Opus 4.6 with adaptive thinking to analyse portfolio data."""

    def __init__(self, cfg: Config, store: DataStore) -> None:
        self._cfg = cfg
        self._store = store
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _call_claude(
        self,
        user_message: str,
        max_tokens: int = 4096,
        use_thinking: bool = True,
    ) -> AnalysisResult:
        """Single Claude API call with streaming and adaptive thinking."""
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        }
        if use_thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        full_text = ""
        thinking_text = ""
        usage_in = 0
        usage_out = 0

        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    pass
                elif event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        thinking_text += event.delta.thinking
                    elif event.delta.type == "text_delta":
                        full_text += event.delta.text

            final = stream.get_final_message()
            usage_in = final.usage.input_tokens
            usage_out = final.usage.output_tokens

        return AnalysisResult(
            content=full_text,
            model=MODEL,
            input_tokens=usage_in,
            output_tokens=usage_out,
            thinking=thinking_text or None,
        )

    @staticmethod
    def _truncate_snapshot(payload: dict, max_holdings: int = 50) -> dict:
        """Trim very large snapshots to stay within reasonable token counts."""
        result = dict(payload)
        if "holdings" in result and len(result["holdings"]) > max_holdings:
            result["holdings"] = sorted(
                result["holdings"],
                key=lambda h: float(h.get("institution_value", 0) or 0),
                reverse=True,
            )[:max_holdings]
        if "transactions" in result and len(result["transactions"]) > 100:
            result["transactions"] = result["transactions"][:100]
        return result

    # ── Public API ────────────────────────────────────────────────────────────

    def daily_digest(self) -> AnalysisResult:
        """Generate the daily portfolio digest using the two latest snapshots."""
        snapshots = self._store.get_snapshot_payloads(limit=2)
        if not snapshots:
            return AnalysisResult(
                content="No portfolio data available. Run `portfolio-bot fetch` first.",
                model=MODEL,
                input_tokens=0,
                output_tokens=0,
            )

        current = self._truncate_snapshot(snapshots[0])
        previous = self._truncate_snapshot(snapshots[1]) if len(snapshots) > 1 else {}

        prompt = _DAILY_DIGEST_PROMPT.format(
            fetched_at=current.get("fetched_at", "unknown"),
            snapshot_json=json.dumps(current, indent=2),
            prev_snapshot_json=json.dumps(previous, indent=2) if previous else "N/A",
        )
        return self._call_claude(prompt, max_tokens=4096)

    def answer_query(self, question: str) -> AnalysisResult:
        """Answer a free-form question about the portfolio."""
        snapshots = self._store.get_snapshot_payloads(limit=5)
        if not snapshots:
            return AnalysisResult(
                content="No portfolio data available. Run `portfolio-bot fetch` first.",
                model=MODEL,
                input_tokens=0,
                output_tokens=0,
            )

        current = self._truncate_snapshot(snapshots[0])
        history = [self._truncate_snapshot(s) for s in snapshots[1:]]

        prompt = _QUERY_PROMPT.format(
            snapshot_json=json.dumps(current, indent=2),
            history_count=len(history),
            history_json=json.dumps(history, indent=2),
            question=question,
        )
        return self._call_claude(prompt, max_tokens=8192)

    def check_drift(self) -> DriftAnalysis | None:
        """
        Compare current vs previous holdings and flag drift above the configured threshold.
        Returns None if fewer than 2 snapshots exist.
        """
        snapshots = self._store.get_snapshot_payloads(limit=2)
        if len(snapshots) < 2:
            return None

        current = self._truncate_snapshot(snapshots[0])
        previous = self._truncate_snapshot(snapshots[1])

        threshold_pct = self._cfg.alert_drift_threshold * 100

        prompt = _DRIFT_ALERT_PROMPT.format(
            current_json=json.dumps(current.get("holdings", []), indent=2),
            previous_json=json.dumps(previous.get("holdings", []), indent=2),
            threshold_pct=threshold_pct,
        )

        result = self._call_claude(prompt, max_tokens=2048, use_thinking=False)

        # Parse JSON response
        try:
            # Extract JSON from markdown code block if present
            raw = result.content.strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            return DriftAnalysis(
                alert_required=bool(data.get("alert_required")),
                drifted_positions=data.get("drifted_positions", []),
                summary=data.get("summary", ""),
                raw_content=result.content,
            )
        except (json.JSONDecodeError, KeyError, IndexError):
            return DriftAnalysis(
                alert_required=False,
                drifted_positions=[],
                summary="Could not parse drift analysis response.",
                raw_content=result.content,
            )

    def research_digest(self, research_report_md: str) -> AnalysisResult:
        """Generate a digest that incorporates auto-research findings.

        Takes the markdown output from AutoResearcher.format_report_markdown()
        and synthesises it with the latest snapshot into a unified, gains-focused
        daily digest.
        """
        snapshots = self._store.get_snapshot_payloads(limit=2)
        if not snapshots:
            return AnalysisResult(
                content="No portfolio data available. Run `portfolio-bot fetch` first.",
                model=MODEL,
                input_tokens=0,
                output_tokens=0,
            )

        current = self._truncate_snapshot(snapshots[0])
        previous = self._truncate_snapshot(snapshots[1]) if len(snapshots) > 1 else {}

        prompt = _RESEARCH_DIGEST_PROMPT.format(
            fetched_at=current.get("fetched_at", "unknown"),
            snapshot_json=json.dumps(current, indent=2),
            prev_snapshot_json=json.dumps(previous, indent=2) if previous else "N/A",
            research_report=research_report_md,
        )
        return self._call_claude(prompt, max_tokens=5120)

    def check_daily_loss(self) -> tuple[bool, str]:
        """
        Check if total portfolio value dropped by more than the daily-loss threshold.
        Returns (alert_required, message).
        """
        snapshots = self._store.get_snapshot_payloads(limit=2)
        if len(snapshots) < 2:
            return False, "Not enough data for daily loss check."

        current_val = Decimal(str(snapshots[0].get("total_value", 0) or 0))
        previous_val = Decimal(str(snapshots[1].get("total_value", 0) or 0))

        if previous_val == 0:
            return False, "Previous portfolio value is zero."

        change_pct = (current_val - previous_val) / previous_val
        threshold = Decimal(str(self._cfg.alert_daily_loss_threshold))

        if change_pct < -threshold:
            msg = (
                f"Portfolio dropped {abs(change_pct) * 100:.2f}% "
                f"(${current_val - previous_val:,.2f}) vs previous snapshot. "
                f"Current value: ${current_val:,.2f}"
            )
            return True, msg

        return False, f"Daily change: {change_pct * 100:+.2f}% — within threshold."
