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
        """Calculates exact Margin Utilization bounds and Liquidation distance dynamically."""
        equity = self.account_summary.get("equity", 0)
        initial_margin = self.account_summary.get("initial_margin", 0)
        maint_margin = self.account_summary.get("maintenance_margin", 0)

        utilization = (initial_margin / equity * 100) if equity > 0 else 0

        status = "Safe"
        if utilization > 25:
            status = "Warning: Margin > 25%"

        return {
            "equity_btc": equity,
            "margin_utilization_pct": round(utilization, 2),
            "margin_status": status,
            "initial_margin": initial_margin,
            "maintenance_margin": maint_margin,
        }

    def _parse_instrument(self, instrument_name: str) -> Dict[str, Any]:
        """Parse Deribit instrument name like BTC-28JUN24-65000-C"""
        parts = instrument_name.split('-')
        if len(parts) == 4:
            date_str = parts[1]
            strike = float(parts[2])
            opt_type = parts[3]

            try:
                expiry = datetime.strptime(date_str, "%d%b%y")
                dte = (expiry - datetime.now(UTC).replace(tzinfo=None)).days
            except Exception:
                dte = 0

            return {"strike": strike, "type": opt_type, "dte": dte}
        return {}

    def analyze_positions(self) -> List[Dict[str, Any]]:
        """Evaluate open positions against the AIRS Playbook rules."""
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
                continue  # Skip ghost / closed positions still returned by API

            raw_delta = pos.get("delta", 0)
            contract_delta = raw_delta / size if size != 0 else 0
            gamma = pos.get("gamma", 0)
            pnl = pos.get("floating_profit_loss", 0)

            # Look up role/campaign/spread from the DB
            leg_info = get_leg_info(instrument)

            parsed = self._parse_instrument(instrument)
            if not parsed:
                continue

            strike = parsed["strike"]
            opt_type = parsed["type"]
            dte = parsed["dte"]

            metrics = f"Δ: {round(raw_delta, 2)} | DTE: {dte} | PnL: {round(pnl, 5)} BTC"

            d_obj = {
                "instrument": instrument,
                # Grouping keys (populated from DB tag; empty if untagged)
                "campaign_name": leg_info.get("campaign_name", "Untagged"),
                "spread_type":   leg_info.get("spread_type",   ""),
                "spread_id":     leg_info.get("spread_id"),
                "role":          leg_info.get("role",          ""),
                # Display
                "status": "HOLD",
                "directive": "Monitoring",
                "metrics": metrics,
                # Raw data for aggregation
                "raw_delta":     raw_delta,
                "contract_delta": contract_delta,
                "raw_gamma":     gamma,
                "raw_pnl":       pnl,
                "raw_size":      size,
                "average_price": pos.get("average_price", 0),
            }

            # ── AIRS Playbook rules ────────────────────────────────────────────
            if size < 0 and opt_type == 'C':
                # Yield Call (Leg A) — roll if ATM or strong delta drift
                if self.spot_price >= strike or abs(contract_delta) >= 0.50:
                    d_obj["status"] = "ROLL"
                    d_obj["directive"] = (
                        f"Spot >= Strike or Delta ({round(abs(contract_delta), 2)}) >= 0.50. "
                        "Roll Up and Out."
                    )
                elif dte <= 7:
                    d_obj["directive"] = "Expiring in <= 7 days. Let expire or close."

            elif size < 0 and opt_type == 'P':
                # Yield Put (Leg B) — antifragile: HOLD unless DTE or delta breach.
                # spot <= strike is NOT a roll trigger; Leg C geometric payout covers it.
                # Only roll if delta has drifted to >= 0.50 (deep ITM) AND margin is at risk.
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

    def generate_report(self) -> str:
        margin_info = self.analyze_margin()
        directives = self.analyze_positions()

        # Dashboard metrics
        satoshi_growth = ((margin_info["equity_btc"] / self.initial_equity) - 1) * 100
        long_gamma  = sum(d.get("raw_gamma", 0) for d in directives if d.get("raw_size", 0) > 0)
        short_gamma = abs(sum(d.get("raw_gamma", 0) for d in directives if d.get("raw_size", 0) < 0))
        convexity_score = (long_gamma / short_gamma) if short_gamma > 0 else (99.0 if long_gamma > 0 else 0.0)

        report = []
        report.append("📊 **AIRS Morning Briefing**")
        report.append(f"Spot Price: ${self.spot_price:,.2f}")
        report.append(f"Satoshi Growth: {satoshi_growth:+.4f}% (Baseline: {self.initial_equity} BTC)")
        report.append(f"Convexity Score: {round(convexity_score, 2)}x (Long/Short Gamma)")
        report.append(f"30d IV Rank: {self.iv_data['rank']}%")
        report.append(f"  └ *Raw DVOL: {self.iv_data['current']} (30d Range: {self.iv_data['min']} - {self.iv_data['max']})*")
        report.append(f"Margin Utilization: {margin_info['margin_utilization_pct']}% ({margin_info['margin_status']})")
        report.append(f"Equity: {margin_info['equity_btc']} BTC")

        report.append("\n**Directives by Campaign:**")

        # ── Group: campaign_name → spread_type → [directives] ─────────────────
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
            all_legs = [d for legs in spreads.values() for d in legs]
            cam_delta = sum(d.get("raw_delta", 0) for d in all_legs)
            cam_float = sum(d.get("raw_pnl", 0) for d in all_legs)

            # Realized PnL: query each unique spread once
            cam_realized = 0.0
            seen_spreads: set = set()
            for d in all_legs:
                sid = d.get("spread_id")
                if sid and sid not in seen_spreads:
                    cam_realized += get_realized_pnl_for_spread(sid)
                    seen_spreads.add(sid)
            cam_total = cam_float + cam_realized

            report.append(f"\n*Campaign: {cam_name}*")
            report.append(
                f"  └ Portfolio → Δ: {round(cam_delta, 3)} | Total PnL: {round(cam_total, 5)} BTC"
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

                net_credit = sum(
                    -d.get("raw_size", 0) * d.get("average_price", 0) for d in legs
                )
                credit_label = "Credit" if net_credit >= 0 else "Debit"

                report.append(f"  {spread_label}")
                report.append(
                    f"    └ Δ: {round(spr_delta, 3)} | "
                    f"PnL: {round(spr_total, 5)} BTC | "
                    f"Net {credit_label}: {round(abs(net_credit), 5)} BTC"
                )
                if spr_realized != 0:
                    report.append(
                        f"      (Floating: {round(spr_float, 5)} | Realized: {round(spr_realized, 5)})"
                    )

                for d in legs:
                    role_label = ROLE_LABELS.get(d.get("role", ""), d.get("role", "—"))
                    report.append(
                        f"  - **{d['instrument']}** [{role_label}]: "
                        f"[{d['metrics']}] → **{d['status']}**: {d['directive']}"
                    )

        return "\n".join(report)

    def get_report_data(self) -> Dict[str, Any]:
        """Returns both the text report and the underlying directive objects for bot interactivity."""
        return {
            "text": self.generate_report(),
            "directives": self.analyze_positions(),
        }
