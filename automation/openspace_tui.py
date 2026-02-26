#!/usr/bin/env python3
"""OpenSpace Lifecycle Manager dashboard-first Textual UI."""
from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from rich.text import Text
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, RichLog, Static

# ----------------------------- #
# Config                        #
# ----------------------------- #
OPENSPACE_ROOT = Path(os.environ.get("OPENSPACE_ROOT", "/opt/openspace"))
LCM_TASKS_DIR = OPENSPACE_ROOT / "automation" / "lifecycle-manager" / "tasks"
INIT_PLAYBOOK = LCM_TASKS_DIR / "init.yml"
VALIDATE_PLAYBOOK = LCM_TASKS_DIR / "validate.yml"
STATUS_PLAYBOOK = LCM_TASKS_DIR / "status.yml"  # optional
CURRENT_ENV_LINK = OPENSPACE_ROOT / "env" / "current"
TASK_NS = "automation"

# ----------------------------- #
# Dashboard model               #
# ----------------------------- #
@dataclass
class DashboardSection:
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class DashboardModel:
    generated_at: str
    last_action: str
    sections: list[DashboardSection]


# ----------------------------- #
# Helpers                       #
# ----------------------------- #
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
    return list(task_descriptions(taskfile).keys())


def filter_tasks_by_prefix(tasks: Iterable[str], prefix: str) -> list[str]:
    return [t for t in tasks if t.startswith(prefix)]


def task_descriptions(taskfile: Path) -> dict[str, str]:
    p = subprocess.run(
        ["task", "-t", str(taskfile), "--list"], capture_output=True, text=True
    )
    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip() or p.stdout.strip() or "task --list failed"
        )
    descriptions: dict[str, str] = {}
    pattern = re.compile(r"^\s*[*-]\s+([A-Za-z0-9:_-]+)\s*:\s*(.*)$")
    for line in p.stdout.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        desc = m.group(2).strip()
        if name.startswith(f"{TASK_NS}:"):
            name = name[len(TASK_NS) + 1 :]
        if name == "task":
            continue
        descriptions[name] = desc
    return descriptions


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


# ----------------------------- #
# Taskfile YAML introspection   #
# ----------------------------- #
def _load_taskfile_yaml(taskfile: Path) -> dict[str, Any]:
    """Load and return the raw Taskfile YAML as a dict."""
    with open(taskfile, "r") as f:
        return yaml.safe_load(f) or {}


def _resolve_included_taskfiles(taskfile: Path) -> dict[str, Path]:
    """Return a map of namespace -> resolved Taskfile path from `includes:`."""
    data = _load_taskfile_yaml(taskfile)
    includes = data.get("includes", {})
    resolved: dict[str, Path] = {}
    base_dir = taskfile.parent
    for ns, spec in includes.items():
        if isinstance(spec, str):
            inc_path = base_dir / spec
        elif isinstance(spec, dict):
            tf_val = spec.get("taskfile", "")
            inc_path = Path(tf_val) if os.path.isabs(tf_val) else base_dir / tf_val
        else:
            continue
        # If pointing at a directory, look for Taskfile.yml inside it
        if inc_path.is_dir():
            inc_path = inc_path / "Taskfile.yml"
        if inc_path.exists():
            resolved[ns] = inc_path.resolve()
    return resolved


def _collect_all_task_defs(taskfile: Path) -> dict[str, dict[str, Any]]:
    """Walk the root Taskfile and all includes, returning a flat map of
    YAML task key -> task definition dict.

    The keys are the raw YAML task names as they appear in each file,
    WITHOUT any namespace prefix.  For example, a task defined as:

        Deploy:Clusters:
          requires:
            vars: [CLUSTER_TYPE]

    is stored under the key ``"Deploy:Clusters:"``.

    We also store a *normalized* alias (trailing colons stripped, lowered) so
    that callers can look up by the name reported by ``task --list`` after
    the ``automation:`` prefix is stripped.
    """
    all_tasks: dict[str, dict[str, Any]] = {}

    def _walk(tf: Path) -> None:
        data = _load_taskfile_yaml(tf)

        # Gather tasks defined in this file
        for name, defn in data.get("tasks", {}).items():
            if name.startswith("_"):
                continue
            if not isinstance(defn, dict):
                continue
            # Store under raw YAML key
            all_tasks[name] = defn
            # Also store under stripped key (no trailing colons)
            stripped = name.rstrip(":")
            if stripped != name:
                all_tasks[stripped] = defn

        # Recurse into includes
        for _ns, inc_path in _resolve_included_taskfiles(tf).items():
            _walk(inc_path)

    _walk(taskfile)
    return all_tasks


def get_task_required_vars(taskfile: Path, task_name: str) -> list[str]:
    """Return the list of required variable names for a given task.

    ``task_name`` is the name as the TUI knows it — i.e. after stripping the
    ``automation:`` namespace prefix.  We try several lookup strategies to
    match it against the raw YAML keys:

      1. Exact match (e.g. ``"Deploy:Clusters"``)
      2. With trailing colon (e.g. ``"Deploy:Clusters:"``)
      3. Case-insensitive match

    Handles both ``requires.vars`` formats:
        requires:
          vars: [CLUSTER_TYPE]
    and:
        requires:
          vars:
            - CLUSTER_TYPE
            - sh: '[ -n "{{.SOMETHING}}" ]'
    """
    all_defs = _collect_all_task_defs(taskfile)

    # Try exact match first
    defn = all_defs.get(task_name)

    # Try with trailing colon (YAML keys like "Deploy:Clusters:")
    if defn is None:
        defn = all_defs.get(task_name + ":")

    # Try case-insensitive match
    if defn is None:
        lower = task_name.lower()
        for key, val in all_defs.items():
            if key.lower().rstrip(":") == lower or key.lower() == lower:
                defn = val
                break

    if not defn or not isinstance(defn, dict):
        return []

    requires = defn.get("requires")
    if not isinstance(requires, dict):
        return []
    raw_vars = requires.get("vars", [])
    if not isinstance(raw_vars, list):
        return []
    names: list[str] = []
    for v in raw_vars:
        if isinstance(v, str):
            names.append(v)
        elif isinstance(v, dict):
            if "name" in v:
                names.append(v["name"])
    return names


# ----------------------------- #
# Inventory introspection       #
# ----------------------------- #
def get_inventory_data(env_name: str) -> Optional[dict]:
    """Run ansible-inventory --list for the current environment's deployment.yml."""
    env_root = OPENSPACE_ROOT / "env" / env_name
    # Try common inventory file names
    for candidate in ("deployment.yml", "inventory.yml", "hosts.yml", "inventory"):
        inv_path = env_root / candidate
        if inv_path.exists():
            try:
                p = subprocess.run(
                    ["ansible-inventory", "-i", str(inv_path), "--list"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if p.returncode == 0:
                    return json.loads(p.stdout)
            except Exception:
                pass
    return None


def inventory_all_hosts(inv: dict) -> list[str]:
    """Return all host names from an ansible-inventory --list output."""
    meta = inv.get("_meta", {})
    hostvars = meta.get("hostvars", {})
    if hostvars:
        return sorted(hostvars.keys())
    # Fallback: gather from all groups
    hosts: set[str] = set()
    for group_name, group_data in inv.items():
        if group_name == "_meta":
            continue
        if isinstance(group_data, dict):
            group_hosts = group_data.get("hosts", [])
            if isinstance(group_hosts, list):
                hosts.update(group_hosts)
    return sorted(hosts)


def inventory_groups(inv: dict) -> list[str]:
    """Return all group names (excluding _meta and 'all')."""
    return sorted(
        k for k in inv.keys() if k not in ("_meta", "all", "ungrouped")
    )


def inventory_hosts_in_group(inv: dict, group: str) -> list[str]:
    """Return hosts belonging to a specific group (non-recursive)."""
    grp = inv.get(group, {})
    if isinstance(grp, dict):
        hosts = grp.get("hosts", [])
        if isinstance(hosts, list):
            return sorted(hosts)
    return []


def inventory_child_groups(inv: dict, parent: str) -> list[str]:
    """Return child group names of a parent group."""
    grp = inv.get(parent, {})
    if isinstance(grp, dict):
        children = grp.get("children", [])
        if isinstance(children, list):
            return [c for c in children if isinstance(c, str)]
    return []


def inventory_hosts_by_hostvar(
    inv: dict, var_name: str, var_value: str
) -> list[str]:
    """Return host names where a specific hostvar equals the given value.

    Searches ``_meta.hostvars`` for hosts whose ``var_name`` matches
    ``var_value`` (case-insensitive string comparison).
    """
    meta = inv.get("_meta", {})
    hostvars = meta.get("hostvars", {})
    matched: list[str] = []
    for host, hvars in hostvars.items():
        if not isinstance(hvars, dict):
            continue
        val = hvars.get(var_name)
        if isinstance(val, str) and val.lower() == var_value.lower():
            matched.append(host)
    return sorted(matched)


# ----------------------------- #
# Variable resolver registry    #
# ----------------------------- #
@dataclass
class VarResolverResult:
    """Result from resolving a single variable — may set multiple task vars."""
    resolved_vars: dict[str, str]


class VarResolver:
    """Knows how to present choices for a task variable based on inventory data.

    The registry maps variable name patterns to resolver functions.
    Each resolver returns (display_choices, callback_to_build_vars).
    """

    def __init__(self, inv: Optional[dict]):
        self.inv = inv
        # Map of var_name -> resolver method
        # Methods return (list_of_display_strings, resolver_fn(selected_display) -> dict[str,str])
        self._registry: dict[str, Callable] = {}
        self._build_registry()

    def _build_registry(self) -> None:
        """Build pattern-based registry.

        This is intentionally configurable — add new variable name patterns here
        as your Taskfile grows. The key is the exact required var name.
        """
        # Cluster selection patterns
        for var_name in ("CLUSTER_TYPE", "CLUSTER_GROUP", "CLUSTER"):
            self._registry[var_name] = self._resolve_cluster_type

        # Host/node selection patterns (all hosts)
        for var_name in (
            "TARGET_HOST", "TARGET_HOSTS", "HOST", "HOSTS",
            "NODE", "NODES", "LIMIT", "TARGET_NODE",
            "VM", "VMS",
        ):
            self._registry[var_name] = self._resolve_host

        # Libvirt VM selection (hosts with host_type == libvirt_vm)
        for var_name in ("LIBVIRT_VM", "LIBVIRT_VMS"):
            self._registry[var_name] = self._resolve_libvirt_vm

        # Group selection patterns
        for var_name in ("GROUP", "TARGET_GROUP", "HOST_GROUP"):
            self._registry[var_name] = self._resolve_group

    def can_resolve(self, var_name: str) -> bool:
        return var_name in self._registry and self.inv is not None

    def get_choices(self, var_name: str) -> list[str]:
        """Return display choices for a variable, or empty list if no resolver."""
        if var_name not in self._registry or self.inv is None:
            return []
        return self._registry[var_name](var_name, choices_only=True)

    def resolve(self, var_name: str, selected: str) -> dict[str, str]:
        """Given a user's selection, return the var(s) to pass to the task.

        Some selections may expand into multiple vars. For example, selecting
        a cluster type might set both CLUSTER_TYPE and CLUSTER_GROUP.
        """
        if var_name not in self._registry or self.inv is None:
            return {var_name: selected}
        return self._registry[var_name](var_name, choices_only=False, selected=selected)

    def _resolve_cluster_type(
        self, var_name: str, choices_only: bool = True, selected: str = ""
    ) -> list[str] | dict[str, str]:
        """Resolve cluster type from inventory groups.

        Looks for groups that represent deployable clusters — typically children
        of a well-known parent group, or groups matching common patterns.
        """
        assert self.inv is not None
        cluster_groups: list[str] = []

        # Strategy 1: Look for children of common parent groups
        for parent in ("clusters", "downstream_clusters", "all_clusters", "k8s_clusters"):
            children = inventory_child_groups(self.inv, parent)
            if children:
                cluster_groups.extend(children)

        # Strategy 2: Look for groups matching cluster naming patterns
        if not cluster_groups:
            all_groups = inventory_groups(self.inv)
            cluster_patterns = re.compile(
                r"(osms|osdc|mcm|management|downstream|cluster)", re.IGNORECASE
            )
            cluster_groups = [g for g in all_groups if cluster_patterns.search(g)]

        # Strategy 3: Just offer all non-trivial groups
        if not cluster_groups:
            cluster_groups = [
                g for g in inventory_groups(self.inv)
                if inventory_hosts_in_group(self.inv, g)
            ]

        # Deduplicate and sort
        seen: set[str] = set()
        unique: list[str] = []
        for g in cluster_groups:
            if g not in seen:
                seen.add(g)
                unique.append(g)
        cluster_groups = sorted(unique)

        if choices_only:
            return cluster_groups

        # Build the result vars — set both CLUSTER_TYPE and CLUSTER_GROUP
        # since tasks often derive one from the other
        result = {var_name: selected}
        if var_name == "CLUSTER_TYPE":
            result["CLUSTER_GROUP"] = selected
        elif var_name == "CLUSTER_GROUP":
            result["CLUSTER_TYPE"] = selected
        return result

    def _resolve_host(
        self, var_name: str, choices_only: bool = True, selected: str = ""
    ) -> list[str] | dict[str, str]:
        """Resolve a host/node selection from inventory."""
        assert self.inv is not None
        hosts = inventory_all_hosts(self.inv)

        if choices_only:
            return hosts

        return {var_name: selected}

    def _resolve_libvirt_vm(
        self, var_name: str, choices_only: bool = True, selected: str = ""
    ) -> list[str] | dict[str, str]:
        """Resolve a libvirt VM selection — hosts with host_type == libvirt_vm."""
        assert self.inv is not None
        hosts = inventory_hosts_by_hostvar(self.inv, "host_type", "libvirt_vm")

        if choices_only:
            return hosts

        return {var_name: selected}

    def _resolve_group(
        self, var_name: str, choices_only: bool = True, selected: str = ""
    ) -> list[str] | dict[str, str]:
        """Resolve a group selection from inventory."""
        assert self.inv is not None
        groups = [
            g for g in inventory_groups(self.inv)
            if inventory_hosts_in_group(self.inv, g)
            or inventory_child_groups(self.inv, g)
        ]

        if choices_only:
            return groups

        return {var_name: selected}


# ----------------------------- #
# Runner                        #
# ----------------------------- #
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

    def run(
        self, spec: RunSpec, on_done: Optional[Callable[[int], None]] = None
    ) -> None:
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


# ----------------------------- #
# Modal prompts                 #
# ----------------------------- #
class PathPrompt(ModalScreen[Optional[str]]):
    def __init__(self, title: str, placeholder: str = "/path/to/deployment.yml"):
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._title),
            Input(placeholder=self._placeholder, id="path_input"),
            Horizontal(
                Button("Cancel", id="path_cancel"),
                Button("OK", variant="primary", id="path_ok"),
            ),
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
    def __init__(
        self, title: str, choices: list[str], run_label: str = "Select"
    ):
        super().__init__()
        self._title = title
        self._choices = choices
        self._run_label = run_label

    def compose(self) -> ComposeResult:
        lv = ListView(
            *[ListItem(Label(c)) for c in self._choices], id="choice_list"
        )
        yield Vertical(
            Label(self._title),
            lv,
            Horizontal(
                Button("Back", id="choice_back"),
                Button(self._run_label, variant="primary", id="choice_ok"),
            ),
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


# ----------------------------- #
# Drill-down screens            #
# ----------------------------- #
class InfoScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._title),
            Static(self._body),
            Button("Back", id="info_back"),
            id="info_root",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "info_back":
            self.app.pop_screen()


# ----------------------------- #
# App                           #
# ----------------------------- #
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
    #path_root, #choice_root {
        border: round #666; padding: 1; width: 90%; height: 90%; margin: 1;
    }
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
        self._admin_stage = "task_nav"
        self._admin_task_tree: dict[str, dict] = {}
        self._admin_task_path: list[str] = []
        self._admin_task_descriptions: dict[str, str] = {}
        self._admin_option_keys: list[str] = []
        self._admin_display_options: list[str] = []
        # Cached inventory data for var resolution
        self._cached_inventory: Optional[dict] = None
        self._cached_inventory_env: Optional[str] = None

    def _get_inventory(self) -> Optional[dict]:
        """Return cached inventory data, refreshing if env changed."""
        if self.current_env == "<not set>":
            return None
        if self._cached_inventory_env != self.current_env:
            self._cached_inventory = get_inventory_data(self.current_env)
            self._cached_inventory_env = self.current_env
            if self._cached_inventory:
                self._append_log(f"Loaded inventory for {self.current_env}")
            else:
                self._append_log(f"[warn] Could not load inventory for {self.current_env}")
        return self._cached_inventory

    def inventory_json(self, inventory_path: Path) -> dict:
        p = subprocess.run(
            ["ansible-inventory", "-i", str(inventory_path), "--list"],
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            detail = (
                p.stderr.strip() or p.stdout.strip() or "ansible-inventory failed"
            )
            raise RuntimeError(detail)
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError as ex:
            raise RuntimeError(
                f"Failed to parse ansible-inventory JSON: {ex}"
            ) from ex

    def list_child_groups(self, inv: dict, parent_group: str) -> list[str]:
        parent = inv.get(parent_group)
        if not isinstance(parent, dict):
            return []
        children = parent.get("children", [])
        if not isinstance(children, list):
            return []
        return [child for child in children if isinstance(child, str)]

    def group_host_count(self, inv: dict, group: str) -> int:
        grp = inv.get(group)
        if not isinstance(grp, dict):
            return 0
        hosts = grp.get("hosts", [])
        if isinstance(hosts, list):
            return len(hosts)
        if isinstance(hosts, dict):
            return len(hosts.keys())
        return 0

    def _build_task_tree(self, tasks: list[str]) -> dict[str, dict]:
        tree: dict[str, dict] = {}
        for task in tasks:
            parts = [part for part in task.split(":") if part]
            if not parts:
                continue
            node = tree
            for part in parts:
                node = node.setdefault(part, {})
            node["__task__"] = task
        return tree

    def _child_keys(self, node: dict[str, dict]) -> list[str]:
        return sorted([k for k in node.keys() if k != "__task__"])

    def _node_for_path(self, path: list[str]) -> Optional[dict[str, dict]]:
        node: dict[str, dict] = self._admin_task_tree
        for part in path:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                return None
            node = nxt
        return node

    def _load_admin_task_browser(self) -> None:
        self._admin_task_tree = {}
        self._admin_task_path = []
        self._admin_task_descriptions = {}
        if not self._ensure_env_selected():
            self._admin_option_keys = []
            self._admin_display_options = []
            return
        try:
            tf = resolve_env_taskfile(self.current_env)
            tasks = task_list(tf)
            self._admin_task_tree = self._build_task_tree(tasks)
            self._admin_task_descriptions = task_descriptions(tf)
        except Exception as ex:
            self._append_log(f"[error] {ex}")
            self._admin_task_tree = {}
            self._admin_task_descriptions = {}
        self._refresh_admin_options()

    def _format_option_label(self, node: dict[str, dict], key: str) -> str:
        child = node.get(key)
        if not isinstance(child, dict):
            return key
        task_name = child.get("__task__")
        if isinstance(task_name, str) and not self._child_keys(child):
            desc = self._admin_task_descriptions.get(task_name, "")
            return f"{key} — {desc}" if desc else key
        return key

    def _refresh_admin_options(self) -> None:
        node = self._node_for_path(self._admin_task_path)
        node = node or {}
        self._admin_option_keys = self._child_keys(node)
        self._admin_display_options = [
            self._format_option_label(node, key) for key in self._admin_option_keys
        ]

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
            self._append_log(
                f"Auto-selected only environment: {self.current_env}"
            )
            self._update_topbar()
            return
        if len(self._env_names) > 1:
            selected = await self.push_screen_wait(
                ChoiceScreen("Select environment", self._env_names, "Use")
            )
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
        if self._admin_stage == "var_select":
            # Show variable selection inline
            collected_str = ", ".join(f"{k}={v}" for k, v in self._var_collected.items())
            title_parts = [f"Select {self._var_current_name}"]
            if collected_str:
                title_parts.append(f"[{collected_str}]")
            self._set_left_title(" ".join(title_parts))
            self._admin_option_keys = list(self._var_choices)
            self._admin_display_options = list(self._var_choices)
            self._refresh_admin_list()
            return

        breadcrumb = " / ".join(self._admin_task_path)
        self._set_left_title(
            f"Admin{(' / ' + breadcrumb) if breadcrumb else ''}"
        )
        if self._admin_stage == "task_nav":
            self._refresh_admin_options()
        else:
            self._admin_option_keys = []
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

    # --------------------------------- #
    # Task execution with var resolution #
    # --------------------------------- #

    # State for inline var selection (admin_stage == "var_select")
    # These are set by _execute_task and consumed by action_admin_select / _render_admin_pane
    _var_task_name: str = ""                    # task being configured
    _var_required: list[str] = []               # ordered list of var names still needed
    _var_collected: dict[str, str] = {}         # vars already chosen
    _var_current_name: str = ""                 # the var we're currently prompting for
    _var_choices: list[str] = []                # choices for the current var
    _var_resolver: Optional[VarResolver] = None # resolver instance

    def _execute_task(self, task_name: str) -> None:
        """Entry point to run a task — checks for required vars and prompts if needed."""
        if self.runner.busy:
            self._append_log("[warn] Busy running a command.")
            return
        if not self._ensure_env_selected():
            return

        tf = resolve_env_taskfile(self.current_env)
        required_vars = get_task_required_vars(tf, task_name)

        if not required_vars:
            # No vars needed — run directly
            self._run_task_with_vars(task_name, {})
            return

        # Initialise inline var-selection state
        inv = self._get_inventory()
        self._var_task_name = task_name
        self._var_required = list(required_vars)
        self._var_collected = {}
        self._var_resolver = VarResolver(inv)

        self._append_log(
            f"Task {task_name} requires: {', '.join(required_vars)}"
        )
        self._advance_var_selection()

    def _advance_var_selection(self) -> None:
        """Move to the next unresolved variable, or run the task if all are collected."""
        # Skip vars already satisfied (e.g. side-effect of a previous selection)
        while self._var_required and self._var_required[0] in self._var_collected:
            self._var_required.pop(0)

        if not self._var_required:
            # All vars collected — check for task routing, then run
            task_name = self._var_task_name
            task_vars = dict(self._var_collected)
            self._clear_var_state()

            routed = self._check_task_route(task_name, task_vars)
            if routed:
                routed_task, routed_vars = routed
                self._append_log(f"  Routed → {routed_task}")
                self._run_task_with_vars(routed_task, routed_vars)
            else:
                self._run_task_with_vars(task_name, task_vars)
            return

        var_name = self._var_required[0]
        self._var_current_name = var_name

        # Get choices from resolver
        resolver = self._var_resolver
        choices = resolver.get_choices(var_name) if resolver else []

        if choices:
            self._var_choices = choices
            self._admin_stage = "var_select"
            self._render_left_pane()
        else:
            # No inventory choices — fall back to text input modal
            # (This is the only case where we still need a modal, since
            # free-text entry doesn't fit a ListView)
            self._admin_stage = "var_select"
            self._var_choices = []
            self.run_worker(self._prompt_freetext_var(var_name), exclusive=True)

    async def _prompt_freetext_var(self, var_name: str) -> None:
        """Fallback modal for vars with no inventory-driven choices."""
        inv = self._get_inventory()
        hint = "(no inventory loaded)" if inv is None else "(no auto-choices available)"
        value = await self.push_screen_wait(
            PathPrompt(
                f"Enter value for {var_name} {hint}",
                placeholder=f"{var_name}=...",
            )
        )
        if value is None:
            self._append_log(f"Cancelled — {var_name} not provided.")
            self._cancel_var_selection()
            return
        self._var_collected[var_name] = value
        self._var_required.pop(0)
        self._append_log(f"  {var_name} = {value}")
        self._advance_var_selection()

    def _select_var_choice(self, selected: str) -> None:
        """Handle user selecting a choice for the current variable."""
        var_name = self._var_current_name
        resolver = self._var_resolver
        if resolver:
            resolved = resolver.resolve(var_name, selected)
        else:
            resolved = {var_name: selected}

        self._var_collected.update(resolved)
        self._var_required.pop(0)

        extra = {k: v for k, v in resolved.items() if k != var_name}
        self._append_log(
            f"  {var_name} = {selected}"
            + (f" (also set: {', '.join(f'{k}={v}' for k, v in extra.items())})" if extra else "")
        )
        self._advance_var_selection()

    def _cancel_var_selection(self) -> None:
        """Cancel var selection and return to task navigation."""
        self._append_log("[info] Variable selection cancelled.")
        self._clear_var_state()
        self._admin_stage = "task_nav"
        self._admin_task_path = []
        self._render_left_pane()

    def _clear_var_state(self) -> None:
        """Reset all var-selection state."""
        self._var_task_name = ""
        self._var_required = []
        self._var_collected = {}
        self._var_current_name = ""
        self._var_choices = []
        self._var_resolver = None
        self._admin_stage = "task_nav"
        self._admin_task_path = []

    # --------------------------------- #
    # Task routing                      #
    # --------------------------------- #
    # Routes redirect a task to a different task based on the collected
    # variable values.  Each entry is:
    #   (original_task, var_name, var_value) -> (alternate_task, replacement_vars)
    #
    # - var_value is compared case-insensitively against the group's
    #   ``cluster_type`` hostvar (looked up from inventory), or against
    #   the raw selected value.
    # - replacement_vars: the vars to pass to the alternate task (empty
    #   dict means no vars needed).
    #
    # This is checked AFTER all vars are collected but BEFORE execution.

    TASK_ROUTES: list[
        tuple[str, str, str, str, dict[str, str]]
    ] = [
        # (original_task_name, var_name, match_value, redirect_task, override_vars)
        #
        # When deploying a cluster and the selected group has cluster_type == mcm,
        # redirect to the onboarder deployment task (which needs no vars).
        (
            "Deploy:Clusters:Deploy_A_Cluster",
            "CLUSTER_TYPE",
            "mcm",
            "Deploy:Onboarder:Deploy_Onboarder_Container",
            {},
        ),
    ]

    def _check_task_route(
        self, task_name: str, task_vars: dict[str, str]
    ) -> Optional[tuple[str, dict[str, str]]]:
        """Check if collected vars trigger a route to a different task.

        For CLUSTER_TYPE, we look up the selected group's ``cluster_type``
        from inventory.  ansible-inventory --list can place group vars in
        several locations depending on version and inventory plugin:

          1. group_data["vars"]["cluster_type"]       — some plugins
          2. _meta.hostvars[host]["cluster_type"]      — merged onto hosts
          3. group_name_itself matches                  — direct value

        We check all three.

        Returns (alternate_task, vars) or None.
        """
        inv = self._get_inventory()

        for orig_task, var_name, match_val, alt_task, alt_vars in self.TASK_ROUTES:
            if task_name != orig_task:
                continue
            selected = task_vars.get(var_name, "")
            if not selected:
                continue

            # Strategy 1: Direct match (selected value IS the match)
            if selected.lower() == match_val.lower():
                return (alt_task, alt_vars)

            if not inv:
                continue

            group_data = inv.get(selected, {})
            if not isinstance(group_data, dict):
                continue

            # Strategy 2: Group-level vars dict
            group_vars = group_data.get("vars", {})
            if isinstance(group_vars, dict):
                ct = group_vars.get("cluster_type", "")
                if isinstance(ct, str) and ct.lower() == match_val.lower():
                    return (alt_task, alt_vars)

            # Strategy 3: Check hostvars of hosts in this group
            # ansible-inventory --list often merges group vars into hostvars
            group_hosts = group_data.get("hosts", [])
            if isinstance(group_hosts, list) and group_hosts:
                meta = inv.get("_meta", {})
                hostvars = meta.get("hostvars", {})
                for host in group_hosts:
                    hvars = hostvars.get(host, {})
                    if isinstance(hvars, dict):
                        ct = hvars.get("cluster_type", "")
                        if isinstance(ct, str) and ct.lower() == match_val.lower():
                            return (alt_task, alt_vars)

        return None

    def _run_task_with_vars(
        self, task_name: str, task_vars: dict[str, str]
    ) -> None:
        """Build and execute the go-task command with collected variables."""
        if not self._ensure_env_selected():
            return

        tf = resolve_env_taskfile(self.current_env)
        full = f"{TASK_NS}:{task_name}"

        cmd = ["task", "-t", str(tf), full]
        # go-task accepts VAR=VALUE after the task name
        for k, v in task_vars.items():
            cmd.append(f"{k}={v}")

        self.runner.run(
            RunSpec(f"Admin task: {full}", cmd),
            on_done=lambda _rc: self._refresh_dashboard(f"admin task {task_name}"),
        )

    # --------------------------------- #
    # Actions                           #
    # --------------------------------- #
    def action_refresh(self) -> None:
        self._refresh_envs()
        current = get_current_env()
        self.current_env = current or "<not set>"
        # Invalidate inventory cache on refresh
        self._cached_inventory = None
        self._cached_inventory_env = None
        self._refresh_dashboard("manual refresh")

    def action_admin(self) -> None:
        self.current_pane = "admin"
        self._admin_stage = "task_nav"
        self._load_admin_task_browser()
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

        # --- Variable selection stage ---
        if self._admin_stage == "var_select":
            if not self._var_choices:
                return
            selected_index = max(0, min(selected_index, len(self._var_choices) - 1))
            selected = self._var_choices[selected_index]
            self._select_var_choice(selected)
            return

        # --- Task navigation stage ---
        if self._admin_stage != "task_nav":
            return

        node = self._node_for_path(self._admin_task_path)
        if node is None:
            self._append_log("[error] Invalid admin task path.")
            return

        options = self._admin_option_keys
        if not options:
            self._append_log("[warn] No task options available.")
            return

        selected_index = max(0, min(selected_index, len(options) - 1))
        selected = options[selected_index]

        next_path = [*self._admin_task_path, selected]
        next_node = self._node_for_path(next_path)
        if next_node is None:
            self._append_log(f"[error] Invalid task selection: {selected}")
            return

        children = self._child_keys(next_node)
        task_name = next_node.get("__task__")

        if children:
            # Descend into submenu
            self._admin_task_path = next_path
            self._render_left_pane()
            return

        if not isinstance(task_name, str):
            self._append_log("[error] Selected task is not runnable.")
            return

        # Use the var-aware execution flow (may switch to var_select stage)
        self._execute_task(task_name)

    def action_back(self) -> None:
        if self.current_pane == "admin":
            if self._admin_stage == "var_select":
                # Cancel variable selection, return to task tree root
                self._cancel_var_selection()
                return
            if self._admin_stage == "task_nav" and self._admin_task_path:
                self._admin_task_path.pop()
            else:
                self.current_pane = "status"
            self._render_left_pane()
            return
        if self.current_pane == "init":
            self.current_pane = "admin"
            self._admin_stage = "task_nav"
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
            self._append_log(
                f"[warn] status.yml missing: {STATUS_PLAYBOOK} (using placeholders in status pane)"
            )
            return
        env_root = OPENSPACE_ROOT / "env" / self.current_env
        default_inventory = env_root / "deployment.yml"
        if default_inventory.exists():
            inventory = default_inventory
            self.runner.run(
                RunSpec(
                    "Gather status",
                    [
                        "ansible-playbook",
                        "-i",
                        str(inventory),
                        str(STATUS_PLAYBOOK),
                    ],
                ),
                on_done=lambda _rc: self._refresh_dashboard("status gather"),
            )
            return
        self.run_worker(self._gather_status_prompt_flow(), exclusive=True)

    async def _gather_status_prompt_flow(self) -> None:
        path = await self.push_screen_wait(
            PathPrompt("Status inventory path", "/path/to/deployment.yml")
        )
        if not path:
            return
        inventory = Path(os.path.expanduser(path)).resolve()
        if not inventory.exists():
            self._append_log(f"[error] File not found: {inventory}")
            return
        self.runner.run(
            RunSpec(
                "Gather status",
                [
                    "ansible-playbook",
                    "-i",
                    str(inventory),
                    str(STATUS_PLAYBOOK),
                ],
            ),
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
            RunSpec(
                "Preflight inventory",
                ["ansible-inventory", "-i", str(p), "--list"],
            ),
            on_done=lambda rc: self._after_preflight(rc, p),
        )

    def _after_preflight(self, rc: int, inventory: Path) -> None:
        if rc != 0:
            self._append_log("Preflight failed; not running validate/init.")
            return
        if VALIDATE_PLAYBOOK.exists():
            self.runner.run(
                RunSpec(
                    "Validate",
                    [
                        "ansible-playbook",
                        "-i",
                        str(inventory),
                        str(VALIDATE_PLAYBOOK),
                    ],
                ),
                on_done=lambda vrc: self._run_init(inventory)
                if vrc == 0
                else self._after_init(),
            )
        else:
            self._append_log("validate.yml not present — skipping validate.")
            self._run_init(inventory)

    def _run_init(self, inventory: Path) -> None:
        self.runner.run(
            RunSpec(
                "Init",
                [
                    "ansible-playbook",
                    "-i",
                    str(inventory),
                    str(INIT_PLAYBOOK),
                ],
            ),
            on_done=lambda _rc: self._after_init(),
        )

    def _after_init(self) -> None:
        self._refresh_envs()
        if len(self._env_names) == 1:
            try:
                set_current_env(self._env_names[0])
                self.current_env = self._env_names[0]
                self._append_log(
                    f"Auto-selected only environment after init: {self.current_env}"
                )
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
            self._append_log(
                f"[error] validate.yml missing: {VALIDATE_PLAYBOOK}"
            )
            return
        self.run_worker(self._validate_flow(), exclusive=True)

    async def _validate_flow(self) -> None:
        path = await self.push_screen_wait(
            PathPrompt("Validate deployment.yml")
        )
        if not path:
            return
        p = Path(os.path.expanduser(path)).resolve()
        if not p.exists():
            self._append_log(f"[error] File not found: {p}")
            return
        self.runner.run(
            RunSpec(
                "Preflight inventory",
                ["ansible-inventory", "-i", str(p), "--list"],
            ),
            on_done=lambda rc: self._after_validate_preflight(rc, p),
        )

    def _after_validate_preflight(self, rc: int, inventory: Path) -> None:
        if rc != 0:
            self._append_log("Preflight failed; validate not run.")
            return
        self.runner.run(
            RunSpec(
                "Validate",
                [
                    "ansible-playbook",
                    "-i",
                    str(inventory),
                    str(VALIDATE_PLAYBOOK),
                ],
            ),
            on_done=lambda _rc: self._refresh_dashboard("validate"),
        )

    def action_deploy(self) -> None:
        self.current_pane = "admin"
        self._admin_stage = "task_nav"
        if not self._admin_task_tree:
            self._load_admin_task_browser()
        if "deploy" in self._admin_task_tree:
            self._admin_task_path = ["deploy"]
        else:
            self._admin_task_path = []
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