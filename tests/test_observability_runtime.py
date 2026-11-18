from __future__ import annotations

from guanjia.observability import ObservationClient


def test_observation_events_are_redacted_persisted_and_retried(tmp_path):
    delivered: list[dict] = []
    should_fail = True

    def sender(events):
        nonlocal should_fail
        if should_fail:
            should_fail = False
            return False
        delivered.extend(events)
        return True

    client = ObservationClient(
        database_path=tmp_path / "outbox.sqlite",
        token="test-token",
        sender=sender,
        auto_start=False,
    )
    trace = client.start_trace("voice-session", "你好")
    span = trace.start_span("asr", "语音识别", {
        "authorization": "Bearer should-not-be-stored",
        "text": "开灯",
    })
    child_span = trace.start_span("main-agent", "主智能体处理", {"text": "开灯"})
    assert child_span.parent_span_id == span.span_id
    traceparent = span.traceparent
    child_span.finish("completed")
    span.finish("completed", {"recognizedText": "开灯"})
    trace.finish("completed", {"verdict": "accept"})

    assert len(traceparent.split("-")) == 4
    assert client.flush_once() == 0
    assert client._db.execute("SELECT count(*) FROM observation_outbox").fetchone()[0] > 0
    assert client.flush_once() > 0
    assert client._db.execute("SELECT count(*) FROM observation_outbox").fetchone()[0] == 0

    span_started = next(event for event in delivered if event["eventType"] == "span.started")
    assert span_started["payload"]["input"]["authorization"] == "[REDACTED]"
    assert any(event["eventType"] == "trace.completed" for event in delivered)
    assert span_started["producerSequence"] > 0
    client.close(timeout=0)


def test_observation_outbox_is_bounded_and_does_not_replace_old_events(tmp_path):
    def sender(events):
        return len(events) > 0

    client = ObservationClient(
        database_path=tmp_path / "bounded.sqlite",
        sender=sender,
        auto_start=False,
        max_events=1,
    )
    count_before = client._db.execute("SELECT count(*) FROM observation_outbox").fetchone()[0]
    client._record("runtime.error", payload={"message": "second event"})
    count_after = client._db.execute("SELECT count(*) FROM observation_outbox").fetchone()[0]
    assert count_after == count_before == 1
    client.close(timeout=0)
