"""Sequential search orchestration; this module never imports a scorer/model."""

from math import isfinite, sqrt
from pathlib import Path
from time import monotonic

from .domain import load_domain
from .models import ArmStats, NOISE_STD, Plan, Z
from .planner import Planner

ROOT = Path(__file__).resolve().parents[2]


class CampaignAgent:
    """Public-API agent. Reusing an instance never reuses statistical state."""

    def __init__(self, event_sink=None, *, history_path=None, policy_path=None):
        self.event_sink = event_sink
        self.history_path = Path(history_path) if history_path is not None else ROOT / "data/change_tariff.csv"
        self.policy_path = Path(policy_path) if policy_path is not None else ROOT / "frozen_policy.json"
        self.diagnostics = {}
        self.last_plan = []

    def emit(self, name: str, payload: dict) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(name, payload)
            except Exception:
                # Observability is deliberately noncritical; callbacks may be a
                # disconnected web client. No observation/decision is discarded.
                pass

    def act(self, env) -> list[dict]:
        started = monotonic()
        self.diagnostics, self.last_plan = {}, []
        domain = load_domain(env, self.history_path, self.policy_path)
        planner = Planner(domain)
        stats, ledger, pilots, flags = {}, {}, [], list(domain.flags)
        used = 0
        normal_plan, final_plan = Plan(), Plan()
        start_budget, start_contacts = float(env.remaining_budget), int(env.remaining_contacts)
        if not isfinite(start_budget) or start_budget < 0 or start_contacts < 0:
            raise ValueError("Invalid public resource balances")
        self.emit("run_start", {"profile_size": len(domain.profile), "budget": start_budget,
                  "contacts": start_contacts, "pilots_left": int(env.pilots_left),
                  "policy_hash": domain.policy_hash, "policy_source": domain.policy_source,
                  "queue_size": len(domain.queue), "first_wave_limit": 14, "flags": flags.copy()})

        def balances():
            return float(env.remaining_budget), int(env.remaining_contacts)

        def detailed_plan(plan):
            return [{**c.campaign(i + 1), "n_customers": c.segment.n, "cost": c.cost,
                     "gain_low": c.gain_low, "effect_low": c.effect_low, "fresh_arpu": c.fresh_arpu,
                     "arpu_sum": c.segment.arpu_sum, "pilot_contacts_in_cell": ledger.get(c.arm[:2], 0),
                     "estimate": stats[c.arm].estimate().as_dict() if c.arm in stats else None,
                     "pilot_numbers": stats[c.arm].pilot_numbers.copy() if c.arm in stats else []}
                    for i, c in enumerate(plan.candidates)]

        def rebuild():
            nonlocal normal_plan, final_plan
            budget, contacts = balances()
            normal_plan = planner.solve(stats, ledger, budget, contacts)
            final_plan = normal_plan if normal_plan.candidates else planner.fallback(stats, ledger, budget, contacts)
            validation = planner.validate(final_plan.campaigns(), budget, contacts)
            if not validation["ok"]:
                final_plan = planner.fallback(stats, ledger, budget, contacts)
                validation = planner.validate(final_plan.campaigns(), budget, contacts)
            if self.event_sink is not None:
                self.emit("plan_selected", {"campaigns": detailed_plan(final_plan),
                          "gain_low": final_plan.gain_low, "cost": final_plan.cost,
                          "contacts": final_plan.contacts, "fallback": final_plan.fallback,
                          "weight": final_plan.weight, "validation": validation})

        def pilot_size_channel(arm):
            budget, contacts = balances()
            # On depleted/custom environments preserve room for one legal final
            # segment. With the official 15,000 contacts this does not reduce n.
            minimum_final = planner.minimum_segment_size if planner.minimum_segment_size is not None else contacts
            n = min(200, domain.cells[arm[:2]].n, max(0, contacts - minimum_final))
            if n < 10:
                return None
            for name in ("sms", "push"):
                channel = domain.channels.get(name)
                if channel and channel.multiplier <= 1 and n * channel.cost <= budget:
                    return n, channel
            return None

        def can_continue():
            if monotonic() - started >= 480:
                if "soft_deadline" not in flags:
                    flags.append("soft_deadline")
                return False
            return int(env.pilots_left) > 0 and used < 20

        def pilot(arm, reason, selection):
            nonlocal used
            n, channel = selection
            before_budget, before_contacts = balances()
            used += 1
            self.emit("arm_selected", {"arm": list(arm), "reason": reason, "requested_n": n,
                      "channel": channel.name, "pilot_number": used})
            try:
                result = env.run_pilot(target_tariff=arm[2], channel=channel.name, n_customers=n,
                                      filter_current_tariff=arm[0], filter_arpu_segment=arm[1])
            except Exception as error:
                # A failing external call can have committed a contact already.
                # Do not retry. Public deltas establish spending when available.
                _, after_contacts = balances()
                spent = max(0, before_contacts - after_contacts)
                ledger[arm[:2]] = ledger.get(arm[:2], 0) + (spent if spent else n)
                flags.append("pilot_error:" + type(error).__name__)
                self.emit("pilot_error", {"arm": list(arm), "error_type": type(error).__name__,
                          "confirmed_contacts": spent, "retry": False})
                rebuild()
                return False
            budget, contacts = balances()
            spent = max(0, before_contacts - contacts)
            ledger[arm[:2]] = ledger.get(arm[:2], 0) + spent
            try:
                actual = int(result["n_customers"])
                observed = float(result["observed_lift_ratio"])
                if actual != spent or actual <= 0 or actual > n or not isfinite(observed):
                    raise ValueError("Invalid pilot result or inconsistent resource ledger")
                statistics = stats.get(arm) or ArmStats()
                statistics.update(observed, actual, channel, used)
                stats[arm] = statistics
            except (KeyError, TypeError, ValueError, OverflowError):
                flags.append("invalid_pilot_observation")
                self.emit("pilot_error", {"arm": list(arm), "error_type": "InvalidObservation", "retry": False})
                rebuild()
                return False
            estimate = statistics.estimate()
            entry = {"pilot": f"pilot_{used}", "pilot_number": used, "arm": list(arm), "current_tariff": arm[0],
                     "arpu_segment": arm[1], "target_tariff": arm[2], "channel": channel.name,
                     "reason": reason, "requested_n": n, "n_customers": actual,
                     "observed_lift_ratio": observed, "cost": before_budget - budget,
                     **estimate.as_dict(), "remaining_budget": budget, "remaining_contacts": contacts}
            pilots.append(entry)
            self.emit("pilot_result", entry.copy())
            rebuild()
            return True

        rebuild()
        stopped_on_error = False
        for arm in domain.queue:
            if not can_continue():
                break
            selection = pilot_size_channel(arm)
            if selection is None:
                continue
            if not pilot(arm, "first_wave", selection):
                stopped_on_error = True
                break

        while not stopped_on_error and can_continue():
            budget, contacts = balances()
            rho = max(0.0, normal_plan.gain_low / max(1, normal_plan.contacts))
            options = []
            sms = domain.channels.get("sms")
            if sms is None:
                break
            for arm, statistics in sorted(stats.items()):
                if monotonic() - started >= 480:
                    break
                estimate = statistics.estimate()
                if not (estimate.mean > 0 and estimate.lower <= 0 < estimate.upper):
                    continue
                selection = pilot_size_channel(arm)
                if selection is None:
                    continue
                n, channel = selection
                for k in range(1, min(int(env.pilots_left), 20 - used) + 1):
                    pilot_cost, pilot_contacts = k * n * channel.cost, k * n
                    if pilot_cost > budget or pilot_contacts >= contacts:
                        break
                    lower_after = estimate.mean - Z / sqrt(statistics.precision + k * n * channel.multiplier**2 / NOISE_STD**2)
                    if lower_after <= 0:
                        continue
                    # Correct the draft's gross-SMS Q: reserve the future SMS
                    # campaign's full cost and size AFTER all k pilot expenses.
                    # Compare only whole feasible segments and deduct its cost.
                    futures = []
                    for segment in planner.segments_by_cell.get(arm[:2], []):
                        if segment.n > contacts - pilot_contacts:
                            continue
                        final_cost = segment.n * sms.cost
                        if final_cost > budget - pilot_cost:
                            continue
                        fresh = segment.fresh_arpu(ledger.get(arm[:2], 0) + pilot_contacts)
                        gain = sms.lower_effect(lower_after) * fresh - final_cost
                        futures.append((gain, segment.key))
                    if futures:
                        future_gain = max(futures, key=lambda item: (item[0], item[1]))[0]
                        q = (future_gain - pilot_cost - pilot_contacts * rho) / k
                        if q > 0:
                            options.append((q, arm, selection, k, future_gain))
                    # Policy uses the first k crossing the positive boundary,
                    # not an exhaustive optimiser of hypothetical future means.
                    break
            if not options:
                self.emit("explore_decision", {"action": "stop", "reason": "no_profitable_confirmation"})
                break
            q, arm, selection, k, future_gain = min(options, key=lambda item: (-item[0], item[1]))
            self.emit("explore_decision", {"action": "repeat", "arm": list(arm), "q": q,
                      "required_pilots": k, "future_sms_gain_low": future_gain, "contact_opportunity_cost": rho})
            if not can_continue() or not pilot(arm, "confirmation", selection):
                break

        rebuild()
        budget, contacts = balances()
        campaigns = final_plan.campaigns()
        validation = planner.validate(campaigns, budget, contacts)
        if not stats:
            flags.append("no_valid_pilot_observation")
        if final_plan.fallback:
            flags.append("fallback_plan")
        if not validation["ok"]:
            flags.append("no_feasible_final_plan")
        self.last_plan = detailed_plan(final_plan)
        estimates = [{"arm": list(arm), **observation.estimate().as_dict(),
                      "contacts": observation.contacts, "pilot_numbers": observation.pilot_numbers.copy()}
                     for arm, observation in sorted(stats.items())]
        self.diagnostics = {"pilots": pilots, "campaigns": self.last_plan, "estimates": estimates,
                            "validation": validation, "flags": sorted(set(flags)),
                            "duration_seconds": monotonic() - started, "policy_hash": domain.policy_hash,
                            "policy_source": domain.policy_source, "n_pilots": len(pilots),
                            "pilot_attempts": used, "gain_low": final_plan.gain_low,
                            "final_cost": final_plan.cost, "final_contacts": final_plan.contacts,
                            "remaining_budget": budget, "remaining_contacts": contacts,
                            "pilot_cost": start_budget - budget, "pilot_contacts": start_contacts - contacts}
        self.emit("run_end", {key: value for key, value in self.diagnostics.items()
                  if key not in ("pilots", "campaigns", "estimates")})
        return campaigns
