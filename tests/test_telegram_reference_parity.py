"""Guard the copied Telegram implementation against accidental local drift."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_telegram_channel_files_match_pinned_reference_source() -> None:
    for name in (
        "base.py",
        "contract.py",
        "reply_context.py",
        "telegram_channel.py",
        "telegram_utils.py",
    ):
        assert (ROOT / "infra/channels" / name).read_bytes() == (
            ROOT / "Reference/infra/channels" / name
        ).read_bytes()


def test_telegram_reference_pin_matches_checkout() -> None:
    expected = (
        ROOT / "kirakira_agent/channels/REFERENCE_PIN"
    ).read_text(encoding="utf-8").strip()
    actual = __import__("subprocess").check_output(
        ["git", "-C", str(ROOT / "Reference"), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    assert actual == expected


def test_supervisor_matches_pinned_reference_source() -> None:
    assert (ROOT / "agent/supervisor.py").read_bytes() == (
        ROOT / "Reference/agent/supervisor.py"
    ).read_bytes()
