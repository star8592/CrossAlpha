from crossalpha.domain.models import ObservationEnvelope, SourceType


def test_observation_has_point_in_time_fields() -> None:
    obs = ObservationEnvelope(source_type=SourceType.CHAIN, source_id="ethereum", observation_type="transfer", payload={"value": "1"})
    assert obs.known_at is not None
    assert obs.observed_at is not None
    assert obs.source_type == SourceType.CHAIN
