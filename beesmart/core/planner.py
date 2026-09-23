"""Whole-segment portfolio construction and independent public-filter checks."""

from math import isfinite

from .domain import ARPU_VALUES, CALL_VALUES, DATA_VALUES, Domain, membership_mask
from .models import ArmStats, Candidate, Plan


class Planner:
    def __init__(self, domain: Domain):
        self.domain = domain
        self.segments_by_cell = {}
        for segment in domain.segments:
            self.segments_by_cell.setdefault(segment.cell, []).append(segment)
        self.minimum_segment_size = min((s.n for s in domain.segments), default=None)
        # Validation has its own filter index, derived from the private stable
        # profile snapshot. It deliberately does not trust candidate masks.
        self._validation_filters = {}
        self._all_rows = (1 << len(domain.profile)) - 1

    def candidates(self, stats: dict, ledger: dict, budget: float, contacts: int) -> list[Candidate]:
        result = []
        for arm, observation in sorted(stats.items()):
            lower = observation.estimate().lower
            if lower <= 0:
                continue
            for segment in self.segments_by_cell.get(arm[:2], []):
                if segment.n > contacts:
                    continue
                fresh = segment.fresh_arpu(ledger.get(arm[:2], 0))
                for channel in self.domain.channels.values():
                    effect = channel.lower_effect(lower)
                    cost = channel.cost * segment.n
                    gain = effect * fresh - cost
                    if gain > 0 and cost <= budget:
                        result.append(Candidate(segment, arm, channel, gain, effect, fresh))
        return result

    def solve(self, stats: dict, ledger: dict, budget: float, contacts: int) -> Plan:
        if contacts <= 0 or budget < 0:
            return Plan()
        candidates = self.candidates(stats, ledger, budget, contacts)
        plans = []
        for weight in (0, 1, 10):
            chosen, union, money, reach = [], 0, budget, contacts
            for _ in range(10):
                available = [c for c in candidates if not c.segment.mask & union
                             and c.cost <= money and c.segment.n <= reach]
                if not available:
                    break

                def rank(candidate):
                    money_fraction = candidate.cost / budget if budget > 0 else 0.0
                    denominator = candidate.segment.n / contacts + weight * money_fraction + 0.1
                    return (-candidate.gain_low / denominator, -candidate.gain_low,
                            candidate.cost, candidate.segment.n, candidate.key)

                best = min(available, key=rank)
                chosen.append(best)
                union |= best.segment.mask
                money -= best.cost
                reach -= best.segment.n
            chosen.sort(key=lambda c: (-c.gain_low, c.key))
            plans.append(Plan(chosen, weight))
        return min(plans, key=lambda p: (-p.gain_low, p.cost, p.contacts, p.signature))

    def fallback(self, stats: dict, ledger: dict, budget: float, contacts: int) -> Plan:
        push = self.domain.channels.get("push")
        if push is None:
            return Plan(fallback=True)
        possibilities = []
        arms = sorted(stats)
        if not arms:
            # Without observations every target has the same loss/size. The
            # canonical tie-break always chooses the first allowed target, so
            # materializing every tariff would repeat an identical comparison.
            arms = [cell + (next(target for target in self.domain.tariffs if target != cell[0]),)
                    for cell in sorted(self.domain.cells)
                    if any(target != cell[0] for target in self.domain.tariffs)]
        for arm in arms:
            lower = stats[arm].estimate().lower if arm in stats else 0.0
            for segment in self.segments_by_cell.get(arm[:2], []):
                if segment.n > contacts or segment.n * push.cost > budget:
                    continue
                loss = max(0.0, -push.multiplier * lower) * segment.arpu_sum
                fresh = segment.fresh_arpu(ledger.get(segment.cell, 0))
                effect = push.lower_effect(lower)
                # A_fresh bounds incremental positive lift only. For a negative
                # fallback, charge all ARPU to avoid understating possible loss.
                gain = effect * (fresh if lower >= 0 else segment.arpu_sum) - segment.n * push.cost
                candidate = Candidate(segment, arm, push, gain, effect, fresh)
                possibilities.append((loss, segment.n, candidate.key, candidate))
        if not possibilities:
            if stats:
                # Observations may cover only oversized/depleted cells. A
                # different unobserved cell can still provide a legal final
                # campaign; do not turn available mandatory output into [].
                return self.fallback({}, ledger, budget, contacts)
            return Plan(fallback=True)
        return Plan([min(possibilities, key=lambda item: item[:3])[3]], fallback=True)

    def validate(self, campaigns: list[dict], budget: float, contacts: int) -> dict:
        """Reapply actual filters, rather than trusting cached candidate masks."""
        errors, union, cost, count = [], 0, 0.0, 0
        if not 1 <= len(campaigns) <= 10:
            errors.append("campaign_count")
        allowed = {"campaign_name", "target_tariff", "channel", "filter_current_tariff",
                   "filter_arpu_segment", "filter_data_segment", "filter_call_segment"}
        categories = {"filter_arpu_segment": ARPU_VALUES, "filter_data_segment": DATA_VALUES,
                      "filter_call_segment": CALL_VALUES}
        for index, campaign in enumerate(campaigns):
            prefix = f"campaign_{index + 1}:"
            if set(campaign) - allowed:
                errors.append(prefix + "unknown_fields")
            if campaign.get("target_tariff") not in self.domain.tariffs:
                errors.append(prefix + "unknown_target")
            if campaign.get("filter_current_tariff") not in self.domain.tariffs:
                errors.append(prefix + "current_tariff")
            if campaign.get("filter_arpu_segment") not in ARPU_VALUES:
                errors.append(prefix + "arpu_filter")
            for field, values in categories.items():
                if field in campaign and campaign[field] not in values:
                    errors.append(prefix + field)
            channel = self.domain.channels.get(campaign.get("channel"))
            if channel is None:
                errors.append(prefix + "channel")
                continue
            selected = self._all_rows
            for field in ("current_tariff", "arpu_segment", "data_segment", "call_segment"):
                if f"filter_{field}" in campaign:
                    value = campaign[f"filter_{field}"]
                    key = (field, value)
                    if key not in self._validation_filters:
                        matches = self.domain.profile[field].eq(value).fillna(False).to_numpy(dtype=bool)
                        self._validation_filters[key] = membership_mask(matches)
                    selected &= self._validation_filters[key]
            selected_count = selected.bit_count()
            if not 0 < selected_count <= 5000:
                errors.append(prefix + "segment_size")
            if selected & union:
                errors.append(prefix + "overlap")
            union |= selected
            count += selected_count
            cost += selected_count * channel.cost
        if count > contacts:
            errors.append("contact_budget")
        if not isfinite(cost) or cost > budget:
            errors.append("money_budget")
        return {"ok": not errors, "errors": errors, "final_contacts": count, "final_cost": cost,
                "overlap_count": count - union.bit_count(), "cap_flags": [] if not errors else errors.copy()}
