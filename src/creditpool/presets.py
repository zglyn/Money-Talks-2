"""Named Policy Preset templates for Program Rules.

A preset fills budget, per-Team cap, eligibility stance, Returns allowed, and an
expiry offset. It is not a runtime policy engine. Apply-time gates still read the
Program row and each Team's eligible flag.
"""

from dataclasses import dataclass
from enum import StrEnum


class EligibilityStance(StrEnum):
    LISTED = "listed"
    OPEN = "open"


STANCE_LABELS: dict[EligibilityStance, str] = {
    EligibilityStance.LISTED: "Eligible Teams are an explicit list",
    EligibilityStance.OPEN: "New Teams start eligible",
}


@dataclass(frozen=True)
class ProgramRules:
    total_budget_cents: int
    per_team_cap_cents: int
    eligibility_stance: EligibilityStance
    returns_allowed: bool
    expires_at: float


@dataclass(frozen=True)
class PolicyPreset:
    name: str
    total_budget_cents: int
    per_team_cap_cents: int
    eligibility_stance: EligibilityStance
    returns_allowed: bool
    expiry_offset_seconds: int

    def rules(self, now: float) -> ProgramRules:
        return ProgramRules(
            total_budget_cents=self.total_budget_cents,
            per_team_cap_cents=self.per_team_cap_cents,
            eligibility_stance=self.eligibility_stance,
            returns_allowed=self.returns_allowed,
            expires_at=now + self.expiry_offset_seconds,
        )

    @property
    def expiry_offset_hours(self) -> int:
        return self.expiry_offset_seconds // 3600


POLICY_PRESETS: tuple[PolicyPreset, ...] = (
    PolicyPreset(
        name="Hackathon weekend",
        total_budget_cents=1_000_000,
        per_team_cap_cents=200_000,
        eligibility_stance=EligibilityStance.LISTED,
        returns_allowed=True,
        expiry_offset_seconds=48 * 3600,
    ),
    PolicyPreset(
        name="Workshop day",
        total_budget_cents=200_000,
        per_team_cap_cents=50_000,
        eligibility_stance=EligibilityStance.OPEN,
        returns_allowed=True,
        expiry_offset_seconds=8 * 3600,
    ),
    PolicyPreset(
        name="Returns closed",
        total_budget_cents=500_000,
        per_team_cap_cents=100_000,
        eligibility_stance=EligibilityStance.LISTED,
        returns_allowed=False,
        expiry_offset_seconds=7 * 24 * 3600,
    ),
)


def require(name: str) -> PolicyPreset:
    for preset in POLICY_PRESETS:
        if preset.name == name:
            return preset
    raise ValueError(f"unknown Policy Preset {name!r}")
