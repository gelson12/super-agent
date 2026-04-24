from __future__ import annotations

from dataclasses import dataclass

from app.models import AgentResponse


@dataclass(frozen=True)
class RankWeights:
    alpha_historical: float = 0.35
    beta_suitability: float = 0.30
    gamma_latency: float = 0.15
    delta_reliability: float = 0.15
    epsilon_cost: float = 0.05


@dataclass
class AgentProfile:
    agent_id: str
    rolling_win_rate: float = 0.5
    error_rate_7d: float = 0.0
    sample_count: int = 0


def score(
    resp: AgentResponse,
    suitability: float,
    profile: AgentProfile,
    weights: RankWeights,
    cold_start_sample_threshold: int = 30,
) -> float:
    if profile.sample_count < cold_start_sample_threshold:
        historical = 0.5
    else:
        historical = profile.rolling_win_rate
    reliability = max(0.0, 1.0 - profile.error_rate_7d)
    latency_term = 1.0 / (1.0 + resp.latency_ms / 1000.0)
    return (
        weights.alpha_historical * historical
        + weights.beta_suitability * suitability
        + weights.gamma_latency * latency_term
        + weights.delta_reliability * reliability
        - weights.epsilon_cost * resp.cost_cents
    )


def pick_winner(
    responses: list[AgentResponse],
    suitabilities: dict[str, float],
    profiles: dict[str, AgentProfile],
    weights: RankWeights,
    min_acceptable: float = 0.35,
    cold_start_sample_threshold: int = 30,
) -> tuple[AgentResponse | None, dict[str, float]]:
    scores: dict[str, float] = {}
    best: AgentResponse | None = None
    best_score = float("-inf")
    for r in responses:
        if not r.success:
            scores[r.agent_id] = 0.0
            continue
        s = score(
            r,
            suitabilities.get(r.agent_id, 0.5),
            profiles.get(r.agent_id, AgentProfile(r.agent_id)),
            weights,
            cold_start_sample_threshold,
        )
        scores[r.agent_id] = s
        if s > best_score:
            best_score = s
            best = r
    if best is None or best_score < min_acceptable:
        return None, scores
    return best, scores
