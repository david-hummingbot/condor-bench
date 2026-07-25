"""Grader must pass faithful candidates and fail each distinct error class."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))

from grade import GradeOptions, grade_fixture  # noqa: E402

H = 3600.0  # 1h fixture interval


def intent(ts, side="LONG", type_="position_perp", notional=1000.0, **extra):
    row = {
        "created_ts": ts,
        "type": type_,
        "side": side,
        "tag": "t",
        "notional_quote": notional,
        "entry_ts": ts,
        "close_ts": ts + 10 * H,
        "close_type": "take_profit",
        "order_type": "market",
        "leverage": 1,
        "take_profit_pct": 0.05,
        "stop_loss_pct": 0.02,
        "trailing_activation_pct": None,
        "trailing_delta_pct": None,
        "time_limit_s": None,
    }
    row.update(extra)
    return row


GOLDEN = [intent(0.0), intent(100 * H, side="SHORT"), intent(200 * H)]


def grade(candidate, golden=GOLDEN, **opts):
    return grade_fixture("fx", golden, candidate, H, GradeOptions(**opts))


def test_identical_resolves_perfectly():
    g = grade(list(GOLDEN))
    assert g.resolved and g.entry_f1 == 1.0 and not g.critical_failures
    assert g.exit_match_rate == 1.0 and g.close_type_agreement == 1.0


def test_timing_within_tolerance_resolves():
    shifted = [intent(i["created_ts"] + H, side=i["side"],
                      close_ts=i["close_ts"] + H) for i in GOLDEN]
    assert grade(shifted).resolved


def test_timing_beyond_tolerance_fails():
    shifted = [intent(i["created_ts"] + 5 * H, side=i["side"]) for i in GOLDEN]
    g = grade(shifted)
    assert not g.resolved and g.entry_f1 == 0.0


def test_flipped_side_fails():
    flipped = [intent(i["created_ts"], side="SHORT" if i["side"] == "LONG" else "LONG")
               for i in GOLDEN]
    assert grade(flipped).entry_f1 == 0.0


def test_wrong_executor_type_fails():
    wrong = [intent(i["created_ts"], side=i["side"], type_="order_perp") for i in GOLDEN]
    assert grade(wrong).entry_f1 == 0.0


def test_missing_stop_loss_is_critical_despite_perfect_f1():
    naked = [intent(i["created_ts"], side=i["side"], stop_loss_pct=None) for i in GOLDEN]
    g = grade(naked)
    assert g.entry_f1 == 1.0
    assert "missing_stop_loss_pct" in g.critical_failures and not g.resolved


def test_barrier_far_off_is_critical():
    off = [intent(i["created_ts"], side=i["side"], take_profit_pct=0.10) for i in GOLDEN]
    g = grade(off)
    assert "take_profit_pct_off" in g.critical_failures and not g.resolved


def test_extra_candidate_protection_is_not_a_failure():
    extra = [intent(i["created_ts"], side=i["side"], time_limit_s=86400.0) for i in GOLDEN]
    assert grade(extra).resolved


def test_sizing_off_is_critical():
    big = [intent(i["created_ts"], side=i["side"], notional=2000.0) for i in GOLDEN]
    g = grade(big)
    assert "sizing_off" in g.critical_failures and not g.resolved


def test_spurious_extra_entries_hurt_precision():
    noisy = list(GOLDEN) + [intent(50 * H), intent(150 * H), intent(250 * H)]
    g = grade(noisy)
    assert g.entry_precision == 0.5 and not g.resolved


def test_missed_entries_hurt_recall():
    assert not grade([intent(0.0)]).resolved


def test_flat_golden_and_flat_candidate_resolve():
    g = grade([], golden=[])
    assert g.resolved and g.entry_f1 == 1.0


def test_flat_golden_with_manufactured_trades_fails():
    g = grade([intent(0.0)], golden=[])
    assert not g.resolved and g.entry_precision == 0.0
