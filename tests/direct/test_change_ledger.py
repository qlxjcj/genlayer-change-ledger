"""Direct-mode tests for ChangeLedger.

The two things that make this contract unlike a one-shot "assess and store"
module, and what these tests pin down:

  * a version chain whose diff is computed by the contract, not reported by a model
  * pull() and aggregate() are separate transactions; aggregate() makes no
    model call at all
  * multi-source agreement is a deterministic majority vote, so splits and
    absent values become DISPUTED / MISSING rather than guesses
  * dispute() only applies a correction when a fresh reading supports it
"""

import json

import pytest

from conftest import (
    BODY_A2,
    BODY_B2,
    FIELDS,
    OWNER,
    STRANGER,
    URL_A,
    URL_B,
    URL_C,
    URL_EMPTY,
    URL_ERROR,
    URL_BAD,
    VAL_AB,
    VAL_AB2,
    VAL_C,
    VAL_EMPTY_WARRANTY,
    VAL_LONG,
    VAL_MISSING_FIELD,
    VAL_SPLIT_A,
    VAL_SPLIT_B,
    pull_all,
    pull_then_aggregate,
    remock,
    start,
)


# ==========================================================================
# start_watch validation
# ==========================================================================


def test_creates_a_watch(ledger):
    vm, c = ledger
    wid = start(vm, c)
    w = json.loads(c.get_watch(wid))
    assert w["subject"] == "Widget spec page"
    assert w["fields"] == FIELDS
    assert w["sources"] == [URL_A, URL_B, URL_C]
    assert w["min_sources"] == 2
    assert w["state"] == "ACTIVE"
    assert w["latest_version"] == 0
    assert c.get_watch_count() == 1


def test_rejects_empty_subject(ledger):
    vm, c = ledger
    with pytest.raises(Exception):
        start(vm, c, subject="")


def test_rejects_empty_or_duplicate_fields(ledger):
    vm, c = ledger
    with pytest.raises(Exception):
        start(vm, c, fields=[])
    with pytest.raises(Exception):
        start(vm, c, fields=["price", "price"])


def test_rejects_blank_field_entry(ledger):
    vm, c = ledger
    with pytest.raises(Exception):
        start(vm, c, fields=["price", "  "])


def test_rejects_too_many_fields(ledger):
    vm, c = ledger
    with pytest.raises(Exception):
        start(vm, c, fields=["f" + str(i) for i in range(20)])


def test_rejects_duplicate_or_non_http_sources(ledger):
    vm, c = ledger
    with pytest.raises(Exception):
        start(vm, c, sources=[URL_A, URL_A])
    with pytest.raises(Exception):
        start(vm, c, sources=[URL_A, "ftp://x.example.com"])


def test_rejects_bad_min_sources(ledger):
    vm, c = ledger
    with pytest.raises(Exception):
        start(vm, c, min_sources=1)
    with pytest.raises(Exception):
        start(vm, c, min_sources=99)


# ==========================================================================
# pull: extraction only, no decision
# ==========================================================================


def test_pull_stores_a_reading_for_the_round(ledger):
    vm, c = ledger
    wid = start(vm, c)
    raw = c.pull(wid, 0)
    assert json.loads(raw) == {"price": "100", "warranty": "2 years"}
    r = json.loads(c.get_reading(wid, 1, 0))
    assert r["round"] == 1
    assert r["source_index"] == 0
    assert r["url"] == URL_A


def test_pull_is_one_source_per_transaction(ledger):
    vm, c = ledger
    wid = start(vm, c)
    pull_all(vm, c, wid, (0, 1))
    assert json.loads(c.get_reading(wid, 1, 0))["url"] == URL_A
    assert json.loads(c.get_reading(wid, 1, 1))["url"] == URL_B
    assert json.loads(c.get_reading(wid, 1, 2)) == {}


def test_rejects_duplicate_pull_for_the_same_source(ledger):
    vm, c = ledger
    wid = start(vm, c)
    c.pull(wid, 0)
    with pytest.raises(Exception):
        c.pull(wid, 0)


def test_rejects_out_of_range_source_index(ledger):
    vm, c = ledger
    wid = start(vm, c)
    with pytest.raises(Exception):
        c.pull(wid, 5)
    with pytest.raises(Exception):
        c.pull(wid, -1)


def test_rejects_pull_on_a_closed_watch(ledger):
    vm, c = ledger
    wid = start(vm, c)
    c.close_watch(wid)
    with pytest.raises(Exception):
        c.pull(wid, 0)


def test_empty_source_body_is_not_a_reading(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_EMPTY, URL_B, URL_C])
    with pytest.raises(Exception):
        c.pull(wid, 0)
    assert json.loads(c.get_reading(wid, 1, 0)) == {}


def test_provider_error_is_not_a_reading(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_ERROR, URL_B, URL_C])
    with pytest.raises(Exception):
        c.pull(wid, 0)


def test_rejects_extraction_with_the_wrong_field_set(ledger):
    """The extraction must name exactly the tracked fields, no more, no less."""
    vm, c = ledger
    wid = start(vm, c, sources=[URL_BAD, URL_B, URL_C])
    with pytest.raises(Exception):
        c.pull(wid, 0)


def test_rejects_oversized_field_value(ledger):
    vm, c = ledger
    remock(vm, [("spec-a", "SPEC-A", "SPEC-A. price: 100.", VAL_LONG)])
    wid = start(vm, c, sources=[URL_A, URL_B, URL_C])
    with pytest.raises(Exception):
        c.pull(wid, 0)


# ==========================================================================
# aggregate: pure deterministic code, no model call
# ==========================================================================


def test_aggregate_makes_no_model_call(ledger):
    """Clearing every LLM mock must not stop aggregation."""
    vm, c = ledger
    wid = start(vm, c)
    pull_all(vm, c, wid)
    vm.clear_mocks()
    assert c.aggregate(wid) == 1


def test_aggregate_needs_min_sources(ledger):
    vm, c = ledger
    wid = start(vm, c, min_sources=2)
    c.pull(wid, 0)
    with pytest.raises(Exception):
        c.aggregate(wid)


def test_strict_majority_wins(ledger):
    """A and B say 100, C says 120: the majority value is stored."""
    vm, c = ledger
    wid = start(vm, c)
    pull_then_aggregate(vm, c, wid)
    f = c.get_field(wid, "price")
    assert f["value"] == "100"
    assert f["status"] == "CONFIRMED"


def test_split_vote_is_disputed_not_guessed(ledger):
    vm, c = ledger
    remock(vm, [
        ("spec-a", "SPEC-A", "SPEC-A. price: 100.", VAL_SPLIT_A),
        ("spec-b", "SPEC-B", "SPEC-B. price: 120.", VAL_SPLIT_B),
    ])
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    f = c.get_field(wid, "price")
    assert f["value"] == ""
    assert f["status"] == "DISPUTED"


def test_absent_value_is_missing(ledger):
    vm, c = ledger
    remock(vm, [
        ("spec-a", "SPEC-A", "SPEC-A. price: 100.", VAL_EMPTY_WARRANTY),
        ("spec-b", "SPEC-B", "SPEC-B. price: 100.", VAL_EMPTY_WARRANTY),
    ])
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    f = c.get_field(wid, "warranty")
    assert f["value"] == ""
    assert f["status"] == "MISSING"


def test_first_version_has_no_parent_and_reports_every_field(ledger):
    vm, c = ledger
    wid = start(vm, c)
    pull_then_aggregate(vm, c, wid)
    v = json.loads(c.get_version(wid, 1))
    assert v["version"] == 1
    assert v["parent"] == 0
    assert sorted(v["changed_fields"]) == sorted(FIELDS)
    assert v["sources_used"] == [0, 1, 2]


def test_diff_reports_only_the_fields_that_actually_changed(ledger):
    """The core claim: the contract computes the change set."""
    vm, c = ledger
    wid = start(vm, c)
    pull_then_aggregate(vm, c, wid)

    remock(vm, [
        ("spec-a", "SPEC-A2", BODY_A2, VAL_AB2),
        ("spec-b", "SPEC-B2", BODY_B2, VAL_AB2),
        ("spec-c", "SPEC-C", "SPEC-C. price: 150. warranty: 2 years.", VAL_AB2),
    ])
    pull_then_aggregate(vm, c, wid)

    v = json.loads(c.get_version(wid, 2))
    assert v["parent"] == 1
    assert v["changed_fields"] == ["price"], "warranty is unchanged and must not be listed"
    assert v["values"]["price"] == "150"
    assert v["values"]["warranty"] == "2 years"


def test_versions_form_a_chain(ledger):
    vm, c = ledger
    wid = start(vm, c)
    pull_then_aggregate(vm, c, wid)
    remock(vm, [
        ("spec-a", "SPEC-A2", BODY_A2, VAL_AB2),
        ("spec-b", "SPEC-B2", BODY_B2, VAL_AB2),
        ("spec-c", "SPEC-C", "SPEC-C. price: 150.", VAL_AB2),
    ])
    pull_then_aggregate(vm, c, wid)

    assert c.get_version_count(wid) == 2
    assert json.loads(c.get_version(wid, 1))["parent"] == 0
    assert json.loads(c.get_version(wid, 2))["parent"] == 1
    hist = c.get_history(wid)
    assert [h["version"] for h in hist] == [1, 2]


def test_aggregate_rejects_on_a_closed_watch(ledger):
    vm, c = ledger
    wid = start(vm, c)
    pull_all(vm, c, wid)
    c.close_watch(wid)
    with pytest.raises(Exception):
        c.aggregate(wid)


# ==========================================================================
# dispute: a correction only applies when a fresh reading supports it
# ==========================================================================


def test_dispute_applies_a_correction_when_a_fresh_reading_supports_it(ledger):
    """A single-source correction can resolve a split: two sources disagree, the
    caller shows one of them actually reads the other value, and the field goes
    from DISPUTED to CONFIRMED."""
    vm, c = ledger
    remock(vm, [
        ("spec-a", "SPEC-A", "SPEC-A. price: 100.", VAL_SPLIT_A),
        ("spec-b", "SPEC-B", "SPEC-B. price: 120.", VAL_SPLIT_B),
    ])
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    assert c.get_field(wid, "price") == {"value": "", "status": "DISPUTED"}

    remock(vm, [
        ("spec-a", "SPEC-A", "SPEC-A. price: 100.", VAL_SPLIT_A),
        ("spec-b", "SPEC-B", "SPEC-B. price: 100.", VAL_AB),
    ])
    vm.sender = STRANGER
    assert c.dispute(wid, 1, 1, "price", "100") is True

    v = json.loads(c.get_version(wid, 1))
    assert v["state"] == "CORRECTED"
    assert v["values"]["price"] == "100"
    assert c.get_field(wid, "price") == {"value": "100", "status": "CONFIRMED"}


def test_dispute_is_rejected_when_the_fresh_reading_disagrees(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    remock(vm, [("spec-a", "SPEC-A", "SPEC-A. price: 100.", VAL_AB),
                ("spec-b", "SPEC-B", "SPEC-B. price: 100.", VAL_AB)])
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.dispute(wid, 1, 0, "price", "999")
    assert json.loads(c.get_version(wid, 1))["state"] == "LIVE"


def test_dispute_is_rejected_when_the_stored_reading_already_matches(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.dispute(wid, 1, 0, "price", "100")


def test_dispute_rejects_unknown_field_and_bad_index(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.dispute(wid, 1, 0, "colour", "red")
    with pytest.raises(Exception):
        c.dispute(wid, 1, 9, "price", "150")


def test_dispute_rejects_oversized_claim(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.dispute(wid, 1, 0, "price", "x" * 300)


def test_only_the_latest_version_can_be_disputed(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_A, URL_B], min_sources=2)
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    remock(vm, [("spec-a", "SPEC-A2", BODY_A2, VAL_AB2),
                ("spec-b", "SPEC-B2", BODY_B2, VAL_AB2)])
    pull_then_aggregate(vm, c, wid, indices=(0, 1))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.dispute(wid, 1, 0, "price", "150")


def test_dispute_requires_a_stored_reading(ledger):
    vm, c = ledger
    wid = start(vm, c, sources=[URL_A, URL_B, URL_C], min_sources=2)
    pull_all(vm, c, wid, (0, 1))
    c.aggregate(wid)
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.dispute(wid, 1, 2, "price", "120")


# ==========================================================================
# ownership and views
# ==========================================================================


def test_only_the_owner_can_close(ledger):
    vm, c = ledger
    wid = start(vm, c)
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.close_watch(wid)
    assert json.loads(c.get_watch(wid))["state"] == "ACTIVE"


def test_cannot_close_twice(ledger):
    vm, c = ledger
    wid = start(vm, c)
    c.close_watch(wid)
    with pytest.raises(Exception):
        c.close_watch(wid)


def test_close_records_the_state(ledger):
    vm, c = ledger
    wid = start(vm, c)
    c.close_watch(wid)
    assert json.loads(c.get_watch(wid))["state"] == "CLOSED"


def test_get_field_reports_value_and_status(ledger):
    vm, c = ledger
    wid = start(vm, c)
    pull_then_aggregate(vm, c, wid)
    assert c.get_field(wid, "price") == {"value": "100", "status": "CONFIRMED"}
    assert c.get_field(wid, "nope") == {"value": "", "status": "MISSING"}


def test_unknown_watch_reads_as_empty(ledger):
    vm, c = ledger
    assert json.loads(c.get_watch("999")) == {}
    assert c.get_history("999") == []
    assert c.get_version_count("999") == 0
    assert c.get_field("999", "price") == {}


def test_reading_is_independent_per_source(ledger):
    vm, c = ledger
    wid = start(vm, c)
    pull_all(vm, c, wid)
    a = json.loads(c.get_reading(wid, 1, 0))
    cc = json.loads(c.get_reading(wid, 1, 2))
    assert a["values"]["price"] == "100"
    assert cc["values"]["price"] == "120", "per-source readings must stay unmerged"


def test_dispute_after_close_fails_and_changes_nothing(ledger):
    """A closed watch is final. dispute() must refuse it exactly like pull() and
    aggregate(), and must leave the reading, the latest version and the watch's
    current values exactly as they were."""
    vm, c = ledger
    wid = start(vm, c)
    pull_all(vm, c, wid)
    c.aggregate(wid)

    vm.sender = OWNER
    c.close_watch(wid)

    before_reading = c.get_reading(wid, 1, 0)
    before_version = c.get_version(wid, 1)
    before_watch = c.get_watch(wid)

    with pytest.raises(Exception):
        c.dispute(wid, 1, 0, "price", "999")

    assert c.get_reading(wid, 1, 0) == before_reading, "the reading must not move"
    assert c.get_version(wid, 1) == before_version, "the latest version must not move"
    assert c.get_watch(wid) == before_watch, "the watch must not move"

    after = json.loads(c.get_watch(wid))
    cached = json.loads(before_watch)
    assert after["values"] == cached["values"]
    assert after["status"] == cached["status"]
    assert after["latest_version"] == cached["latest_version"]
    assert after["state"] == "CLOSED"
