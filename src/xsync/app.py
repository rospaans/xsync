"""The hub: the overview of all projects, one project's view, and the loop that drives both."""

import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import __version__
from .config import STARTER_CONFIG, clean_path, find_root, load_registry, load_settings, save_registry, save_settings
from .output import Printer, clock, emit, paint, state, warn
from .project import Project
from . import update
from .ui import (
    BAR_DOT, BAR_TAG, BAR_TRANSPORT, KEY_HELP, OVERVIEW_HELP, OVERVIEW_KEYS, ROW_SELECTED, SETTINGS_HELP,
    SETTINGS_KEYS, Panel, Screen, segments_length, spread, styled,
)


class App:
    """The hub: an overview of all remembered projects, and one project's view at a time.

    Everything runs in one loop: keys are handled, then every project gets a
    tick. Projects with watch mode on therefore keep syncing whichever view is
    shown, and one long sync briefly holds up the rest.
    """

    def __init__(self, registry: Path, verbose: bool = False, screen: Screen | None = None):
        self.registry = registry
        self.screen = screen or Screen()
        self.pretty = not verbose
        self.view: Project | None = None  # None shows the overview
        self.selected = 0
        self.panel = Panel("", OVERVIEW_KEYS)
        self.notes: list[str] = []  # lines under the overview table, until the next key press
        self.settings_file = registry.parent / "settings.toml"
        self.settings = load_settings(self.settings_file)
        self.settings_panel = Panel("settings", SETTINGS_KEYS)
        self.in_settings = False  # the settings screen is shown (self.view is None meanwhile)
        self.settings_notes: list[str] = []  # lines under the settings, until the next key press
        self.latest: tuple[str | None, str] | None = None  # the version on GitHub, and when that was checked
        self.updated_to = ""  # the version installed by u; it is used from the next start
        self.update_note = ""  # set from a background thread when GitHub has a newer version
        self._update_found = False
        self._drawn_rows = 0
        self._list_height = 0  # rows of a project's list that fit on screen
        self._resized = False  # set by the resize signal; the redraw happens in the loop
        self.projects = [Project(root, self) for root in load_registry(registry)]
        self.sort()

    # --- projects

    def sort(self):
        """Keep the overview in alphabetical order, with the highlight on the same project."""
        highlighted = self.projects[self.selected] if self.selected < len(self.projects) else None
        self.projects.sort(key=lambda project: (project.name.lower(), str(project.root)))
        if highlighted is not None:
            self.selected = self.projects.index(highlighted)

    def register(self, root: Path) -> Project:
        """Remember a project (if it is new) and return it."""
        root = root.resolve()
        for project in self.projects:
            if project.root == root:
                return project
        project = Project(root, self)
        self.projects.append(project)
        self.sort()
        self.save()
        return project

    def remove(self, project: Project):
        """Forget a project. Its files, local and remote, are not touched."""
        project.close()
        self.projects.remove(project)
        self.selected = max(0, min(self.selected, len(self.projects) - 1))
        self.save()

    def save(self):
        save_registry(self.registry, [project.root for project in self.projects])

    def activate(self, project: Project):
        """Make a project the one whose rsync output and warnings are being recorded."""
        state.output = project
        state.pretty = self.pretty and project.gnu

    # --- drawing

    def show_line(self, project: Project, text: str):
        """A project added a line to its list; print it if the end of that list is on screen."""
        if self.view is not project:
            return
        if project.scroll:
            project.scroll += 1  # keep showing the same lines while scrolled back
            self.draw()
        else:
            print(text, flush=True)

    def while_waiting(self, project: Project, seconds: float) -> bool:
        """rsync is running for `project`: show for how long, allow scrolling, and cancel on Esc."""
        self.after_resize()
        base = project.panel.state.split("  ·  ")[0]
        if seconds >= 3:
            project.panel.state = f"{base}  ·  {int(seconds)}s, Esc cancels"
            self.refresh(project)
        cancel = False
        while (key := self.screen.read_key(0)) is not None:
            if key == "esc":
                cancel = True
            elif self.view is project and key in ("up", "down", "pageup", "pagedown", "home", "end"):
                self.on_project_key(key)
        return cancel

    def after_resize(self):
        """Redraw if the terminal changed size. Called from the loops, never from the signal handler:
        drawing there could land in the middle of another write and garble the screen."""
        if self._resized:
            self._resized = False
            self.draw(clear=True)

    def refresh(self, project: Project | None = None):
        """Redraw the fixed rows if what changed is visible."""
        if self.view is None or project is None or project is self.view:
            self.draw()

    def draw(self, clear: bool = False):
        if self.in_settings:
            self.draw_settings()
        elif self.view is None:
            self.draw_overview()
        else:
            self.draw_project(clear)

    def summarize(self):
        """Fill the overview's status bar from the state of all projects."""
        watching = sum(project.watching for project in self.projects)
        offline = sum(project.offline for project in self.projects)
        broken = sum(bool(project.error) for project in self.projects)
        count = len(self.projects)
        self.panel.name = f"{count} project" + ("" if count == 1 else "s")
        parts = [f"{watching} watching"] if watching else []
        parts += [f"{offline} offline"] if offline else []
        parts += [f"{broken} with a problem"] if broken else []
        self.panel.state = ", ".join(parts) or "nothing watching"
        self.panel.level = "problem" if offline or broken else ("ok" if watching else "busy")
        self.panel.mode = "offline" if offline else "idle"
        changes = sum(project.changes for project in self.projects)
        self.panel.detail = f"{changes} changes" if changes else ""
        self.panel.switches = {"v": not self.pretty}

    def table_rows(self, width: int) -> list[str]:
        """The overview: one row per project, the highlighted one marked."""
        if not self.projects:
            return [paint("  No projects yet. Press a to add this folder or another one.", "dim")]
        name_width = min(max(len(project.name) for project in self.projects), 28)
        rows = []
        for index, project in enumerate(self.projects):
            selected = index == self.selected
            left = [(" › " if selected else "   ", "1;97"), (project.name[:name_width].ljust(name_width), "1;97")]
            if project.remote:
                left += [("   ", ""), (f" {project.panel.transport.upper()} ", BAR_TRANSPORT),
                         (f" {project.remote}", "38;5;250")]
            right = [("●", BAR_DOT[project.panel.level]), (f" {project.panel.state}", "38;5;255")]
            if project.changes:
                right += [("   ", ""), (f" {project.changes} changes ", BAR_TAG)]
            right.append((" ", ""))
            if project.remote:  # too narrow: shorten the remote
                room = width - segments_length(left[:4]) - 2 - segments_length(right)
                if len(left[4][0]) > room:
                    left[4] = (left[4][0][:max(room - 1, 0)] + "…" if room > 0 else "", left[4][1])
            rows.append(styled(spread(left, right, width), ROW_SELECTED if selected else ""))
        return rows

    def draw_overview(self):
        size = self.screen.size()
        width = size.columns
        self._drawn_rows = 0  # the next project view starts from a cleared screen
        self.summarize()
        notes = [paint(f" {note}"[:max(width - 1, 0)], "dim") for note in self.notes]
        if self.update_note:
            notes.append(paint(f" {self.update_note}"[:max(width - 1, 0)], "yellow"))
        rows = (self.panel.header_rows(width) + self.table_rows(width) + [""] + notes)[:size.lines]
        fixed = "".join(f"\033[{row};1H\033[2K{text}" for row, text in enumerate(rows, 1))
        below = f"\033[{len(rows) + 1};1H\033[J" if len(rows) < size.lines else ""
        title = f"\033]0;xsync: {self.panel.state}\007"
        self.screen.write(f"\033[r{fixed}{below}{title}")

    def settings_rows(self) -> list[str]:
        """What the settings screen lists: versions, where xsync came from, and the update check."""
        folder = update.install_folder()
        if self.latest is None:
            on_github = "not checked yet; press c"
        elif self.latest[0] is None:
            on_github = f"could not reach GitHub at {self.latest[1]}"
        elif self.updated_to:
            on_github = f"{self.latest[0]}, installed; quit and start xsync again to use it"
        elif update.is_newer(self.latest[0]):
            on_github = f"{self.latest[0]}, newer than yours (checked {self.latest[1]})"
        else:
            on_github = f"{self.latest[0]}, you are up to date (checked {self.latest[1]})"
        at_start = "on" if self.settings["check_for_updates"] else "off"
        if update.check_disabled():
            at_start = f"off: {update.OPT_OUT} is set"
        rows = [
            ("version", f"xsync {__version__}"),
            ("installed", f"from {folder}" if folder else "not from a checkout, so it cannot update itself"),
            ("on GitHub", on_github),
            ("at start", f"check for a newer version: {at_start}"),
            ("settings", str(self.settings_file)),
        ]
        return [f"{label.ljust(10)} {value}" for label, value in rows]

    def update_available(self) -> bool:
        return self.latest is not None and update.is_newer(self.latest[0]) and not self.updated_to

    def draw_settings(self):
        size = self.screen.size()
        width = size.columns
        self._drawn_rows = 0
        panel = self.settings_panel
        newer = self.update_available()
        if self.updated_to:
            panel.state, panel.level = f"updated to {self.updated_to}, restart to use it", "ok"
        elif newer:
            panel.state, panel.level = f"update available: {self.latest[0]}", "busy"
        else:
            panel.state, panel.level = f"xsync {__version__}", "ok"
        panel.switches = {"a": self.settings["check_for_updates"] and not update.check_disabled()}
        panel.highlights = {"u"} if newer else set()
        body = [paint(f" {row}"[:max(width - 1, 0)], "dim") for row in self.settings_rows()]
        if newer:
            headline = f" ▲ Update available: xsync {self.latest[0]} (you have {__version__}). Press u to update."
            body = [paint(headline[:max(width - 1, 0)], "yellow"), ""] + body
        elif self.updated_to:
            headline = f" ✓ Updated to xsync {self.updated_to}. Quit (q) and start xsync again to use it."
            body = [paint(headline[:max(width - 1, 0)], "green"), ""] + body
        notes = [paint(f" {note}"[:max(width - 1, 0)], "dim") for note in self.settings_notes]
        rows = (panel.header_rows(width) + body + [""] + notes)[:size.lines]
        fixed = "".join(f"\033[{row};1H\033[2K{text}" for row, text in enumerate(rows, 1))
        below = f"\033[{len(rows) + 1};1H\033[J" if len(rows) < size.lines else ""
        self.screen.write(f"\033[r{fixed}{below}\033]0;xsync: settings\007")

    def draw_project(self, clear: bool = False):
        """The fixed rows of the project on screen; its list scrolls in the region below them."""
        project = self.view
        size = self.screen.size()
        rows = project.panel.header_rows(size.columns)
        if project.scroll:  # say so where the problem line normally is
            hint = f" ↑ scrolled back, {project.scroll} line(s) below   ↓ PgDn: forward   End: back to the newest"
            rows[-2] = paint(hint[:max(size.columns - 1, 0)], "yellow")
        # The header grows when the buttons wrap; start over rather than draw over the list
        clear = clear or len(rows) != self._drawn_rows
        self._drawn_rows = len(rows)
        top = len(rows) + 1
        self._list_height = max(size.lines - top, 0)  # the last row stays free for the next line
        # Setting the scroll region moves the cursor, so save and restore it
        start = "\033[2J" if clear else "\0337"
        end = "\0338"
        fixed = "".join(f"\033[{row};1H\033[2K{text}" for row, text in enumerate(rows, 1))
        title = f"\033]0;xsync {project.name}: {project.panel.state}\007"
        shown = ""
        if clear or project.scroll:
            # Draw the part of the list that belongs on screen: its end, or further back when scrolled
            last = max(len(project.log) - project.scroll, 0)
            window = list(project.log)[max(last - self._list_height, 0):last]
            shown = "".join(f"\033[{top + offset};1H\033[2K" for offset in range(self._list_height + 1))
            shown += "".join(f"\033[{top + offset};1H{line}" for offset, line in enumerate(window))
            if clear:  # new lines continue right below
                end = f"\033[{top + len(window)};1H"
        self.screen.write(f"{start}\033[{top};{size.lines}r{fixed}{shown}{end}{title}")

    def scroll_list(self, lines: int):
        """Scroll the list of the project on screen: positive goes back, negative forward."""
        project = self.view
        furthest = max(len(project.log) - self._list_height, 0)
        position = max(0, min(project.scroll + lines, furthest))
        if position != project.scroll:
            project.scroll = position
            self.draw(clear=position == 0)  # at the end again: follow new lines

    # --- input

    def ask(self, label: str, text: str = "") -> str | None:
        """Read a line of text on the message row, starting from `text`. Returns None if cancelled with Esc."""
        panel = self.view.panel if self.view else self.settings_panel if self.in_settings else self.panel
        previous = (panel.message, panel.message_color)
        while True:
            panel.message, panel.message_color = f"{label}: {text}▏   Enter to confirm, Esc to cancel", "bold"
            self.draw()
            key = self.screen.read_key(0.5)
            self.after_resize()
            if key is None:
                continue
            if key in ("\r", "\n"):
                break
            if key == "esc":
                text = None
                break
            if key in ("\x7f", "\b"):
                text = text[:-1]
            elif key == "\x15":  # Ctrl+U
                text = ""
            elif len(key) == 1 and key.isprintable():
                text += key
        panel.message, panel.message_color = previous
        self.draw()
        return text

    def open(self, project: Project):
        self.view = project
        self.selected = self.projects.index(project)
        self.notes = []
        project.scroll = 0
        self.draw(clear=True)

    def back(self):
        self.view = None
        self.in_settings = False
        self.draw()

    # --- settings

    def open_settings(self):
        self.in_settings = True
        self.notes = []
        self.settings_notes = []
        self.draw()

    def check_now(self):
        self.settings_notes = ["checking GitHub…"]
        self.draw()
        self.found_version(update.latest_version())
        self.settings_notes = [] if self.latest[0] else ["no answer from GitHub; is this computer online?"]

    def found_version(self, latest: str | None):
        """Remember what GitHub said; from the startup check's thread too, so nothing is drawn here."""
        self.latest = (latest, clock()[:5])
        self.update_note = "" if self.updated_to else update.update_note(latest) or ""

    def update_xsync(self):
        """Pull the checkout xsync was installed from and reinstall, once the user typed yes.

        Only when GitHub has a newer version: up to date, or GitHub out of reach, changes nothing.
        """
        if self.updated_to:
            self.settings_notes = [f"Already updated to {self.updated_to}. Quit (q) and start xsync again to use it."]
            return
        self.check_now()
        latest = self.latest[0]
        if latest is None:
            self.settings_notes = ["Could not reach GitHub, so it is not known whether there is a newer version. Nothing was changed."]
            return
        if not update.is_newer(latest):
            self.settings_notes = [f"xsync {__version__} is up to date; nothing to update."]
            return
        folder = update.install_folder()
        problem = update.why_not_update(folder)
        if problem:
            self.settings_notes = [f"Not updating: {problem}"]
            return
        answer = self.ask(f"update xsync {__version__} to {latest}: git pull and reinstall from {folder}? type yes")
        if (answer or "").strip().lower() != "yes":
            self.settings_notes = ["Not updated."]
            return
        self.settings_notes = [f"updating from {folder}…"]
        self.draw()
        ok, lines = update.run_update(folder)
        tail = lines[-12:]
        if ok:
            self.update_note = ""
            self.updated_to = latest
            self.settings_notes = tail
        else:
            self.settings_notes = tail + ["", "The update failed; nothing was changed in xsync as it runs now. See the lines above."]

    def on_settings_key(self, key: str):
        self.settings_notes = []
        if key in ("b", "esc", "left"):
            self.back()
            return
        if key == "c":
            self.check_now()
        elif key == "u":
            self.update_xsync()
        elif key == "a":
            self.settings["check_for_updates"] = not self.settings["check_for_updates"]
            save_settings(self.settings_file, self.settings)
        elif key == "h":
            self.settings_notes = [f"{keys.ljust(5)} {text}" for keys, text in SETTINGS_HELP.items()]
        self.draw()

    def toggle_verbose(self) -> str:
        """Switch between the summarized list and rsync's raw output, for all projects."""
        self.pretty = not self.pretty
        for project in self.projects:
            project.sync_switches()
        return "verbose off" if self.pretty else "verbose on: showing rsync's raw output"

    def add_project(self):
        """Add a folder as a project; a folder without a config gets a starter one to edit."""
        text = self.ask("project folder to add (type, paste or drag it here; . is the current folder)")
        if not text:
            return
        folder = Path(clean_path(text))
        if not folder.is_dir():
            self.notes = [f"Not a folder: {folder}"]
            return
        root = find_root(folder.resolve())
        created = root is None
        if created:
            root = folder.resolve()
            (root / ".xsync.toml").write_text(STARTER_CONFIG)
        project = self.register(root)
        self.selected = self.projects.index(project)
        if created:
            self.edit_config(project)
            self.notes.insert(0, f"Created {root / '.xsync.toml'}.")
        else:
            self.notes = [f"Added {project.name}."]

    def edit_config(self, project: Project):
        """Open the project's .xsync.toml in the user's editor, then read it again."""
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano")
        self.screen.suspend()
        try:
            subprocess.run([*editor, str(project.root / ".xsync.toml")])
            failed = ""
        except OSError as error:
            failed = f"Could not start the editor '{editor[0]}': {error}. Set $EDITOR to the one you use."
        finally:
            self.screen.resume()
            self._drawn_rows = 0  # the editor used the whole screen; start over
        project.reload()
        self.notes = [failed or f"{project.name}: {project.error or 'config reloaded'}"]
        self.draw(clear=True)

    def on_overview_key(self, key: str):
        self.notes = []
        project = self.projects[self.selected] if self.projects else None
        if key in ("up", "k"):
            self.selected = max(self.selected - 1, 0)
        elif key in ("down", "j"):
            self.selected = min(self.selected + 1, max(len(self.projects) - 1, 0))
        elif key == "a":
            self.add_project()
        elif key == "v":
            self.notes = [self.toggle_verbose()]
        elif key == "h":
            self.notes = [f"xsync {__version__}"] + [f"{keys.ljust(5)} {text}" for keys, text in OVERVIEW_HELP.items()]
        elif key == ",":
            self.open_settings()
            return
        elif project is None:
            pass
        elif key == "x":
            self.remove(project)
            self.notes = [f"Forgot {project.name}: it is off this list, and no file was touched. Add it back with a."]
        elif key == "e":
            self.edit_config(project)
            return
        elif key in ("o", "\r", "\n", "right"):
            self.open(project)
            return
        elif key in ("s", "c", "w"):
            if project.error:
                self.notes = [f"{project.name}: {project.error}"]
            else:
                project.touched = True
                if key == "s":
                    project.full_sync()
                elif key == "c":
                    project.compare()
                elif project.watching:
                    project.stop_watch()
                else:
                    project.start_watch()
                self.notes = [f"{project.name}: {project.panel.state}. Press o to see its list."]
        self.draw()

    def on_project_key(self, key: str):
        project = self.view
        if key in ("b", "esc", "left"):
            self.back()
            return
        page = max(self._list_height - 1, 1)
        scrolling = {"up": 1, "down": -1, "pageup": page, "pagedown": -page,
                     "home": len(project.log), "end": -len(project.log)}
        if key in scrolling:
            self.scroll_list(scrolling[key])
            return
        if project.scroll and key in KEY_HELP:  # anything else happens at the end of the list
            self.scroll_list(-len(project.log))
        self.activate(project)
        actions = {"s": project.full_sync, "c": project.compare, "i": project.show_status,
                   "r": lambda: project.check_connection(announce=True),
                   "w": lambda: project.stop_watch() if project.watching else project.start_watch()}
        if key == "h":
            project.show_help()
        elif key == "p":
            project.show_problems()
        elif key == "e":
            self.edit_config(project)
        elif key == "l":
            project.log.clear()
            project.scroll = 0
            self.draw(clear=True)
        elif key == "v":
            message = self.toggle_verbose()
            if self.pretty and not project.gnu:
                message = "the summarized output needs GNU rsync 3.x; this project stays verbose"
            emit(message, "dim")
            project.rest()
        elif key in actions or key == "f":
            if project.error:
                warn(project.error, "magenta")
            else:
                project.touched = True
                if key == "f":
                    text = self.ask("file or folder to sync (type, paste or drag it here)")
                    if text:
                        project.sync_one(text)
                else:
                    actions[key]()

    def check_for_update(self):
        """Look for a newer version on GitHub without holding up the screen; runs in its own thread."""
        self.found_version(update.latest_version())
        self._update_found = True  # the loop redraws; drawing from this thread could garble the screen

    def start_update_check(self) -> threading.Thread | None:
        """Check for a newer version in the background, unless that is switched off."""
        if not self.settings["check_for_updates"] or update.check_disabled():
            return None
        thread = threading.Thread(target=self.check_for_update, daemon=True)
        thread.start()
        return thread

    def run(self):
        """Show the overview and handle keys until quit."""
        self.start_update_check()
        try:
            with self.screen:
                if hasattr(signal, "SIGWINCH"):
                    signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "_resized", True))
                # Nothing runs until a key is pressed: no compare, no sync, no connection
                self.draw()
                while True:
                    key = self.screen.read_key(0.2)
                    self.after_resize()
                    if self._update_found:
                        self._update_found = False
                        if self.view is None:
                            self.draw()  # the overview or the settings, both show it
                    if key == "q":
                        break
                    if key is not None:
                        if self.in_settings:
                            self.on_settings_key(key)
                        elif self.view is None:
                            self.on_overview_key(key)
                        else:
                            self.on_project_key(key)
                    now = time.monotonic()
                    for project in list(self.projects):
                        project.tick(now)
        except KeyboardInterrupt:
            pass
        finally:
            state.output = Printer()
            for project in self.projects:
                project.close()
