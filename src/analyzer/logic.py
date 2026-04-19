from typing import List, Dict, Any
from datetime import datetime, UTC
from database.queries import get_leg_info, get_realized_pnl_for_spread
from database.models import ROLE_LABELS


class AIRSAnalyzer:
    def __init__(self, spot_price: float, positions: List[Dict[str, Any]],
                 account_summary: Dict[str, Any] = None, iv_data: dict = None,
                 initial_equity: float = 1.0):
        self.spot_price = spot_price
        self.positions = positions
        self.account_summary = account_summary or {}
        self.iv_data = iv_data or {"rank": 0.0, "current": 0.0, "min": 0.0, "max": 0.0}
        self.initial_equity = initial_equity

    def analyze_margin(self) -> Dict[str, Any]:
        equity = self.account_summary.get("equity", 0)
        initial_margin = self.account_summary.get("initial_margin", 0)
        maint_margin = self.account_summary.get("maintenance_margin", 0)
        utilization = (initial_margin / equity * 100) if equity > 0 else 0
        if utilization > 40:
            status = "Critical"
        elif utilization > 25:
            status = "Breach"
        elif utilization > 20:
            status = "Warning"
        else:
            status = "Safe"
        return {
            "equity_btc": equity,
            "margin_utilization_pct": round(utilization, 2),
            "margin_status": status,
            "initial_margin": initial_margin,
            "maintenance_margin": maint_margin,
        }

    def _parse_instrument(self, instrument_name: str) -> Dict[str, Any]:
        parts = instrument_name.split('-')
        if len(parts) == 4:
            try:
                expiry = datetime.strptime(parts[1], "%d%b%y")
                dte = (expiry - datetime.now(UTC).replace(tzinfo=None)).days
                return {"strike": float(parts[2]), "type": parts[3], "dte": dte}
            except Exception:
                pass
        return {}

    def analyze_positions(self) -> List[Dict[str, Any]]:
        directives = []

        if not self.positions:
            return [{"action": (
                "Action Required: No positions open. Initiate AIRS Strategy:\n"
                "  • Leg A (Yield Call): Sell 0.5x @ 0.10 Delta (30-45 DTE)\n"
                "  • Leg B (Yield Put): Sell 0.2x @ 0.10 Delta (30-45 DTE)\n"
                "  • Leg C (Crash Hedge): Buy 0.6x @ 0.03 Delta (30-45 DTE)\n"
                "  • Leg D (Moon Hedge): Buy 1.0x @ 0.02 Delta (30-45 DTE)"
            )}]

        for pos in self.positions:
            instrument = pos.get("instrument_name", "")
            size = pos.get("size", 0)
            if size == 0:
                continue

            raw_delta = pos.get("delta", 0)
            contract_delta = raw_delta / size if size != 0 else 0
            gamma = pos.get("gamma", 0)
            pnl = pos.get("floating_profit_loss", 0)
            leg_info = get_leg_info(instrument)
            parsed = self._parse_instrument(instrument)
            if not parsed:
                continue

            strike, opt_type, dte = parsed["strike"], parsed["type"], parsed["dte"]
            metrics = f"Δ: {round(raw_delta, 2)} | DTE: {dte} | PnL: {round(pnl, 5)} BTC"

            d_obj = {
                "instrument":    instrument,
                "campaign_name": leg_info.get("campaign_name", "Untagged"),
                "spread_type":   leg_info.get("spread_type",   ""),
                "spread_id":     leg_info.get("spread_id"),
                "role":          leg_info.get("role",          ""),
                "status":        "HOLD",
                "directive":     "Monitoring",
                "metrics":       metrics,
                "raw_delta":     raw_delta,
                "contract_delta": contract_delta,
                "raw_gamma":     gamma,
                "raw_pnl":       pnl,
                "raw_size":      size,
                "average_price": pos.get("average_price", 0),
            }

            # ── AIRS Playbook rules ────────────────────────────────────────────
            if size < 0 and opt_type == 'C':
                if self.spot_price >= strike or abs(contract_delta) >= 0.50:
                    d_obj["status"] = "ROLL"
                    d_obj["directive"] = (
                        f"Spot >= Strike or Delta ({round(abs(contract_delta), 2)}) >= 0.50. "
                        "Roll Up and Out."
                    )
                elif dte <= 7:
                    d_obj["directive"] = "Expiring in <= 7 days. Let expire or close."

            elif size < 0 and opt_type == 'P':
                # Antifragile rule: spot <= strike is NOT a roll trigger (Leg C covers it).
                # Only roll on deep ITM delta breach; check margin before acting.
                if dte <= 7:
                    d_obj["directive"] = "Expiring in <= 7 days. Let expire or close."
                elif abs(contract_delta) >= 0.50:
                    d_obj["status"] = "ROLL"
                    d_obj["directive"] = (
                        f"Delta ({round(abs(contract_delta), 2)}) >= 0.50 (deep ITM). "
                        "Check margin first; roll Down and Out only if margin > 20%."
                    )

            elif size > 0:
                if dte <= 7:
                    d_obj["directive"] = "Expiring in <= 7 days. Close if profitable, else let expire."
                else:
                    d_obj["directive"] = "Long hedge active."

            directives.append(d_obj)

        return directives

    # ── Invariant helpers ──────────────────────────────────────────────────────

    def _by_role_totals(self, directives: List[Dict]) -> Dict[str, Dict]:
        """Aggregate live position data by role for the by-role summary block."""
        totals: Dict[str, Dict] = {
            role: {"size": 0.0, "delta": 0.0, "gamma": 0.0, "pnl": 0.0, "count": 0}
            for role in ("yield_call", "yield_put", "crash_hedge", "moon_hedge", "untagged")
        }
        for d in directives:
            if "action" in d:
                continue
            role = d.get("role") or "untagged"
            if role not in totals:
                role = "untagged"
            totals[role]["size"]  += abs(d.get("raw_size", 0))
            totals[role]["delta"] += d.get("raw_delta", 0)
            totals[role]["gamma"] += d.get("raw_gamma", 0)
            totals[role]["pnl"]   += d.get("raw_pnl", 0)
            totals[role]["count"] += 1
        return totals

    def _compute_invariants(self, by_role: Dict, equity_btc: float,
                            margin_pct: float) -> Dict[str, Dict]:
        """Compute the 4 AIRS portfolio invariants."""
        b = by_role["yield_put"]["size"]
        c = by_role["crash_hedge"]["size"]
        a = by_role["yield_call"]["size"]
        long_g  = by_role["crash_hedge"]["gamma"] + by_role["moon_hedge"]["gamma"]
        short_g = abs(by_role["yield_call"]["gamma"] + by_role["yield_put"]["gamma"])

        put_ratio  = (c / b) if b > 0 else (99.0 if c > 0 else 0.0)
        call_floor = (a / equity_btc) if equity_btc > 0 else 0.0
        convexity  = (long_g / short_g) if short_g > 0 else (99.0 if long_g > 0 else 0.0)

        return {
            "put_ratio":  {"value": round(put_ratio, 2),  "label": "Put ratio  C:B", "target": "≥ 3.0", "ok": put_ratio >= 3.0},
            "call_floor": {"value": round(call_floor, 2), "label": "Call floor A/Eq","target": "≤ 1.0", "ok": call_floor <= 1.0},
            "convexity":  {"value": round(convexity, 2),  "label": "Convexity Lγ/Sγ","target": "> 1.0", "ok": convexity > 1.0},
            "margin":     {"value": round(margin_pct, 1), "label": "Margin util",     "target": "≤ 25%", "ok": margin_pct <= 25.0},
        }

    # ── Report ─────────────────────────────────────────────────────────────────

    def generate_report(self) -> str:
        margin_info = self.analyze_margin()
        directives  = self.analyze_positions()
        equity      = margin_info["equity_btc"]
        margin_pct  = margin_info["margin_utilization_pct"]

        satoshi_growth = ((equity / self.initial_equity) - 1) * 100 if self.initial_equity else 0

        by_role    = self._by_role_totals(directives)
        invariants = self._compute_invariants(by_role, equity, margin_pct)

        report = []

        # ── Header ────────────────────────────────────────────────────────────
        report.append("📊 *AIRS Morning Briefing*")
        report.append(f"Spot: ${self.spot_price:,.2f}  |  IV Rank: {self.iv_data['rank']}%")
        report.append(
            f"  └ DVOL: {self.iv_data['current']} "
            f"(30d {self.iv_data['min']}–{self.iv_data['max']})"
        )
        report.append(
            f"Equity: {equity} BTC  |  "
            f"Margin: {margin_pct}% [{margin_info['margin_status']}]"
        )
        report.append(f"Satoshi Growth: {satoshi_growth:+.4f}% (vs {self.initial_equity} BTC baseline)")

        # ── Portfolio Invariants ───────────────────────────────────────────────
        report.append("\n*Portfolio Invariants*")
        for inv in invariants.values():
            tick = "✅" if inv["ok"] else "❌"
            report.append(
                f"  {inv['label']:<18} = {inv['value']:<6} {tick}  (target {inv['target']})"
            )

        # ── By-Role Summary ────────────────────────────────────────────────────
        report.append("\n*By Role — Portfolio-wide*")
        role_order = [
            ("yield_call",  "A Yield Call  ", "Short"),
            ("yield_put",   "B Yield Put   ", "Short"),
            ("crash_hedge", "C Crash Hedge ", "Long "),
            ("moon_hedge",  "D Moon Hedge  ", "Long "),
        ]
        for role_key, role_label, direction in role_order:
            r = by_role[role_key]
            if r["count"] == 0:
                continue
            report.append(
                f"  {role_label} | {direction} {round(r['size'], 2):>5} "
                f"| Δ {round(r['delta'], 3):>+7.3f} "
                f"| PnL {round(r['pnl'], 5):>+.5f} BTC"
            )
        if by_role["untagged"]["count"] > 0:
            report.append(
                f"  ⚠️  Untagged: {by_role['untagged']['count']} position(s) — "
                "use /tag to include in campaign PnL"
            )

        # ── Campaigns ──────────────────────────────────────────────────────────
        report.append("\n*Directives by Campaign:*")

        campaigns: Dict[str, Dict[str, list]] = {}
        action_msgs = []
        for d in directives:
            if "action" in d:
                action_msgs.append(d["action"])
                continue
            cam = d["campaign_name"]
            spr = d["spread_type"] or "Untagged"
            campaigns.setdefault(cam, {}).setdefault(spr, []).append(d)

        if not campaigns:
            for msg in action_msgs:
                report.append(msg)
            return "\n".join(report)

        for cam_name, spreads in campaigns.items():
            all_legs  = [d for legs in spreads.values() for d in legs]
            cam_delta = sum(d.get("raw_delta", 0) for d in all_legs)
            cam_float = sum(d.get("raw_pnl", 0) for d in all_legs)

            seen_spreads: set = set()
            cam_realized = 0.0
            for d in all_legs:
                sid = d.get("spread_id")
                if sid and sid not in seen_spreads:
                    cam_realized += get_realized_pnl_for_spread(sid)
                    seen_spreads.add(sid)
            cam_total = cam_float + cam_realized

            report.append(f"\n*Campaign: {cam_name}*")
            report.append(
                f"  └ Δ: {round(cam_delta, 3)} | Total PnL: {round(cam_total, 5)} BTC"
            )
            if cam_realized != 0:
                report.append(
                    f"    (Floating: {round(cam_float, 5)} | Realized: {round(cam_realized, 5)})"
                )

            for spread_type, legs in spreads.items():
                spread_label = (
                    "📞 Call Spread (A+D)" if spread_type == "call_spread" else
                    "📉 Put Spread  (B+C)" if spread_type == "put_spread"  else
                    "⚠️ Untagged"
                )
                spr_delta = sum(d.get("raw_delta", 0) for d in legs)
                spr_float = sum(d.get("raw_pnl", 0) for d in legs)
                spr_realized = 0.0
                seen: set = set()
                for d in legs:
                    sid = d.get("spread_id")
                    if sid and sid not in seen:
                        spr_realized += get_realized_pnl_for_spread(sid)
                        seen.add(sid)
                spr_total = spr_float + spr_realized
                net_credit = sum(-d.get("raw_size", 0) * d.get("average_price", 0) for d in legs)
                credit_label = "Credit" if net_credit >= 0 else "Debit"

                report.append(f"  {spread_label}")
                report.append(
                    f"    └ Δ: {round(spr_delta, 3)} | "
                    f"PnL: {round(spr_total, 5)} BTC | "
                    f"Net {credit_label}: {round(abs(net_credit), 5)} BTC"
                )
                if spr_realized != 0:
                    report.append(
                        f"      (Float: {round(spr_float, 5)} | Real: {round(spr_realized, 5)})"
                    )

                for d in legs:
                    role_label = ROLE_LABELS.get(d.get("role", ""), d.get("role", "—"))
                    report.append(
                        f"  - *{d['instrument']}* [{role_label}]: "
                        f"[{d['metrics']}] → *{d['status']}*: {d['directive']}"
                    )

        return "\n".join(report)

    def get_report_data(self) -> Dict[str, Any]:
        return {
            "text": self.generate_report(),
            "directives": self.analyze_positions(),
        }
