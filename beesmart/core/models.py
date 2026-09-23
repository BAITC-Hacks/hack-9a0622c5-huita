"""Small value types and the Gaussian observation model."""

from dataclasses import dataclass, field
from math import isfinite, sqrt
from statistics import NormalDist

NOISE_STD = 0.804
Z = NormalDist().inv_cdf(0.95)
CellKey = tuple[str, str]
ArmKey = tuple[str, str, str]


@dataclass(frozen=True)
class Channel:
    name: str
    cost: float
    multiplier: float

    def lower_effect(self, lower: float) -> float:
        if self.multiplier <= 1 or lower < 0:
            return self.multiplier * lower
        # Conversion saturation makes multiplier * theta optimistic for calls.
        return lower


@dataclass(frozen=True)
class Estimate:
    mean: float
    se: float
    lower: float
    upper: float

    def as_dict(self) -> dict:
        return {"mean": self.mean, "se": self.se, "lower": self.lower, "upper": self.upper}


@dataclass
class ArmStats:
    precision: float = 0.0
    weighted_sum: float = 0.0
    contacts: int = 0
    pilot_numbers: list[int] = field(default_factory=list)

    def update(self, observed: float, actual_n: int, channel: Channel, pilot_number: int) -> None:
        if not isfinite(observed) or actual_n <= 0 or not 0 < channel.multiplier <= 1:
            raise ValueError("Invalid nonsaturating pilot observation")
        precision = actual_n * channel.multiplier**2 / NOISE_STD**2
        next_precision = self.precision + precision
        next_sum = self.weighted_sum + observed / channel.multiplier * precision
        if not isfinite(next_precision) or next_precision <= 0 or not isfinite(next_sum):
            raise ValueError("Pilot observation exceeds finite statistical range")
        if not isfinite(next_sum / next_precision) or not isfinite(1 / next_precision):
            raise ValueError("Pilot estimate exceeds finite statistical range")
        self.precision = next_precision
        self.weighted_sum = next_sum
        self.contacts += actual_n
        self.pilot_numbers.append(pilot_number)

    def estimate(self) -> Estimate:
        if self.precision <= 0:
            raise ValueError("No valid pilot observation")
        mean = self.weighted_sum / self.precision
        se = sqrt(1 / self.precision)
        return Estimate(mean, se, mean - Z * se, mean + Z * se)


@dataclass(frozen=True)
class Segment:
    key: tuple[str, str, str, str]
    filters: dict[str, str]
    mask: int
    n: int
    arpu_sum: float
    top_prefix: tuple[float, ...]

    @property
    def cell(self) -> CellKey:
        return self.key[:2]

    def fresh_arpu(self, pilot_contacts: int) -> float:
        return max(0.0, self.arpu_sum - self.top_prefix[min(max(0, pilot_contacts), self.n)])


@dataclass(frozen=True)
class Cell:
    key: CellKey
    n: int
    arpu_sum: float


@dataclass(frozen=True)
class Candidate:
    segment: Segment
    arm: ArmKey
    channel: Channel
    gain_low: float
    effect_low: float
    fresh_arpu: float

    @property
    def key(self) -> tuple:
        return self.segment.key + (self.arm[2], self.channel.name)

    @property
    def cost(self) -> float:
        return self.segment.n * self.channel.cost

    def campaign(self, number: int) -> dict:
        return {"campaign_name": f"C{number:02d}_{self.arm[0]}_{self.arm[1]}_{self.arm[2]}_{self.channel.name}",
                **self.segment.filters, "target_tariff": self.arm[2], "channel": self.channel.name}


@dataclass
class Plan:
    candidates: list[Candidate] = field(default_factory=list)
    weight: int = 0
    fallback: bool = False

    @property
    def gain_low(self) -> float:
        return sum(c.gain_low for c in self.candidates)

    @property
    def cost(self) -> float:
        return sum(c.cost for c in self.candidates)

    @property
    def contacts(self) -> int:
        return sum(c.segment.n for c in self.candidates)

    @property
    def signature(self) -> tuple:
        return tuple(sorted(c.key for c in self.candidates))

    def campaigns(self) -> list[dict]:
        return [c.campaign(i + 1) for i, c in enumerate(self.candidates)]
