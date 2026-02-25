#!/usr/bin/env python3
"""OpenSpace Lifecycle Manager dashboard-first Textual UI."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from rich.text import Text
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, RichLog, Static


# -----------------------------
# Config
# -----------------------------
OPENSPACE_ROOT = Path(os.environ.get("OPENSPACE_ROOT", "/opt/openspace"))
LCM_TASKS_DIR = OPENSPACE_ROOT / "automation" / "lifecycle-manager" / "tasks"
INIT_PLAYBOOK = LCM_TASKS_DIR / "init.yml"
VALIDATE_PLAYBOOK = LCM_TASKS_DIR / "validate.yml"
STATUS_PLAYBOOK = LCM_TASKS_DIR / "status.yml"  # optional
CURRENT_ENV_LINK = OPENSPACE_ROOT / "env" / "current"
TASK_NS = "automation"


# -----------------------------
# Dashboard model
# -----------------------------
@dataclass
class DashboardSection:
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class DashboardModel:
    generated_at: str
    last_action: str
    sections: list[DashboardSection]


# -----------------------------
# Helpers
# -----------------------------
def safe_run(cmd: list[str], cwd: Optional[Path] = None) -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("ANSIBLE_FORCE_COLOR", "1")
    env.setdefault("PY_COLORS", "1")
    env.setdefault("TERM", "xterm-256color")

    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )


def list_envs(root: Path) -> list[str]:
    env_dir = root / "env"
    if not env_dir.exists():
        return []
    names: list[str] = []
    for entry in env_dir.iterdir():
        if not entry.is_dir() or entry.name == "current":
            continue
        if (entry / "Taskfile.yml").exists():
            names.append(entry.name)
    return sorted(names)


def get_current_env() -> Optional[str]:
    try:
        if CURRENT_ENV_LINK.is_symlink():
            return CURRENT_ENV_LINK.resolve().name
    except Exception:
        return None
    return None


def set_current_env(name: str) -> None:
    target = OPENSPACE_ROOT / "env" / name
    if not target.exists():
        raise FileNotFoundError(f"Env not found: {target}")

    try:
        if CURRENT_ENV_LINK.exists() or CURRENT_ENV_LINK.is_symlink():
            CURRENT_ENV_LINK.unlink(missing_ok=True)
        CURRENT_ENV_LINK.symlink_to(target)
        return
    except PermissionError:
        pass

    subprocess.check_call(["sudo", "ln", "-sfn", str(target), str(CURRENT_ENV_LINK)])


def resolve_env_taskfile(name: str) -> Path:
    tf = OPENSPACE_ROOT / "env" / name / "Taskfile.yml"
    if not tf.exists():
        raise FileNotFoundError(f"Taskfile missing: {tf}")
    return tf


def task_list(taskfile: Path) -> list[str]:
    p = subprocess.run(["task", "-t", str(taskfile), "--list"], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "task --list failed")

    names: list[str] = []
    for line in p.stdout.splitlines():
        tokenized = line.strip().replace("*", " ").replace("-", " ").split()
        for tok in tokenized:
            if ":" in tok:
                tok = tok.rstrip(":,")
                if tok.startswith(f"{TASK_NS}:"):
                    tok = tok[len(TASK_NS) + 1 :]
                names.append(tok)
                break

    seen = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def filter_tasks_by_prefix(tasks: Iterable[str], prefix: str) -> list[str]:
    return [t for t in tasks if t.startswith(prefix)]


def gather_dashboard_data(current_env: str, last_action: str) -> DashboardModel:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    sections = [
        DashboardSection(
            "System Overview",
            [
                f"Platform status: {'READY' if current_env != '<not set>' else 'NEEDS ENV'}",
                "Lifecycle manager: online (placeholder)",
                f"Last action: {last_action}",
                f"Snapshot time: {now}",
            ],
        ),
        DashboardSection(
            "Clusters",
            [
                "MCM: unknown (placeholder)",
                "OSMS: unknown (placeholder)",
                "OSDC: unknown (placeholder)",
            ],
        ),
        DashboardSection(
            "Infrastructure",
            [
                "Network: not yet wired",
                "Storage: not yet wired",
                "Registry: not yet wired",
            ],
        ),
        DashboardSection(
            "Security & Compliance",
            [
                "STIG: not yet wired",
                "FIPS: not yet wired",
                "Audit logging: not yet wired",
            ],
        ),
        DashboardSection(
            "Artifacts",
            [
                "RPMs: unknown (placeholder)",
                "Images: unknown (placeholder)",
                "Backups: unknown (placeholder)",
            ],
        ),
    ]
    return DashboardModel(generated_at=now, last_action=last_action, sections=sections)


# -----------------------------
# Runner
# -----------------------------
@dataclass
class RunSpec:
    title: str
    cmd: list[str]


class Runner:
    def __init__(self, on_line: Callable[[str], None]):
        self.on_line = on_line
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def run(self, spec: RunSpec, on_done: Optional[Callable[[int], None]] = None) -> None:
        with self._lock:
            if self._busy:
                raise RuntimeError("Runner busy")
            self._busy = True

        def _worker() -> None:
            rc = 1
            try:
                self.on_line(f"\n=== {spec.title} ===\n$ {shlex.join(spec.cmd)}")
                proc = safe_run(spec.cmd)
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.on_line(line.rstrip("\n"))
                rc = proc.wait()
                self.on_line(f"[exit {rc}] {spec.title}")
            except Exception as ex:
                self.on_line(f"[error] {ex}")
            finally:
                with self._lock:
                    self._busy = False
                if on_done:
                    on_done(rc)

        threading.Thread(target=_worker, daemon=True).start()


# -----------------------------
# Modal prompts
# -----------------------------
class PathPrompt(ModalScreen[Optional[str]]):
    def __init__(self, title: str, placeholder: str = "/path/to/deployment.yml"):
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._title),
            Input(placeholder=self._placeholder, id="path_input"),
            Horizontal(Button("Cancel", id="path_cancel"), Button("OK", variant="primary", id="path_ok")),
            id="path_root",
        )

    def on_mount(self) -> None:
        self.query_one("#path_input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "path_cancel":
            self.dismiss(None)
        elif event.button.id == "path_ok":
            value = self.query_one("#path_input", Input).value.strip()
            self.dismiss(value if value else None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ChoiceScreen(ModalScreen[Optional[str]]):
    def __init__(self, title: str, choices: list[str], run_label: str = "Select"):
        super().__init__()
        self._title = title
        self._choices = choices
        self._run_label = run_label

    def compose(self) -> ComposeResult:
        lv = ListView(*[ListItem(Label(c)) for c in self._choices], id="choice_list")
        yield Vertical(
            Label(self._title),
            lv,
            Horizontal(Button("Back", id="choice_back"), Button(self._run_label, variant="primary", id="choice_ok")),
            id="choice_root",
        )

    def on_mount(self) -> None:
        self.query_one("#choice_list", ListView).focus()

    def _selected(self) -> Optional[str]:
        lv = self.query_one("#choice_list", ListView)
        if lv.index is None or lv.index < 0 or lv.index >= len(self._choices):
            return None
        return self._choices[lv.index]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "choice_back":
            self.dismiss(None)
        elif event.button.id == "choice_ok":
            self.dismiss(self._selected())

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "enter":
            self.dismiss(self._selected())


# -----------------------------
# Drill-down screens
# -----------------------------
class InfoScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Vertical(Label(self._title), Static(self._body), Button("Back", id="info_back"), id="info_root")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "info_back":
            self.app.pop_screen()


# -----------------------------
# App
# -----------------------------
class OpenSpaceTUI(App):
    CSS = """
    #topbar { height: 3; border: round #666; padding: 0 1; }
    #body { height: 1fr; }
    #dashboard_panel { width: 55%; border: round #666; padding: 1; }
    #log_panel { width: 45%; border: round #666; padding: 1; }
    #dashboard_text { height: 1fr; border: round #444; padding: 1; }
    #admin_list { height: 1fr; border: round #444; padding: 0 1; }
    #log { height: 1fr; border: round #444; padding: 1; }
    #quick_actions { height: 3; border: round #666; padding: 0 1; }
    #path_root, #choice_root { border: round #666; padding: 1; width: 90%; height: 90%; margin: 1; }
    """

    TITLE = "OpenSpace Lifecycle Manager"

    current_env: reactive[str] = reactive("<not set>")
    last_action: reactive[str] = reactive("startup")
    current_pane: reactive[str] = reactive("status")

    BINDINGS = [
        ("a", "admin", "Admin"),
        ("s", "status", "Status"),
        ("g", "debug", "Debug"),
        ("d", "docs", "Docs"),
        ("i", "admin", "Admin"),
        ("r", "refresh", "Refresh"),
        ("b", "back", "Back"),
        ("c", "clear_log", "Clear log"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.runner = Runner(self._append_log)
        self._ui_thread_id: int | None = None
        self._env_names: list[str] = []
        self._admin_stage = "menu"
        self._admin_options = ["Deploy", "Init / Import", "Validate", "Destroy", "Upgrade", "Repair"]
        self._admin_index = 0
        self._admin_deploy_groups = ["Reference architecture", "Cluster"]
        self._admin_deploy_group_index = 0
        self._admin_task_prefix = ""
        self._admin_tasks: list[str] = []
        self._admin_task_index = 0
        self._admin_display_options: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="dashboard_panel"):
                yield Label("Status", id="left_title")
                yield Static("", id="dashboard_text")
                yield ListView(id="admin_list")
            with Vertical(id="log_panel"):
                yield Label("Execution Log")
                yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        with Horizontal(id="quick_actions"):
            yield Button("Admin (A)", id="btn_admin", variant="primary")
            yield Button("Status (S)", id="btn_status")
            yield Button("Debug (G)", id="btn_debug")
            yield Button("Docs (D)", id="btn_docs")
            yield Button("Gather Status", id="btn_gather")
            yield Button("Back (B)", id="btn_back")
            yield Button("Clear Log (C)", id="btn_clear")
        yield Footer()

    def on_mount(self) -> None:
        self._ui_thread_id = threading.get_ident()
        self.run_worker(self._startup_flow(), exclusive=True)

    async def _startup_flow(self) -> None:
        self._refresh_envs()
        await self._resolve_startup_environment()
        self._refresh_dashboard("startup gather")
        self.action_status()

    async def _resolve_startup_environment(self) -> None:
        current = get_current_env()
        if current:
            self.current_env = current
            self._update_topbar()
            return

        if len(self._env_names) == 1:
            set_current_env(self._env_names[0])
            self.current_env = self._env_names[0]
            self._append_log(f"Auto-selected only environment: {self.current_env}")
            self._update_topbar()
            return

        if len(self._env_names) > 1:
            selected = await self.push_screen_wait(ChoiceScreen("Select environment", self._env_names, "Use"))
            if selected:
                set_current_env(selected)
                self.current_env = selected
                self._append_log(f"Selected environment: {self.current_env}")

        self._update_topbar()

    def _update_topbar(self) -> None:
        text = f"OpenSpace Lifecycle Manager{' ' * 8}Env: {self.current_env}"
        self.query_one("#topbar", Static).update(text)

    def _refresh_dashboard(self, reason: str) -> None:
        self.last_action = reason
        model = gather_dashboard_data(self.current_env, self.last_action)
        lines: list[str] = []
        for section in model.sections:
            lines.append(section.title)
            lines.append("-" * len(section.title))
            lines.extend(section.lines)
            lines.append("")
        self._update_topbar()
        self._render_left_pane()

    def _set_left_title(self, text: str) -> None:
        self.query_one("#left_title", Label).update(text)

    def _show_admin_list(self, show: bool) -> None:
        self.query_one("#admin_list", ListView).display = show
        self.query_one("#dashboard_text", Static).display = not show

    def _refresh_admin_list(self) -> None:
        lv = self.query_one("#admin_list", ListView)
        lv.clear()
        for item in self._admin_display_options:
            lv.append(ListItem(Label(item)))
        if self._admin_display_options:
            lv.index = 0
        lv.focus()

    def _render_left_pane(self) -> None:
        if self.current_pane == "status":
            self._show_admin_list(False)
            self._render_status_pane()
        elif self.current_pane == "admin":
            self._show_admin_list(True)
            self._render_admin_pane()
        elif self.current_pane == "debug":
            self._show_admin_list(False)
            self._render_debug_pane()
        elif self.current_pane == "docs":
            self._show_admin_list(False)
            self._render_docs_pane()
        elif self.current_pane == "init":
            self._show_admin_list(False)
            self._render_init_pane()

    def _render_status_pane(self) -> None:
        self._set_left_title("Status")
        model = gather_dashboard_data(self.current_env, self.last_action)
        lines: list[str] = ["STATUS OVERVIEW", "===============", ""]
        for section in model.sections:
            lines.append(section.title)
            lines.append("-" * len(section.title))
            lines.extend(section.lines)
            lines.append("")

        lines.extend(
            [
                "Reachability (placeholder checks)",
                "-------------------------------",
                "Nodes reachable: unknown (wire SSH/ping checks)",
                "Harbor reachable: unknown",
                "Rancher reachable: unknown",
                "OpsCenter reachable: unknown",
                "Gitea reachable: unknown",
                "ArgoCD reachable: unknown",
            ]
        )
        self.query_one("#dashboard_text", Static).update("\n".join(lines))

    def _render_admin_pane(self) -> None:
        self._set_left_title("Admin")

        if self._admin_stage == "menu":
            self._admin_display_options = list(self._admin_options)
        elif self._admin_stage == "deploy_group":
            self._admin_display_options = list(self._admin_deploy_groups)
        elif self._admin_stage == "deploy_task":
            self._admin_display_options = list(self._admin_tasks)
        else:
            self._admin_display_options = []

        self._refresh_admin_list()

    def _render_debug_pane(self) -> None:
        self._set_left_title("Debug")
        self.query_one("#dashboard_text", Static).update(
            "DEBUG\n=====\n\n"
            "Diagnostics placeholder.\n"
            "- journald slices (future)\n"
            "- k8s/rke2 state (future)\n"
            "- last task traces (future)"
        )

    def _render_docs_pane(self) -> None:
        self._set_left_title("Docs")
        self.query_one("#dashboard_text", Static).update(
            "DOCS\n====\n\n"
            "Local documentation paths:\n"
            "- /opt/openspace/README.md\n"
            "- /opt/openspace/docs/ (if present)\n\n"
            "Future: searchable docs index."
        )

    def _render_init_pane(self) -> None:
        self._set_left_title("Init / Import")
        self.query_one("#dashboard_text", Static).update(
            "INIT / IMPORT\n=============\n\n"
            "Import workflow:\n"
            "1) Prompt for deployment.yml\n"
            "2) ansible-inventory preflight\n"
            "3) optional validate.yml\n"
            "4) init.yml\n\n"
            "Choose Init / Import from Admin and press Enter to start."
        )

    def _append_log(self, line: str) -> None:
        def _do() -> None:
            self.query_one("#log", RichLog).write(Text.from_ansi(line))

        if threading.get_ident() == self._ui_thread_id:
            _do()
        else:
            self.call_from_thread(_do)

    def _refresh_envs(self) -> None:
        self._env_names = list_envs(OPENSPACE_ROOT)

    def _ensure_env_selected(self) -> bool:
        if self.current_env != "<not set>":
            return True
        self._append_log("[warn] No environment selected.")
        return False

    def action_refresh(self) -> None:
        self._refresh_envs()
        current = get_current_env()
        self.current_env = current or "<not set>"
        self._refresh_dashboard("manual refresh")

    def action_admin(self) -> None:
        self.current_pane = "admin"
        self._admin_stage = "menu"
        self._render_left_pane()

    def action_admin_next(self) -> None:
        if self.current_pane != "admin":
            return
        lv = self.query_one("#admin_list", ListView)
        if lv.index is None:
            lv.index = 0
            return
        if self._admin_display_options:
            lv.index = (lv.index + 1) % len(self._admin_display_options)

    def action_admin_prev(self) -> None:
        if self.current_pane != "admin":
            return
        lv = self.query_one("#admin_list", ListView)
        if lv.index is None:
            lv.index = 0
            return
        if self._admin_display_options:
            lv.index = (lv.index - 1) % len(self._admin_display_options)

    def action_admin_select(self) -> None:
        if self.current_pane != "admin":
            return

        lv = self.query_one("#admin_list", ListView)
        selected_index = lv.index if lv.index is not None else 0

        if self._admin_stage == "menu":
            if not self._admin_options:
                return
            self._admin_index = max(0, min(selected_index, len(self._admin_options) - 1))
            selected = self._admin_options[self._admin_index]
            if selected == "Deploy":
                self._admin_stage = "deploy_group"
                self._admin_deploy_group_index = 0
                self._render_left_pane()
            elif selected == "Init / Import":
                self.action_init_import()
            elif selected == "Validate":
                self.action_validate()
            else:
                self._append_log(f"[info] {selected} action is a placeholder for now.")
                self._refresh_dashboard(f"admin: {selected.lower()}")
            return

        if self._admin_stage == "deploy_group":
            if not self._admin_deploy_groups:
                return
            self._admin_deploy_group_index = max(0, min(selected_index, len(self._admin_deploy_groups) - 1))
            group = self._admin_deploy_groups[self._admin_deploy_group_index]
            self._admin_task_prefix = "ref:" if group.startswith("Reference") else "cluster:"
            if not self._ensure_env_selected():
                self._admin_stage = "menu"
                self._render_left_pane()
                return
            try:
                tf = resolve_env_taskfile(self.current_env)
                tasks = task_list(tf)
                self._admin_tasks = [
                    t.replace(self._admin_task_prefix, "", 1)
                    for t in filter_tasks_by_prefix(tasks, self._admin_task_prefix)
                ]
            except Exception as ex:
                self._append_log(f"[error] {ex}")
                self._admin_stage = "menu"
                self._render_left_pane()
                return

            if not self._admin_tasks:
                self._append_log(f"No {self._admin_task_prefix}* tasks found.")
                self._admin_stage = "menu"
                self._render_left_pane()
                return

            self._admin_stage = "deploy_task"
            self._admin_task_index = 0
            self._render_left_pane()
            return

        if self._admin_stage == "deploy_task":
            if self.runner.busy:
                self._append_log("[warn] Busy running a command.")
                return
            if not self._ensure_env_selected() or not self._admin_tasks:
                return

            self._admin_task_index = max(0, min(selected_index, len(self._admin_tasks) - 1))
            chosen = self._admin_tasks[self._admin_task_index]
            tf = resolve_env_taskfile(self.current_env)
            full = f"{TASK_NS}:{self._admin_task_prefix}{chosen}"
            self.runner.run(
                RunSpec(f"Deploy: {full}", ["task", "-t", str(tf), full]),
                on_done=lambda _rc: self._refresh_dashboard(f"deploy {self._admin_task_prefix}{chosen}"),
            )
            self._admin_stage = "menu"
            self._render_left_pane()

    def action_back(self) -> None:
        if self.current_pane == "admin":
            if self._admin_stage == "deploy_task":
                self._admin_stage = "deploy_group"
            elif self._admin_stage == "deploy_group":
                self._admin_stage = "menu"
            else:
                self.current_pane = "status"
            self._render_left_pane()
            return

        if self.current_pane == "init":
            self.current_pane = "admin"
            self._admin_stage = "menu"
            self._render_left_pane()
            return

        if self.current_pane in {"debug", "docs"}:
            self.current_pane = "status"
            self._render_left_pane()
            return

        self.action_status()

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_status(self) -> None:
        self.current_pane = "status"
        self._render_left_pane()

    def action_debug(self) -> None:
        self.current_pane = "debug"
        self._render_left_pane()

    def action_docs(self) -> None:
        self.current_pane = "docs"
        self._render_left_pane()

    def action_gather_status(self) -> None:
        self.current_pane = "status"
        self._render_left_pane()

        if self.runner.busy:
            self._append_log("[warn] Busy running a command.")
            return
        if not self._ensure_env_selected():
            return

        if not STATUS_PLAYBOOK.exists():
            self._append_log(f"[warn] status.yml missing: {STATUS_PLAYBOOK} (using placeholders in status pane)")
            return

        env_root = OPENSPACE_ROOT / "env" / self.current_env
        default_inventory = env_root / "deployment.yml"

        if default_inventory.exists():
            inventory = default_inventory
            self.runner.run(
                RunSpec("Gather status", ["ansible-playbook", "-i", str(inventory), str(STATUS_PLAYBOOK)]),
                on_done=lambda _rc: self._refresh_dashboard("status gather"),
            )
            return

        self.run_worker(self._gather_status_prompt_flow(), exclusive=True)

    async def _gather_status_prompt_flow(self) -> None:
        path = await self.push_screen_wait(PathPrompt("Status inventory path", "/path/to/deployment.yml"))
        if not path:
            return

        inventory = Path(os.path.expanduser(path)).resolve()
        if not inventory.exists():
            self._append_log(f"[error] File not found: {inventory}")
            return

        self.runner.run(
            RunSpec("Gather status", ["ansible-playbook", "-i", str(inventory), str(STATUS_PLAYBOOK)]),
            on_done=lambda _rc: self._refresh_dashboard("status gather"),
        )

    def action_init_import(self) -> None:
        self.current_pane = "init"
        self._render_left_pane()
        if self.runner.busy:
            self._append_log("[warn] Busy running a command.")
            return
        if not INIT_PLAYBOOK.exists():
            self._append_log(f"[error] init.yml missing: {INIT_PLAYBOOK}")
            return
        self.run_worker(self._init_import_flow(), exclusive=True)

    async def _init_import_flow(self) -> None:
        path = await self.push_screen_wait(PathPrompt("Import deployment.yml"))
        if not path:
            return

        p = Path(os.path.expanduser(path)).resolve()
        if not p.exists():
            self._append_log(f"[error] File not found: {p}")
            return

        self.runner.run(
            RunSpec("Preflight inventory", ["ansible-inventory", "-i", str(p), "--list"]),
            on_done=lambda rc: self._after_preflight(rc, p),
        )

    def _after_preflight(self, rc: int, inventory: Path) -> None:
        if rc != 0:
            self._append_log("Preflight failed; not running validate/init.")
            return

        if VALIDATE_PLAYBOOK.exists():
            self.runner.run(
                RunSpec("Validate", ["ansible-playbook", "-i", str(inventory), str(VALIDATE_PLAYBOOK)]),
                on_done=lambda vrc: self._run_init(inventory) if vrc == 0 else self._after_init(),
            )
        else:
            self._append_log("validate.yml not present — skipping validate.")
            self._run_init(inventory)

    def _run_init(self, inventory: Path) -> None:
        self.runner.run(
            RunSpec("Init", ["ansible-playbook", "-i", str(inventory), str(INIT_PLAYBOOK)]),
            on_done=lambda _rc: self._after_init(),
        )

    def _after_init(self) -> None:
        self._refresh_envs()
        if len(self._env_names) == 1:
            try:
                set_current_env(self._env_names[0])
                self.current_env = self._env_names[0]
                self._append_log(f"Auto-selected only environment after init: {self.current_env}")
            except Exception as ex:
                self._append_log(f"[error] {ex}")
        else:
            current = get_current_env()
            self.current_env = current or "<not set>"
        self._refresh_dashboard("init/import")

    def action_validate(self) -> None:
        if self.runner.busy:
            self._append_log("[warn] Busy running a command.")
            return
        if not VALIDATE_PLAYBOOK.exists():
            self._append_log(f"[error] validate.yml missing: {VALIDATE_PLAYBOOK}")
            return
        self.run_worker(self._validate_flow(), exclusive=True)

    async def _validate_flow(self) -> None:
        path = await self.push_screen_wait(PathPrompt("Validate deployment.yml"))
        if not path:
            return
        p = Path(os.path.expanduser(path)).resolve()
        if not p.exists():
            self._append_log(f"[error] File not found: {p}")
            return
        self.runner.run(
            RunSpec("Preflight inventory", ["ansible-inventory", "-i", str(p), "--list"]),
            on_done=lambda rc: self._after_validate_preflight(rc, p),
        )

    def _after_validate_preflight(self, rc: int, inventory: Path) -> None:
        if rc != 0:
            self._append_log("Preflight failed; validate not run.")
            return
        self.runner.run(
            RunSpec("Validate", ["ansible-playbook", "-i", str(inventory), str(VALIDATE_PLAYBOOK)]),
            on_done=lambda _rc: self._refresh_dashboard("validate"),
        )

    def action_deploy(self) -> None:
        self.current_pane = "admin"
        self._admin_stage = "deploy_group"
        self._admin_deploy_group_index = 0
        self._render_left_pane()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "admin_list":
            return
        if self.current_pane == "admin":
            self.action_admin_select()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_admin":
            self.action_admin()
        elif bid == "btn_status":
            self.action_status()
        elif bid == "btn_debug":
            self.action_debug()
        elif bid == "btn_docs":
            self.action_docs()
        elif bid == "btn_gather":
            self.action_gather_status()
        elif bid == "btn_back":
            self.action_back()
        elif bid == "btn_clear":
            self.action_clear_log()


if __name__ == "__main__":
    OpenSpaceTUI().run()
