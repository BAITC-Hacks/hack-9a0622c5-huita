from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path = Path(__file__).resolve().parent.parent
    run_timeout_seconds: float = 540
    retained_runs: int = 20
    max_events: int = 400


LIMITS = {
    "total_budget": 100_000,
    "max_total_contacts": 15_000,
    "max_campaigns": 10,
    "max_pilots": 20,
    "max_customers_per_campaign": 5_000,
}

