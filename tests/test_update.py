"""Newer versions on GitHub: the check, the settings screen, and updating from a checkout."""

import subprocess
from pathlib import Path

import pytest

from xsync import app as app_module, config as config_module, update as update_module


@pytest.mark.parametrize("newer, older", [("0.3.0", "0.2.0"), ("0.10.0", "0.9.9"), ("1.0", "0.99.1"), ("0.2.1", "0.2.0")])
def test_versions_compare_as_numbers(newer, older):
    assert update_module.version_tuple(newer) > update_module.version_tuple(older)


@pytest.fixture
def github(monkeypatch, tmp_path):
    """Pretend GitHub's main branch has the version this returns, and xsync was installed from a checkout."""
    monkeypatch.setattr(update_module, "__version__", "0.2.0")
    monkeypatch.setattr(app_module, "__version__", "0.2.0")
    monkeypatch.setattr(update_module, "install_folder", lambda: "/opt/xsync-checkout")
    latest = {"version": None}
    monkeypatch.setattr(update_module, "latest_version", lambda: latest["version"])
    return latest


def test_a_newer_version_is_announced(github):
    note = update_module.update_note("0.3.0")
    assert "xsync 0.3.0 is available (you have 0.2.0)" in note
    assert "cd /opt/xsync-checkout && git pull && bash install.sh" in note


@pytest.mark.parametrize("latest", ["0.2.0", "0.1.9", None])
def test_nothing_is_said_when_up_to_date_or_offline(github, latest):
    assert update_module.update_note(latest) is None


def test_the_version_is_read_from_the_init_file(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def read(self, size): return b'"""xsync"""\n\n__version__ = "0.4.1"\n'

    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda url, timeout: Response())
    assert update_module.latest_version() == "0.4.1"

    def offline(url, timeout):
        raise OSError("no network")

    monkeypatch.setattr(update_module.urllib.request, "urlopen", offline)
    assert update_module.latest_version() is None


def test_the_startup_check_shows_a_note_in_the_overview(hub, github):
    github["version"] = "0.3.0"
    hub.check_for_update()
    hub.draw()
    assert "xsync 0.3.0 is available" in hub.screen.written[-1]


def test_the_startup_check_follows_the_setting(hub, github, monkeypatch):
    github["version"] = "0.3.0"
    hub.settings["check_for_updates"] = False
    assert hub.start_update_check() is None                 # switched off in the settings
    hub.settings["check_for_updates"] = True
    assert hub.start_update_check() is None                 # the tests set XSYNC_NO_UPDATE_CHECK
    monkeypatch.delenv(update_module.OPT_OUT)
    hub.start_update_check().join()
    assert "0.3.0 is available" in hub.update_note


# --- the settings screen

def test_settings_open_with_comma_and_close_with_b(hub, github):
    hub.on_overview_key(",")
    assert hub.in_settings
    assert "Check for updates" in hub.screen.written[-1] and "xsync 0.2.0" in hub.screen.written[-1]
    hub.on_settings_key("b")
    assert not hub.in_settings and hub.view is None


def test_check_now_says_what_github_has(hub, github):
    hub.open_settings()
    github["version"] = "0.3.0"
    hub.on_settings_key("c")
    assert "0.3.0, newer than yours" in hub.screen.written[-1]
    assert "update available: 0.3.0" in hub.screen.written[-1]
    github["version"] = None
    hub.on_settings_key("c")
    assert "could not reach GitHub" in hub.screen.written[-1]
    github["version"] = "0.2.0"
    hub.on_settings_key("c")
    assert "✓ xsync 0.2.0 is up to date." in hub.screen.written[-1]   # says so, not only in the list
    assert hub.settings_notes == [("✓ xsync 0.2.0 is up to date.", "green")]


def test_the_startup_check_can_be_switched_off_and_is_remembered(hub, monkeypatch):
    monkeypatch.delenv(update_module.OPT_OUT)
    hub.open_settings()
    assert hub.settings["check_for_updates"]
    hub.on_settings_key("a")
    assert not hub.settings["check_for_updates"]
    assert config_module.load_settings(hub.settings_file) == {"check_for_updates": False}
    hub.on_settings_key("a")
    assert config_module.load_settings(hub.settings_file) == {"check_for_updates": True}


def test_settings_survive_a_broken_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("check_for_updates = [broken")
    assert config_module.load_settings(path) == config_module.DEFAULT_SETTINGS
    path.write_text('check_for_updates = "sometimes"\n')
    assert config_module.load_settings(path) == config_module.DEFAULT_SETTINGS


def test_an_available_update_stands_out(hub, github):
    github["version"] = "0.3.0"
    hub.open_settings()
    hub.on_settings_key("c")
    screen = hub.screen.written[-1]
    assert "Update available: xsync 0.3.0 (you have 0.2.0). Press u to update." in screen
    assert hub.settings_panel.highlights == {"u"}
    github["version"] = "0.2.0"
    hub.on_settings_key("c")
    assert "Update available" not in hub.screen.written[-1] and not hub.settings_panel.highlights


def test_update_needs_a_typed_yes(hub, github, monkeypatch):
    github["version"] = "0.3.0"
    ran = []
    monkeypatch.setattr(update_module, "why_not_update", lambda folder: None)
    monkeypatch.setattr(update_module, "run_update", lambda folder: ran.append(folder) or (True, ["done"]))
    hub.open_settings()
    for answer in (None, "", "y", "no"):                     # Esc, Enter, and anything but yes
        monkeypatch.setattr(hub, "ask", lambda label, text="": answer)
        hub.on_settings_key("u")
        assert ran == [] and "Not updated" in hub.screen.written[-1]
    monkeypatch.setattr(hub, "ask", lambda label, text="": "yes")
    hub.on_settings_key("u")
    assert ran == ["/opt/xsync-checkout"]
    assert "Updated to xsync 0.3.0. Quit (q) and start xsync again" in hub.screen.written[-1]
    assert not hub.settings_panel.highlights


def test_update_does_nothing_when_up_to_date_or_offline(hub, github, monkeypatch):
    monkeypatch.setattr(update_module, "run_update", lambda folder: pytest.fail("pulled without a newer version"))
    monkeypatch.setattr(hub, "ask", lambda label, text="": pytest.fail("asked without a newer version"))
    hub.open_settings()
    github["version"] = "0.2.0"
    hub.on_settings_key("u")
    assert "✓ xsync 0.2.0 is up to date; nothing to update." in hub.screen.written[-1]
    github["version"] = None
    hub.on_settings_key("u")
    assert "Could not reach GitHub" in hub.screen.written[-1]


def test_update_is_not_run_twice(hub, github, monkeypatch):
    """After an update, this xsync still runs the old version; u says to restart instead of pulling again."""
    github["version"] = "0.3.0"
    ran = []
    monkeypatch.setattr(update_module, "why_not_update", lambda folder: None)
    monkeypatch.setattr(update_module, "run_update", lambda folder: ran.append(folder) or (True, ["done"]))
    monkeypatch.setattr(hub, "ask", lambda label, text="": "yes")
    hub.open_settings()
    hub.on_settings_key("u")
    hub.on_settings_key("u")
    assert len(ran) == 1
    assert "Already updated to 0.3.0" in hub.screen.written[-1]
    hub.on_settings_key("c")                                 # checking again does not bring the note back
    assert hub.update_note == ""


def test_update_refuses_when_it_is_not_safe(hub, github, monkeypatch):
    github["version"] = "0.3.0"
    monkeypatch.setattr(update_module, "why_not_update", lambda folder: "it has uncommitted changes")
    monkeypatch.setattr(hub, "ask", lambda label, text="": pytest.fail("asked although it cannot update"))
    hub.open_settings()
    hub.on_settings_key("u")
    assert "Not updating: it has uncommitted changes" in hub.screen.written[-1]


# --- updating a real checkout

def git(folder: Path, *args: str):
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    """A clone of a stand-in for the GitHub repository, on main, with an install.sh that leaves a mark."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "install.sh").write_text('echo "installed $(cat version)" > installed.txt\necho Installed\n')
    (origin / "version").write_text("0.2.0")
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "first")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    (clone / ".gitignore").write_text("installed.txt\n")
    return origin, clone


def test_updating_pulls_and_installs(checkout):
    origin, clone = checkout
    (origin / "version").write_text("0.3.0")
    git(origin, "commit", "-q", "-am", "new version")
    assert update_module.why_not_update(str(clone)) is None
    ok, lines = update_module.run_update(str(clone))
    assert ok, lines
    assert (clone / "installed.txt").read_text().strip() == "installed 0.3.0"
    assert "Installed" in lines


def test_updating_refuses_local_changes_and_other_branches(checkout):
    _, clone = checkout
    (clone / "version").write_text("my own edit")
    assert "uncommitted changes" in update_module.why_not_update(str(clone))
    git(clone, "checkout", "-q", "version")
    git(clone, "checkout", "-q", "-b", "my-feature")
    assert "branch 'my-feature'" in update_module.why_not_update(str(clone))
    assert "not installed from a checkout" in update_module.why_not_update(None)


def test_a_failed_pull_stops_before_installing(checkout):
    origin, clone = checkout
    (origin / "version").write_text("0.3.0")
    git(origin, "commit", "-q", "-am", "upstream")
    (clone / "version").write_text("diverged")
    git(clone, "commit", "-q", "-am", "local commit")       # history differs: no fast-forward
    ok, lines = update_module.run_update(str(clone))
    assert not ok and not (clone / "installed.txt").exists()
