from __future__ import annotations

from dataclasses import dataclass

from app.models import AgentResponse


@dataclass(frozen=True)
class RankWeights:
    alpha_historical: float = 0.35
    beta_suitability: float = 0.25
    gamma_latency: float = 0.10
    delta_reliability: float = 0.15
    epsilon_cost: float = 0.05
    zeta_content_depth: float = 0.10


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
    content_depth: float = 0.5,
) -> float:
    if profile.sample_count < cold_start_sample_threshold:
        historical = 0.5
    else:
        historical = profile.rolling_win_rate
    reliability = max(0.0, 1.0 - profile.error_rate_7d)
    latency_term = 1.0 / (1.0 + resp.latency_ms / 1000.0)
    zeta = getattr(weights, "zeta_content_depth", 0.10)
    return (
        weights.alpha_historical * historical
        + weights.beta_suitability * suitability
        + weights.gamma_latency * latency_term
        + weights.delta_reliability * reliability
        - weights.epsilon_cost * resp.cost_cents
        + zeta * content_depth
    )


def pick_winner(
    responses: list[AgentResponse],
    suitabilities: dict[str, float],
    profiles: dict[str, AgentProfile],
    weights: RankWeights,
    min_acceptable: float = 0.35,
    cold_start_sample_threshold: int = 30,
) -> tuple[AgentResponse | None, AgentResponse | None, dict[str, float]]:
    # Compute max content length across successful responses for depth normalisation.
    # A 2000-char answer scores 1.0; shorter answers scale proportionally (capped at 1.0).
    # This rewards substantive, complete answers without penalising very long ones.
    _DEPTH_TARGET_CHARS = 2000
    max_len = max(
        (len(r.content or "") for r in responses if r.success),
        default=1,
    )
    depth_scale = max(max_len, _DEPTH_TARGET_CHARS)

    scored_responses: list[tuple[float, AgentResponse]] = []
    scores: dict[str, float] = {}

    for r in responses:
        if not r.success:
            scores[r.agent_id] = 0.0
            continue
        content_depth = min(1.0, len(r.content or "") / depth_scale)
        s = score(
            r,
            suitabilities.get(r.agent_id, 0.5),
            profiles.get(r.agent_id, AgentProfile(r.agent_id)),
            weights,
            cold_start_sample_threshold,
            content_depth=content_depth,
        )
        scores[r.agent_id] = s
        scored_responses.append((s, r))

    scored_responses.sort(key=lambda t: t[0], reverse=True)

    if not scored_responses or scored_responses[0][0] < min_acceptable:
        return None, None, scores

    winner = scored_responses[0][1]
    runner_up = scored_responses[1][1] if len(scored_responses) > 1 else None
    return winner, runner_up, scores
