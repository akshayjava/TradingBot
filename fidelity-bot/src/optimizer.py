"""Portfolio optimizer — objective: maximise gains.

Strategy
--------
The primary objective is **maximising expected portfolio gains**, subject to
a configurable maximum per-position weight (concentration guard-rail).

Algorithm (pure-Python, no numpy required):

1. **Historical return estimation**
   For every ticker in the latest snapshot, pull its value history from
   `holdings_history`.  Compute period-over-period returns and derive:
     - mean_return        (proxy for expected gain per period)
     - volatility         (std-dev of returns — used for Kelly sizing)
     - momentum_score     (recent 3-period rolling return vs full history)

2. **Research signal overlay** (optional)
   If an `AutoResearcher` report is supplied, the signal and confidence from
   the research phase are blended into the expected-return estimate:
     signal multipliers:  buy_more → 1.3×  |  hold → 1.0×  |
                          trim    → 0.6×  |  sell → 0.0×

3. **Kelly-criterion position sizing** (partial Kelly for safety)
   For each ticker:
       edge        = mean_return
       variance    = volatility²
       kelly_full  = edge / variance        (full Kelly fraction)
       kelly_half  = kelly_full * kelly_fraction   (default 0.5 — "half Kelly")
   Kelly fractions are then normalised to sum to 1.0.

4. **Gains-maximisation allocation**
   The target weight for each ticker is a weighted blend:
       w_target = α * kelly_weight + (1-α) * momentum_weight
   where α is `kelly_alpha` (default 0.6).
   Positions with negative mean_return or "sell" signal are set to 0.

5. **Concentration cap**
   Any weight > `max_position_pct` is clamped and excess redistributed
   pro-rata among uncapped positions.

6. **Rebalancing plan**
   Comparing target weights to current weights gives a sorted list of
   RebalancingActions ordered by |delta| (largest move first).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.data_store import DataStore

if TYPE_CHECKING:
    from src.auto_researcher import ResearchReport

# Signal multipliers for expected-return blending
_SIGNAL_MULT: dict[str, float] = {
    "buy_more": 1.30,
    "hold":     1.00,
    "trim":     0.60,
    "sell":     0.00,
}

# Minimum data points to compute reliable statistics
_MIN_HISTORY = 2


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class TickerStats:
    ticker: str
    name: str
    current_value: float
    current_weight: float           # fraction of total portfolio
    mean_return: float              # average period-over-period return
    volatility: float               # std-dev of returns (0 if insufficient data)
    momentum_score: float           # recent vs long-term return ratio
    kelly_weight: float             # optimal Kelly fraction (uncapped)
    research_signal: str = "hold"
    research_confidence: float = 0.5
    adjusted_expected_return: float = 0.0


@dataclass
class RebalancingAction:
    ticker: str
    name: str
    current_pct: float              # % of portfolio now
    target_pct: float               # % after rebalancing
    delta_pct: float                # target - current  (positive = buy more)
    delta_dollars: float            # approximate dollar change
    action: str                     # "buy_more" | "trim" | "sell" | "hold"
    priority: str                   # "high" | "medium" | "low"


@dataclass
class OptimizationResult:
    total_portfolio_value: float
    ticker_stats: dict[str, TickerStats]
    current_weights: dict[str, float]
    target_weights: dict[str, float]
    rebalancing_actions: list[RebalancingAction]
    expected_portfolio_gain: float  # weighted sum of adjusted expected returns
    data_quality_note: str          # warns when history is sparse


# ── Optimizer ─────────────────────────────────────────────────────────────────

class PortfolioOptimizer:
    """Gains-maximising portfolio optimiser.

    Args:
        store:              DataStore instance for historical data.
        max_position_pct:   Hard cap on any single position (default 0.30 = 30%).
        kelly_alpha:        Blend weight between Kelly and momentum (default 0.6).
        kelly_fraction:     Partial-Kelly scaling factor (default 0.5 for safety).
        history_limit:      Number of historical data-points to use per ticker.
        min_delta_pct:      Ignore rebalancing moves smaller than this (default 1%).
    """

    def __init__(
        self,
        store: DataStore,
        max_position_pct: float = 0.30,
        kelly_alpha: float = 0.60,
        kelly_fraction: float = 0.50,
        history_limit: int = 30,
        min_delta_pct: float = 1.0,
    ) -> None:
        self._store = store
        self._max_pos = max_position_pct
        self._kelly_alpha = kelly_alpha
        self._kelly_fraction = kelly_fraction
        self._history_limit = history_limit
        self._min_delta_pct = min_delta_pct

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _compute_returns(values: list[float]) -> list[float]:
        """Period-over-period returns from a value series (oldest → newest)."""
        if len(values) < 2:
            return []
        return [
            (values[i] - values[i - 1]) / values[i - 1]
            for i in range(1, len(values))
            if values[i - 1] != 0
        ]

    def _ticker_stats(
        self,
        ticker: str,
        name: str,
        current_value: float,
        current_weight: float,
    ) -> TickerStats:
        """Compute statistics for a single ticker from SQLite history."""
        rows = self._store.get_ticker_history(ticker, limit=self._history_limit)
        # rows are newest-first; reverse to get chronological order
        values = [
            float(r["institution_value"])
            for r in reversed(rows)
            if r["institution_value"]
        ]

        returns = self._compute_returns(values)

        if len(returns) >= _MIN_HISTORY:
            mean_ret = statistics.mean(returns)
            volatility = statistics.stdev(returns) if len(returns) > 1 else abs(mean_ret)
        else:
            # Fall back to unrealised gain pct if available
            mean_ret = 0.0
            volatility = 0.05  # default 5% volatility assumption

        # Momentum: compare recent 3 periods vs full history
        if len(returns) >= 4:
            recent = statistics.mean(returns[-3:])
            overall = statistics.mean(returns)
            momentum_score = recent / overall if overall != 0 else 1.0
        else:
            momentum_score = 1.0

        # Kelly sizing:  f* = edge / variance
        variance = volatility ** 2
        if variance > 0 and mean_ret > 0:
            kelly_full = mean_ret / variance
            kelly_weight = kelly_full * self._kelly_fraction
        else:
            kelly_weight = 0.0  # no edge → no Kelly weight

        return TickerStats(
            ticker=ticker,
            name=name,
            current_value=current_value,
            current_weight=current_weight,
            mean_return=mean_ret,
            volatility=volatility,
            momentum_score=max(0.0, momentum_score),
            kelly_weight=max(0.0, kelly_weight),
        )

    def _apply_research_overlay(
        self,
        stats: dict[str, TickerStats],
        report: "ResearchReport",
    ) -> None:
        """Mutate stats in-place: blend research signals into expected return."""
        signal_map: dict[str, tuple[str, float]] = {}
        for sig in report.ticker_final_signals:
            t = sig.get("ticker", "")
            signal_map[t] = (sig.get("signal", "hold"), float(sig.get("confidence", 0.5)))

        for ticker, s in stats.items():
            signal, conf = signal_map.get(ticker, ("hold", 0.5))
            mult = _SIGNAL_MULT.get(signal, 1.0)
            # Blend: research confidence weights how much we trust the signal
            blended_mult = conf * mult + (1 - conf) * 1.0
            s.research_signal = signal
            s.research_confidence = conf
            s.adjusted_expected_return = s.mean_return * blended_mult

            # Zero out Kelly weight for explicit "sell" signals
            if signal == "sell":
                s.kelly_weight = 0.0

    def _normalize_weights(self, raw: dict[str, float]) -> dict[str, float]:
        """Normalise weights to sum to 1.0, respecting max_position cap."""
        total = sum(raw.values())
        if total <= 0:
            # Equal weight fallback
            n = len(raw)
            return {t: 1.0 / n for t in raw} if n else {}

        # Initial normalisation
        weights = {t: v / total for t, v in raw.items()}

        # Iterative capping: clamp and redistribute excess
        for _ in range(20):  # max iterations
            capped: dict[str, float] = {}
            excess = 0.0
            uncapped_sum = 0.0
            for t, w in weights.items():
                if w > self._max_pos:
                    capped[t] = self._max_pos
                    excess += w - self._max_pos
                else:
                    uncapped_sum += w
            if excess <= 1e-9:
                break  # converged
            # Redistribute excess pro-rata among uncapped positions
            for t in weights:
                if t in capped:
                    weights[t] = capped[t]
                elif uncapped_sum > 0:
                    weights[t] += excess * (weights[t] / uncapped_sum)
        return weights

    @staticmethod
    def _priority(delta_pct: float) -> str:
        abs_d = abs(delta_pct)
        if abs_d >= 5.0:
            return "high"
        if abs_d >= 2.0:
            return "medium"
        return "low"

    # ── Public API ────────────────────────────────────────────────────────────

    def optimize(self, research_report: "ResearchReport | None" = None) -> OptimizationResult:
        """Run the gains-maximising optimisation.

        Args:
            research_report: Optional ResearchReport from AutoResearcher.
                             When provided, research signals are blended into
                             the expected-return estimates.

        Returns:
            OptimizationResult with target weights and rebalancing actions.
        """
        snapshot = self._store.get_latest_snapshot_payload()
        if not snapshot:
            raise ValueError("No snapshot found. Run `portfolio-bot fetch` first.")

        total_value = float(snapshot.get("total_value", 0) or 0)
        if total_value == 0:
            raise ValueError("Portfolio total value is zero — cannot optimise.")

        holdings = snapshot.get("holdings", [])

        # ── Compute per-ticker stats ───────────────────────────────────────────
        stats: dict[str, TickerStats] = {}
        for h in holdings:
            ticker = h.get("ticker") or ""
            if not ticker:
                continue
            val = float(h.get("institution_value", 0) or 0)
            weight = val / total_value
            s = self._ticker_stats(
                ticker=ticker,
                name=h.get("name", ticker),
                current_value=val,
                current_weight=weight,
            )
            # Seed adjusted_expected_return with mean_return before overlay
            s.adjusted_expected_return = s.mean_return
            stats[ticker] = s

        # ── Apply research overlay ─────────────────────────────────────────────
        data_quality_note = ""
        tickers_with_history = sum(1 for s in stats.values() if s.mean_return != 0)
        if tickers_with_history < len(stats) * 0.5:
            data_quality_note = (
                f"Only {tickers_with_history}/{len(stats)} tickers have sufficient "
                "history. Gains-maximisation will rely heavily on research signals. "
                "Run daily fetches to build historical data."
            )

        if research_report:
            self._apply_research_overlay(stats, research_report)

        # ── Build raw target weights ───────────────────────────────────────────
        # Primary driver: adjusted expected return (gains maximisation)
        # Secondary driver: Kelly fraction (bet sizing)
        # Blend: w_raw = kelly_alpha * kelly + (1-kelly_alpha) * momentum_adj

        raw_weights: dict[str, float] = {}
        for ticker, s in stats.items():
            # Gains-maximisation: proportional to expected return
            gain_score = max(0.0, s.adjusted_expected_return)
            # Momentum boost
            momentum_score = max(0.0, s.momentum_score)
            # Kelly sizing
            kelly = max(0.0, s.kelly_weight)

            # Combined score — gains-first weighting
            combined = self._kelly_alpha * kelly + (1 - self._kelly_alpha) * (
                gain_score * momentum_score if gain_score > 0 else 0.0
            )
            raw_weights[ticker] = combined

        # If all raw weights are zero (no history, no research), fall back to
        # equal-weight among non-sell tickers
        if sum(raw_weights.values()) == 0:
            sell_tickers = {
                sig.get("ticker")
                for sig in (research_report.ticker_final_signals if research_report else [])
                if sig.get("signal") == "sell"
            }
            eligible = [t for t in stats if t not in sell_tickers]
            raw_weights = {t: 1.0 for t in eligible}
            data_quality_note += " Fell back to equal-weight (insufficient history + no research)."

        # ── Normalise with concentration cap ──────────────────────────────────
        target_weights = self._normalize_weights(raw_weights)

        # ── Build rebalancing plan ─────────────────────────────────────────────
        current_weights = {t: s.current_weight for t, s in stats.items()}

        # Include tickers targeted to 0 that are not in target_weights (new sells)
        for ticker in current_weights:
            if ticker not in target_weights:
                target_weights[ticker] = 0.0

        actions: list[RebalancingAction] = []
        for ticker, current_w in current_weights.items():
            target_w = target_weights.get(ticker, 0.0)
            delta_pct = (target_w - current_w) * 100
            if abs(delta_pct) < self._min_delta_pct:
                continue  # too small to bother

            delta_dollars = delta_pct / 100 * total_value
            if delta_pct > 0:
                action = "buy_more"
            elif delta_pct < -current_w * 100 * 0.99:  # near-full exit
                action = "sell"
            else:
                action = "trim"

            actions.append(
                RebalancingAction(
                    ticker=ticker,
                    name=stats[ticker].name,
                    current_pct=round(current_w * 100, 2),
                    target_pct=round(target_w * 100, 2),
                    delta_pct=round(delta_pct, 2),
                    delta_dollars=round(delta_dollars, 2),
                    action=action,
                    priority=self._priority(delta_pct),
                )
            )

        # Sort by absolute delta descending (biggest moves first)
        actions.sort(key=lambda a: abs(a.delta_pct), reverse=True)

        # ── Expected portfolio gain ────────────────────────────────────────────
        expected_gain = sum(
            target_weights.get(t, 0.0) * s.adjusted_expected_return
            for t, s in stats.items()
        )

        return OptimizationResult(
            total_portfolio_value=total_value,
            ticker_stats=stats,
            current_weights=current_weights,
            target_weights=target_weights,
            rebalancing_actions=actions,
            expected_portfolio_gain=expected_gain,
            data_quality_note=data_quality_note,
        )

    def format_result_markdown(
        self,
        result: OptimizationResult,
        max_actions: int = 10,
    ) -> str:
        """Render an OptimizationResult as markdown."""
        lines: list[str] = []
        lines.append("# Portfolio Optimisation — Maximise Gains\n")
        lines.append(
            f"**Total portfolio value:** ${result.total_portfolio_value:,.2f}  \n"
            f"**Expected gain (weighted):** {result.expected_portfolio_gain * 100:+.3f}% per period\n"
        )

        if result.data_quality_note:
            lines.append(f"> ⚠️  {result.data_quality_note}\n")

        if result.rebalancing_actions:
            lines.append("## Rebalancing Actions")
            lines.append(
                "| # | Ticker | Action | Current % | Target % | Delta % | Delta $ | Priority |"
            )
            lines.append("|---|--------|--------|-----------|----------|---------|---------|----------|")
            for i, a in enumerate(result.rebalancing_actions[:max_actions], 1):
                arrow = "▲" if a.delta_pct > 0 else "▼"
                lines.append(
                    f"| {i} | **{a.ticker}** | {a.action.upper()} | "
                    f"{a.current_pct:.1f}% | {a.target_pct:.1f}% | "
                    f"{arrow}{abs(a.delta_pct):.1f}% | "
                    f"${a.delta_dollars:+,.0f} | {a.priority} |"
                )
            lines.append("")
        else:
            lines.append("*Portfolio is already optimally weighted — no significant rebalancing needed.*\n")

        # Stats table
        lines.append("## Ticker Analysis")
        lines.append("| Ticker | Exp. Return | Volatility | Momentum | Kelly w% | Research Signal |")
        lines.append("|--------|-------------|------------|----------|----------|-----------------|")
        for ticker, s in sorted(
            result.ticker_stats.items(),
            key=lambda kv: kv[1].adjusted_expected_return,
            reverse=True,
        ):
            sig_map = {
                "buy_more": "🟢 BUY MORE",
                "hold": "🟡 HOLD",
                "trim": "🟠 TRIM",
                "sell": "🔴 SELL",
            }
            lines.append(
                f"| {ticker} | {s.adjusted_expected_return * 100:+.2f}% | "
                f"{s.volatility * 100:.2f}% | {s.momentum_score:.2f}x | "
                f"{s.kelly_weight * 100:.1f}% | {sig_map.get(s.research_signal, s.research_signal)} |"
            )

        return "\n".join(lines)
