from __future__ import annotations

import itertools

import pytest

from app.broker.state import (
    SESSION_TRANSITIONS,
    STATEMENT_TRANSITIONS,
    InvalidStateTransition,
    transition_session,
    transition_statement,
)
from app.domain.console_session import ConsoleSessionState, ConsoleStatementState


@pytest.mark.parametrize(
    ("source", "target"),
    sorted(SESSION_TRANSITIONS, key=lambda pair: (pair[0].value, pair[1].value)),
)
def test_every_documented_session_transition_is_accepted(
    source: ConsoleSessionState, target: ConsoleSessionState
) -> None:
    assert transition_session(source, target) is target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        pair
        for pair in itertools.product(ConsoleSessionState, repeat=2)
        if pair not in SESSION_TRANSITIONS and pair[0] is not pair[1]
    ],
)
def test_every_undocumented_session_transition_is_rejected(
    source: ConsoleSessionState, target: ConsoleSessionState
) -> None:
    with pytest.raises(InvalidStateTransition, match="session"):
        transition_session(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    sorted(STATEMENT_TRANSITIONS, key=lambda pair: (pair[0].value, pair[1].value)),
)
def test_every_documented_statement_transition_is_accepted(
    source: ConsoleStatementState, target: ConsoleStatementState
) -> None:
    assert transition_statement(source, target) is target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        pair
        for pair in itertools.product(ConsoleStatementState, repeat=2)
        if pair not in STATEMENT_TRANSITIONS and pair[0] is not pair[1]
    ],
)
def test_every_undocumented_statement_transition_is_rejected(
    source: ConsoleStatementState, target: ConsoleStatementState
) -> None:
    with pytest.raises(InvalidStateTransition, match="statement"):
        transition_statement(source, target)


def test_idempotent_state_reapplication_is_allowed_for_pg_convergence() -> None:
    assert (
        transition_session(ConsoleSessionState.IDLE, ConsoleSessionState.IDLE)
        is ConsoleSessionState.IDLE
    )
    assert (
        transition_statement(
            ConsoleStatementState.OUTCOME_UNKNOWN, ConsoleStatementState.OUTCOME_UNKNOWN
        )
        is ConsoleStatementState.OUTCOME_UNKNOWN
    )
