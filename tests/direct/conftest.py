"""Fixtures for ChangeLedger direct-mode tests.

The contract has no payable entry points, so no value hooks are needed. What
matters here is the split between the two transaction types:

  pull()      one fetch + one extraction, no decision
  aggregate() no model call at all, pure deterministic aggregation

so the mocks are keyed per source (URL for the fetch, body marker for the
extraction) and can be swapped between rounds to simulate a source changing.
"""

import json
import os

import pytest

CONTRACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "change_ledger.py",
)

OWNER = "0x" + "aa" * 20
STRANGER = "0x" + "44" * 20

FIELDS = ["price", "warranty"]

URL_A = "https://spec-a.example.com/page"
URL_B = "https://spec-b.example.com/page"
URL_C = "https://spec-c.example.com/page"
URL_EMPTY = "https://spec-empty.example.com/page"
URL_ERROR = "https://spec-error.example.com/page"
URL_BAD = "https://spec-z.example.com/page"

BODY_A = "SPEC-A. price: 100. warranty: 2 years."
BODY_B = "SPEC-B. price: 100. warranty: 2 years."
BODY_C = "SPEC-C. price: 120. warranty: 2 years."

# round 2: the price moves on A and B
BODY_A2 = "SPEC-A2. price: 150. warranty: 2 years."
BODY_B2 = "SPEC-B2. price: 150. warranty: 2 years."

VAL_AB = json.dumps({"price": "100", "warranty": "2 years"})
VAL_C = json.dumps({"price": "120", "warranty": "2 years"})
VAL_AB2 = json.dumps({"price": "150", "warranty": "2 years"})
VAL_SPLIT_A = json.dumps({"price": "100", "warranty": "2 years"})
VAL_SPLIT_B = json.dumps({"price": "120", "warranty": "2 years"})
VAL_EMPTY_WARRANTY = json.dumps({"price": "100", "warranty": ""})
VAL_MISSING_FIELD = json.dumps({"price": "100"})
VAL_LONG = json.dumps({"price": "x" * 300, "warranty": "2 years"})


def remock(vm, spec=None, bad_marker="SPEC-Z"):
    """spec: list of (url_marker, body_marker, body, llm_response).

    Markers must not be substrings of one another: mocks are first-match-wins,
    so "SPEC-B" would otherwise swallow "SPEC-BAD".
    """
    if spec is None:
        spec = [
            ("spec-a", "SPEC-A", BODY_A, VAL_AB),
            ("spec-b", "SPEC-B", BODY_B, VAL_AB),
            ("spec-c", "SPEC-C", BODY_C, VAL_C),
        ]
    vm.clear_mocks()
    for url_marker, body_marker, body, resp in spec:
        vm.mock_web(".*" + url_marker + ".*", {"method": "GET", "status": 200, "body": body})
        if resp is not None:
            vm.mock_llm(".*" + body_marker + ".*", resp)
    vm.mock_web(".*spec-empty.*", {"method": "GET", "status": 200, "body": ""})
    vm.mock_web(".*spec-error.*", {"method": "GET", "status": 500, "body": "Internal Server Error"})
    if bad_marker:
        vm.mock_web(".*spec-z.*", {"method": "GET", "status": 200, "body": "SPEC-Z. price: 100."})
        vm.mock_llm(".*SPEC-Z.*", VAL_MISSING_FIELD)


@pytest.fixture
def ledger(direct_vm, direct_deploy):
    vm = direct_vm
    remock(vm)
    c = direct_deploy(CONTRACT)
    vm.sender = OWNER
    return vm, c


def start(vm, c, fields=None, sources=None, min_sources=2, subject="Widget spec page"):
    vm.sender = OWNER
    return c.start_watch(
        subject,
        fields if fields is not None else list(FIELDS),
        sources if sources is not None else [URL_A, URL_B, URL_C],
        min_sources,
    )


def pull_all(vm, c, watch_id, indices=(0, 1, 2)):
    out = []
    for i in indices:
        out.append(c.pull(watch_id, i))
    return out


def pull_then_aggregate(vm, c, watch_id, indices=(0, 1, 2)):
    pull_all(vm, c, watch_id, indices)
    return c.aggregate(watch_id)
