from app.models import AgentResponse
from app.rank import AgentProfile, RankWeights, pick_winner, score


def _resp(agent_id: str, success: bool = True, latency_ms: int = 500, cost: float = 0.0) -> AgentResponse:
    return AgentResponse(
        agent_id=agent_id,
        content="x" if success else None,
        success=success,
        latency_ms=latency_ms,
        self_confidence=0.7 if success else 0.0,
        cost_cents=cost,
    )


def test_score_cold_start_uses_uniform_prior():
    weights = RankWeights()
    profile_cold = AgentProfile(agent_id="kimi", rolling_win_rate=0.9, sample_count=5)
    profile_warm = AgentProfile(agent_id="kimi", rolling_win_rate=0.9, sample_count=100)
    r = _resp("kimi", latency_ms=1000)
    cold = score(r, suitability=0.5, profile=profile_cold, weights=weights)
    warm = score(r, suitability=0.5, profile=profile_warm, weights=weights)
    assert cold < warm  # cold treated as 0.5, warm uses actual 0.9


def test_score_latency_penalises_slow_response():
    weights = RankWeights()
    profile = AgentProfile(agent_id="kimi", sample_count=50)
    fast = score(_resp("kimi", latency_ms=200), 0.5, profile, weights)
    slow = score(_resp("kimi", latency_ms=10_000), 0.5, profile, weights)
    assert fast > slow


def test_score_cost_reduces_winner():
    weights = RankWeights()
    profile = AgentProfile(agent_id="kimi", sample_count=50)
    cheap = score(_resp("kimi", cost=0.0), 0.5, profile, weights)
    expensive = score(_resp("kimi", cost=10.0), 0.5, profile, weights)
    assert cheap > expensive


def test_pick_winner_returns_highest_scorer():
    weights = RankWeights()
    responses = [_resp("a", latency_ms=5000), _resp("b", latency_ms=300)]
    profiles = {
        "a": AgentProfile("a", sample_count=50, rolling_win_rate=0.5),
        "b": AgentProfile("b", sample_count=50, rolling_win_rate=0.5),
    }
    winner, runner_up, scores = pick_winner(responses, {"a": 0.5, "b": 0.5}, profiles, weights)
    assert winner is not None
    assert winner.agent_id == "b"
    assert runner_up is not None
    assert runner_up.agent_id == "a"
    assert scores["b"] > scores["a"]


def test_pick_winner_all_failed_returns_none():
    weights = RankWeights()
    responses = [_resp("a", success=False), _resp("b", success=False)]
    winner, runner_up, scores = pick_winner(responses, {}, {}, weights)
    assert winner is None
    assert runner_up is None
    assert scores == {"a": 0.0, "b": 0.0}


def test_pick_winner_below_minimum_returns_none():
    weights = RankWeights()
    # Make every score very low by crushing suitability + reliability + historical
    responses = [_resp("a", latency_ms=60_000, cost=20.0)]
    profiles = {"a": AgentProfile("a", sample_count=50, rolling_win_rate=0.0, error_rate_7d=1.0)}
    winner, runner_up, _ = pick_winner(responses, {"a": 0.0}, profiles, weights, min_acceptable=0.5)
    assert winner is None
    assert runner_up is None
