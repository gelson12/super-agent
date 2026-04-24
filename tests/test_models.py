import pytest
from pydantic import ValidationError

from app.models import AgentResponse, RespondRequest


def test_respond_request_defaults():
    r = RespondRequest(query="hi")
    assert r.complexity == 3
    assert r.modality == "text"
    assert r.deadline_ms == 8000
    assert r.shortlist_override is None


def test_respond_request_rejects_empty_query():
    with pytest.raises(ValidationError):
        RespondRequest(query="")


def test_respond_request_rejects_out_of_range_complexity():
    with pytest.raises(ValidationError):
        RespondRequest(query="x", complexity=7)


def test_agent_response_confidence_bounds():
    with pytest.raises(ValidationError):
        AgentResponse(agent_id="x", content=None, success=False,
                      latency_ms=0, self_confidence=1.5)
