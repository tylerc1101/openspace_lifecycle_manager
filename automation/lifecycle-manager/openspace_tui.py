#!/usr/bin/env python3
"""
OpenSpace Lifecycle Manager — Textual TUI Wrapper (v2)

A professional, dashboard-first terminal interface for managing OpenSpace
infrastructure through Go Task automation.

This is a WRAPPER — all values are resolved dynamically from:
  - Environment variables (OPENSPACE_ROOT, OPENSPACE_ENV)
  - Taskfile.yml task definitions (task --list, YAML introspection)
  - Ansible inventory (deployment.yml)
  - Filesystem state (/opt/openspace/env/*)

No infrastructure values are hardcoded in this file.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from rich.markup import escape as rich_escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import (
    Container,
    Horizontal,
    HorizontalGroup,
    Vertical,
    VerticalScroll,
)
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    RichLog,
    Rule,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — All paths derived from environment, nothing hardcoded
# ═══════════════════════════════════════════════════════════════════════════════
OPENSPACE_ROOT = Path(os.environ.get("OPENSPACE_ROOT", "/opt/openspace"))
LCM_DIR = OPENSPACE_ROOT / "automation" / "lifecycle-manager"
LCM_TASKS_DIR = LCM_DIR / "tasks"
STATUS_PLAYBOOK = LCM_TASKS_DIR / "status.yml"
INIT_PLAYBOOK = LCM_TASKS_DIR / "init.yml"
VALIDATE_PLAYBOOK = LCM_TASKS_DIR / "validate.yml"
CURRENT_ENV_LINK = OPENSPACE_ROOT / "env" / "current"
DOCS_DIR = OPENSPACE_ROOT / "docs"
LOG_DIR = OPENSPACE_ROOT / "logs"

# Poll interval for status page (seconds) — 0 disables
STATUS_POLL_INTERVAL = int(os.environ.get("OPENSPACE_STATUS_POLL", "60"))


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class ServiceHealth:
    """Health status for a single service/component."""
    name: str
    status: str = "unknown"  # ok, degraded, down, unknown
    detail: str = ""
    last_check: str = ""


@dataclass
class RunSpec:
    """Specification for a command to execute."""
    label: str
    cmd: list[str]
    cwd: Optional[Path] = None
    env_extra: Optional[dict[str, str]] = None


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — Pure functions, no UI coupling
# ═══════════════════════════════════════════════════════════════════════════════
def list_envs(root: Path = OPENSPACE_ROOT) -> list[str]:
    """Discover available environments by scanning /opt/openspace/env/."""
    env_dir = root / "env"
    if not env_dir.exists():
        return []
    names: list[str] = []
    for entry in sorted(env_dir.iterdir()):
        if not entry.is_dir() or entry.name == "current":
            continue
        if (entry / "Taskfile.yml").exists() or (entry / "deployment.yml").exists():
            names.append(entry.name)
    return names


def get_current_env() -> Optional[str]:
    """Read the current environment from the symlink."""
    try:
        if CURRENT_ENV_LINK.is_symlink():
            return CURRENT_ENV_LINK.resolve().name
    except Exception:
        return None
    return None


def set_current_env(name: str) -> None:
    """Point the 'current' symlink to the named environment."""
    target = OPENSPACE_ROOT / "env" / name
    if not target.exists():
        raise FileNotFoundError(f"Environment directory not found: {target}")
    try:
        if CURRENT_ENV_LINK.exists() or CURRENT_ENV_LINK.is_symlink():
            CURRENT_ENV_LINK.unlink(missing_ok=True)
        CURRENT_ENV_LINK.symlink_to(target)
    except PermissionError:
        subprocess.check_call(
            ["sudo", "ln", "-sfn", str(target), str(CURRENT_ENV_LINK)]
        )


def resolve_env_taskfile(name: str) -> Optional[Path]:
    """Return the Taskfile for an environment, or None."""
    tf = OPENSPACE_ROOT / "env" / name / "Taskfile.yml"
    return tf if tf.exists() else None


def task_descriptions(taskfile: Path) -> dict[str, str]:
    """Run `task --list` and parse task names + descriptions."""
    try:
        p = subprocess.run(
            ["task", "-t", str(taskfile), "--list"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if p.returncode != 0:
        return {}
    descriptions: dict[str, str] = {}
    pattern = re.compile(r"^\s*[*\-]\s+([A-Za-z0-9:_\-]+)\s*:\s*(.*)$")
    for line in p.stdout.splitlines():
        m = pattern.match(line)
        if m:
            name = m.group(1).strip()
            desc = m.group(2).strip()
            descriptions[name] = desc
    return descriptions


def build_task_tree(tasks: dict[str, str]) -> dict:
    """Build a nested dict from colon-separated task names.

    Example: {"Deploy:MCM:Prep": "desc"} ->
        {"Deploy": {"MCM": {"Prep": {"__desc__": "desc", "__leaf__": True}}}}
    """
    tree: dict = {}
    for full_name, desc in sorted(tasks.items()):
        parts = full_name.split(":")
        node = tree
        for i, part in enumerate(parts):
            if part not in node:
                node[part] = {}
            node = node[part]
        node["__desc__"] = desc
        node["__task__"] = full_name
        node["__leaf__"] = True
    return tree


def _load_taskfile_yaml(taskfile: Path) -> dict[str, Any]:
    """Load and return the raw Taskfile YAML as a dict."""
    try:
        with open(taskfile, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


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
        if inc_path.is_dir():
            inc_path = inc_path / "Taskfile.yml"
        if inc_path.exists():
            resolved[ns] = inc_path.resolve()
    return resolved


def _collect_all_task_defs(taskfile: Path) -> dict[str, dict[str, Any]]:
    """Walk the root Taskfile and all includes, returning a flat map of
    task name -> task definition dict.

    Stores each task under multiple keys for flexible lookup:
      - Raw YAML key as-is
      - With trailing colon stripped
      - With include namespace prefix (ns:taskname)
    """
    all_tasks: dict[str, dict[str, Any]] = {}

    def _walk(tf: Path, ns_prefix: str = "") -> None:
        data = _load_taskfile_yaml(tf)
        for name, defn in data.get("tasks", {}).items():
            if name.startswith("_"):
                continue
            if not isinstance(defn, dict):
                continue
            # Store under raw YAML key
            all_tasks[name] = defn
            stripped = name.rstrip(":")
            if stripped != name:
                all_tasks[stripped] = defn
            # Also store with namespace prefix if we're in an include
            if ns_prefix:
                prefixed = f"{ns_prefix}:{name}"
                all_tasks[prefixed] = defn
                prefixed_stripped = f"{ns_prefix}:{stripped}"
                if prefixed_stripped != prefixed:
                    all_tasks[prefixed_stripped] = defn
        # Recurse into includes with accumulated namespace
        for child_ns, inc_path in _resolve_included_taskfiles(tf).items():
            full_ns = f"{ns_prefix}:{child_ns}" if ns_prefix else child_ns
            _walk(inc_path, full_ns)

    _walk(taskfile)
    return all_tasks


def get_task_required_vars(
    taskfile: Path, task_name: str, debug_log: Optional[Callable[[str], None]] = None
) -> list[str]:
    """Return the list of required variable names for a given task.

    Tries multiple lookup strategies to handle namespace prefixing,
    trailing colons, and case differences:
      1. Exact match
      2. With trailing colon
      3. Progressive prefix stripping (Deploy:Clusters:X → Clusters:X → X)
      4. Case-insensitive match (including prefix-stripped)

    Handles both requires.vars formats:
        requires:
          vars: [CLUSTER_TYPE]
    and:
        requires:
          vars:
            - CLUSTER_TYPE
            - sh: '...'

    If debug_log is provided, it will be called with diagnostic messages.
    """
    _dbg = debug_log or (lambda _: None)

    all_defs = _collect_all_task_defs(taskfile)
    _dbg(f"   [debug] Looking up: '{task_name}'")
    _dbg(f"   [debug] Known keys ({len(all_defs)}): {sorted(all_defs.keys())[:20]}")

    # Strategy 1: Exact match
    defn = all_defs.get(task_name)
    if defn is not None:
        _dbg(f"   [debug] Matched via exact key")

    # Strategy 2: With trailing colon
    if defn is None:
        defn = all_defs.get(task_name + ":")
        if defn is not None:
            _dbg(f"   [debug] Matched via trailing colon")

    # Strategy 3: Progressive prefix stripping
    # task --list may report "automation:Deploy:Clusters:Deploy_A_Cluster"
    # but the YAML key is "Deploy:Clusters:Deploy_A_Cluster:" or just
    # "Deploy_A_Cluster:" in an included file
    if defn is None:
        parts = task_name.split(":")
        for i in range(1, len(parts)):
            suffix = ":".join(parts[i:])
            defn = all_defs.get(suffix)
            if defn is None:
                defn = all_defs.get(suffix + ":")
            if defn is not None:
                _dbg(f"   [debug] Matched via prefix strip: '{suffix}'")
                break

    # Strategy 4: Case-insensitive match (full name and suffixes)
    if defn is None:
        lower = task_name.lower()
        candidates = [lower]
        parts = task_name.split(":")
        for i in range(1, len(parts)):
            candidates.append(":".join(parts[i:]).lower())
        for key, val in all_defs.items():
            key_lower = key.lower().rstrip(":")
            if key_lower in candidates:
                defn = val
                _dbg(f"   [debug] Matched via case-insensitive: '{key}'")
                break

    if defn is None:
        _dbg(f"   [debug] NO MATCH found for '{task_name}'")

    if not defn or not isinstance(defn, dict):
        return []

    requires = defn.get("requires")
    if not isinstance(requires, dict):
        _dbg(f"   [debug] Task found but has no 'requires' block")
        return []
    raw_vars = requires.get("vars", [])
    if not isinstance(raw_vars, list):
        return []
    names: list[str] = []
    for v in raw_vars:
        if isinstance(v, str):
            names.append(v)
        elif isinstance(v, dict) and "name" in v:
            names.append(v["name"])
    _dbg(f"   [debug] Found required vars: {names}")
    return names


def get_inventory_data(env_name: str) -> Optional[dict]:
    """Run ansible-inventory --list for the environment's deployment.yml.

    Returns the raw JSON dict, or None on failure.
    """
    env_root = OPENSPACE_ROOT / "env" / env_name
    for candidate in ("deployment.yml", "inventory.yml", "hosts.yml", "inventory"):
        inv_path = env_root / candidate
        if inv_path.exists():
            try:
                p = subprocess.run(
                    ["ansible-inventory", "-i", str(inv_path), "--list"],
                    capture_output=True, text=True, timeout=30,
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
    """Return all group names (excluding _meta, all, ungrouped)."""
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
    """Return host names where a specific hostvar equals the given value."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# VARIABLE RESOLVER — Maps task var names to inventory-driven choices
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class VarResolverResult:
    """Result from resolving a single variable — may set multiple task vars."""
    resolved_vars: dict[str, str]


class VarResolver:
    """Presents inventory-driven choices for task variables.

    The registry maps variable name patterns to resolver methods.
    Each resolver returns either choices (list[str]) or resolved vars (dict).
    """

    def __init__(self, inv: Optional[dict]):
        self.inv = inv
        self._registry: dict[str, Callable] = {}
        self._build_registry()

    def _build_registry(self) -> None:
        for var_name in ("CLUSTER_TYPE", "CLUSTER_GROUP", "CLUSTER"):
            self._registry[var_name] = self._resolve_cluster_type
        for var_name in (
            "TARGET_HOST", "TARGET_HOSTS", "HOST", "HOSTS",
            "NODE", "NODES", "LIMIT", "TARGET_NODE", "VM", "VMS",
        ):
            self._registry[var_name] = self._resolve_host
        for var_name in ("LIBVIRT_VM", "LIBVIRT_VMS"):
            self._registry[var_name] = self._resolve_libvirt_vm
        for var_name in ("GROUP", "TARGET_GROUP", "HOST_GROUP"):
            self._registry[var_name] = self._resolve_group

    def can_resolve(self, var_name: str) -> bool:
        return var_name in self._registry and self.inv is not None

    def get_choices(self, var_name: str) -> list[str]:
        if var_name not in self._registry or self.inv is None:
            return []
        return self._registry[var_name](var_name, choices_only=True)

    def resolve(self, var_name: str, selected: str) -> dict[str, str]:
        """Given a user selection, return the var(s) to pass to the task.

        Some selections expand into multiple vars (e.g. CLUSTER_TYPE also
        sets CLUSTER_GROUP).
        """
        if var_name not in self._registry or self.inv is None:
            return {var_name: selected}
        return self._registry[var_name](
            var_name, choices_only=False, selected=selected
        )

    def _resolve_cluster_type(
        self, var_name: str, choices_only: bool = True, selected: str = ""
    ) -> list[str] | dict[str, str]:
        """Resolve cluster type — returns inventory GROUP NAMES (not type values).

        Looks for groups representing deployable clusters via:
        1. Children of well-known parent groups (clusters, downstream_clusters, etc.)
        2. Groups matching cluster naming patterns
        3. All non-trivial groups as fallback
        """
        assert self.inv is not None
        cluster_groups: list[str] = []

        # Strategy 1: children of common parent groups
        for parent in (
            "clusters", "downstream_clusters", "all_clusters", "k8s_clusters",
        ):
            children = inventory_child_groups(self.inv, parent)
            if children:
                cluster_groups.extend(children)

        # Strategy 2: groups matching cluster patterns
        if not cluster_groups:
            all_grps = inventory_groups(self.inv)
            pat = re.compile(
                r"(osms|osdc|mcm|management|downstream|cluster|local)",
                re.IGNORECASE,
            )
            cluster_groups = [g for g in all_grps if pat.search(g)]

        # Strategy 3: all groups with hosts
        if not cluster_groups:
            cluster_groups = [
                g for g in inventory_groups(self.inv)
                if inventory_hosts_in_group(self.inv, g)
            ]

        # Deduplicate
        seen: set[str] = set()
        unique: list[str] = []
        for g in cluster_groups:
            if g not in seen:
                seen.add(g)
                unique.append(g)
        cluster_groups = sorted(unique)

        if choices_only:
            return cluster_groups

        result = {var_name: selected}
        if var_name == "CLUSTER_TYPE":
            result["CLUSTER_GROUP"] = selected
        elif var_name == "CLUSTER_GROUP":
            result["CLUSTER_TYPE"] = selected
        return result

    def _resolve_host(
        self, var_name: str, choices_only: bool = True, selected: str = ""
    ) -> list[str] | dict[str, str]:
        """Resolve a host/node selection — all hosts from inventory."""
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


def discover_docs(docs_dir: Path = DOCS_DIR) -> list[Path]:
    """Find documentation files in the docs directory."""
    docs: list[Path] = []
    # Check root-level README first
    readme = OPENSPACE_ROOT / "README.md"
    if readme.exists():
        docs.append(readme)
    if docs_dir.exists():
        extensions = {".md", ".txt", ".rst", ".adoc"}
        for f in sorted(docs_dir.rglob("*")):
            if f.is_file() and f.suffix.lower() in extensions:
                docs.append(f)
    return docs


def discover_log_files() -> list[Path]:
    """Find recent log files."""
    if not LOG_DIR.exists():
        return []
    logs: list[Path] = []
    for f in sorted(
        LOG_DIR.rglob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        logs.append(f)
    return logs[:25]


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER — Async subprocess execution with log streaming
# ═══════════════════════════════════════════════════════════════════════════════
class Runner:
    """Manages subprocess execution and streams output to a callback."""

    def __init__(self, log_callback: Callable[[str], None]):
        self._log = log_callback
        self._proc: Optional[subprocess.Popen] = None
        self._busy = False
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy

    def run(
        self,
        spec: RunSpec,
        on_done: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Run a command in a background thread, streaming output."""
        if self._busy:
            self._log("⚠  Busy — cannot start: " + spec.label)
            return
        self._busy = True
        self._log("━" * 60)
        self._log(f"▶  {spec.label}")
        self._log(f"   $ {shlex.join(spec.cmd)}")
        self._log("━" * 60)

        def _worker():
            env = os.environ.copy()
            env.setdefault("ANSIBLE_FORCE_COLOR", "1")
            env.setdefault("PY_COLORS", "1")
            env.setdefault("TERM", "xterm-256color")
            if spec.env_extra:
                env.update(spec.env_extra)
            try:
                self._proc = subprocess.Popen(
                    spec.cmd,
                    cwd=str(spec.cwd) if spec.cwd else None,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in self._proc.stdout:  # type: ignore
                    self._log(line.rstrip("\n"))
                self._proc.wait()
                rc = self._proc.returncode
                icon = "✓" if rc == 0 else "✗"
                self._log(f"{icon}  {spec.label} — exit code {rc}")
            except Exception as exc:
                rc = -1
                self._log(f"✗  {spec.label} failed: {exc}")
            finally:
                self._busy = False
                self._proc = None
                if on_done:
                    on_done(rc)

        threading.Thread(target=_worker, daemon=True).start()

    def cancel(self) -> None:
        """Attempt to terminate a running process."""
        if self._proc:
            try:
                self._proc.terminate()
                self._log("⚠  Process terminated by user")
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# MODAL SCREENS
# ═══════════════════════════════════════════════════════════════════════════════
class EnvironmentPickerScreen(ModalScreen[Optional[str]]):
    """Modal for selecting an environment."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Cancel")]

    def __init__(self, envs: list[str], current: Optional[str] = None):
        super().__init__()
        self._envs = envs
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="env-picker-modal"):
            yield Label("Select Environment", id="env-picker-title")
            yield Rule()
            yield OptionList(
                *[
                    Option(
                        f"{'● ' if e == self._current else '  '}{e}",
                        id=e,
                    )
                    for e in self._envs
                ],
                id="env-picker-list",
            )
            with Horizontal(id="env-picker-actions"):
                yield Button("Select", variant="primary", id="env-pick-ok")
                yield Button("Cancel", id="env-pick-cancel")

    def on_mount(self) -> None:
        self.query_one("#env-picker-list", OptionList).focus()

    @on(OptionList.OptionSelected)
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    @on(Button.Pressed, "#env-pick-ok")
    def on_ok(self) -> None:
        ol = self.query_one("#env-picker-list", OptionList)
        if ol.highlighted is not None:
            opt = ol.get_option_at_index(ol.highlighted)
            self.dismiss(opt.id)

    @on(Button.Pressed, "#env-pick-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Simple yes/no confirmation dialog."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Cancel")]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-modal"):
            yield Label(self._title, id="confirm-title")
            yield Rule()
            yield Static(self._body, id="confirm-body")
            with Horizontal(id="confirm-actions"):
                yield Button("Confirm", variant="warning", id="confirm-yes")
                yield Button("Cancel", variant="default", id="confirm-no")

    @on(Button.Pressed, "#confirm-yes")
    def on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def on_no(self) -> None:
        self.dismiss(False)

    def action_dismiss_modal(self) -> None:
        self.dismiss(False)


class InputPromptScreen(ModalScreen[Optional[str]]):
    """Prompt the user for free-text input."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Cancel")]

    def __init__(self, title: str, placeholder: str = ""):
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="input-modal"):
            yield Label(self._title, id="input-title")
            yield Rule()
            yield Input(placeholder=self._placeholder, id="input-field")
            with Horizontal(id="input-actions"):
                yield Button("OK", variant="primary", id="input-ok")
                yield Button("Cancel", id="input-cancel")

    def on_mount(self) -> None:
        self.query_one("#input-field", Input).focus()

    @on(Input.Submitted)
    def on_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)

    @on(Button.Pressed, "#input-ok")
    def on_ok(self) -> None:
        val = self.query_one("#input-field", Input).value
        self.dismiss(val or None)

    @on(Button.Pressed, "#input-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class VarResolverScreen(ModalScreen[Optional[str]]):
    """Prompt user to select/enter a value for a required task variable."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Cancel")]

    def __init__(self, var_name: str, choices: list[str]):
        super().__init__()
        self._var_name = var_name
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="var-modal"):
            yield Label(f"Set variable: {self._var_name}", id="var-title")
            yield Rule()
            if self._choices:
                yield OptionList(
                    *[Option(c, id=c) for c in self._choices],
                    id="var-options",
                )
            else:
                yield Input(
                    placeholder=f"Enter value for {self._var_name}",
                    id="var-input",
                )
            with Horizontal(id="var-actions"):
                yield Button("OK", variant="primary", id="var-ok")
                yield Button("Cancel", id="var-cancel")

    def on_mount(self) -> None:
        if self._choices:
            self.query_one("#var-options", OptionList).focus()
        else:
            self.query_one("#var-input", Input).focus()

    @on(OptionList.OptionSelected)
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    @on(Input.Submitted)
    def on_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)

    @on(Button.Pressed, "#var-ok")
    def on_ok(self) -> None:
        if self._choices:
            ol = self.query_one("#var-options", OptionList)
            if ol.highlighted is not None:
                opt = ol.get_option_at_index(ol.highlighted)
                self.dismiss(opt.id)
        else:
            val = self.query_one("#var-input", Input).value
            self.dismiss(val or None)

    @on(Button.Pressed, "#var-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════
class OpenSpaceTUI(App):
    """OpenSpace Lifecycle Manager — Professional TUI Dashboard."""

    TITLE = "OpenSpace Lifecycle Manager"
    SUB_TITLE = "Infrastructure Operations Console"

    # ── Textual CSS ─────────────────────────────────────────────────────────
    CSS = """
    /* ── Global ────────────────────────────────────── */
    Screen {
        background: $surface;
    }

    /* ── Environment bar ───────────────────────────── */
    #env-bar {
        dock: top;
        height: 3;
        background: $primary-background;
        color: $text;
        padding: 0 2;
        content-align: left middle;
    }
    #env-bar Label {
        margin-right: 2;
    }
    #env-label {
        color: $success;
        text-style: bold;
    }
    #timestamp-label {
        color: $text-muted;
        dock: right;
    }
    #env-switch-btn {
        dock: right;
        margin-right: 1;
        min-width: 18;
    }

    /* ── Tab panes ─────────────────────────────────── */
    TabbedContent {
        height: 1fr;
    }
    TabPane {
        padding: 1 2;
    }

    /* ── Status page ───────────────────────────────── */
    #status-page {
        height: 1fr;
    }
    #health-table {
        height: 1fr;
        max-height: 100%;
    }
    #status-summary {
        height: auto;
        max-height: 8;
        padding: 1;
        border: tall $primary;
        margin-bottom: 1;
    }
    #status-actions {
        height: auto;
        padding: 0 0 1 0;
    }
    #status-actions Button {
        margin-right: 1;
    }
    #poll-status-label {
        margin-left: 2;
        color: $text-muted;
        content-align: left middle;
    }

    /* ── Admin page ────────────────────────────────── */
    #admin-page {
        height: 1fr;
    }
    #admin-layout {
        height: 1fr;
    }
    #admin-left {
        width: 1fr;
        height: 1fr;
    }
    #admin-right {
        width: 1fr;
        height: 1fr;
        margin-left: 1;
    }
    #task-tree {
        height: 1fr;
        border: tall $accent;
        padding: 0 1;
    }
    #admin-detail {
        height: 1fr;
        border: tall $primary;
        padding: 1;
    }
    #admin-actions {
        height: auto;
        padding: 0 0 1 0;
    }
    #admin-actions Button {
        margin-right: 1;
    }
    .section-label {
        text-style: bold;
        margin-bottom: 1;
        color: $text;
    }

    /* ── Troubleshoot page ─────────────────────────── */
    #troubleshoot-content {
        height: 1fr;
        padding: 0;
    }
    #troubleshoot-intro {
        margin-bottom: 1;
        color: $text-muted;
    }
    .troubleshoot-section {
        margin-bottom: 1;
    }
    .troubleshoot-section Label {
        text-style: bold;
        margin-bottom: 1;
    }
    .troubleshoot-section Button {
        margin: 0 1 1 0;
        min-width: 24;
    }

    /* ── Docs page ─────────────────────────────────── */
    #docs-page {
        height: 1fr;
    }
    #docs-layout {
        height: 1fr;
    }
    #docs-left {
        width: 35;
        height: 1fr;
    }
    #docs-right {
        width: 1fr;
        height: 1fr;
        margin-left: 1;
    }
    #docs-file-list {
        height: 1fr;
        border: tall $accent;
    }
    #docs-viewer {
        height: 1fr;
    }

    /* ── Logs page ─────────────────────────────────── */
    #log-pane-content {
        height: 1fr;
    }
    #exec-log {
        height: 1fr;
        border: tall $accent;
    }
    #log-actions {
        height: auto;
        padding: 0 0 1 0;
    }
    #log-actions Button {
        margin-right: 1;
    }
    #log-line-count {
        margin-left: 2;
        color: $text-muted;
        content-align: left middle;
    }

    /* ── Modal screens ─────────────────────────────── */
    EnvironmentPickerScreen,
    ConfirmScreen,
    InputPromptScreen,
    VarResolverScreen {
        align: center middle;
    }
    #env-picker-modal,
    #confirm-modal,
    #input-modal,
    #var-modal {
        width: 60;
        max-width: 80%;
        height: auto;
        max-height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #env-picker-title,
    #confirm-title,
    #input-title,
    #var-title {
        text-style: bold;
        width: 100%;
        content-align: center middle;
        color: $text;
        margin-bottom: 1;
    }
    #env-picker-list,
    #var-options {
        height: auto;
        max-height: 16;
        margin: 1 0;
    }
    #env-picker-actions,
    #confirm-actions,
    #input-actions,
    #var-actions {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #env-picker-actions Button,
    #confirm-actions Button,
    #input-actions Button,
    #var-actions Button {
        margin: 0 1;
    }
    #confirm-body {
        margin: 1 0;
        color: $text-muted;
    }
    """

    # ── Key bindings ────────────────────────────────────────────────────────
    BINDINGS = [
        Binding("1", "switch_tab('status')", "Status", show=True),
        Binding("2", "switch_tab('admin')", "Admin", show=True),
        Binding("3", "switch_tab('troubleshoot')", "Troubleshoot", show=True),
        Binding("4", "switch_tab('docs')", "Docs", show=True),
        Binding("5", "switch_tab('logs')", "Logs", show=True),
        Binding("e", "switch_env", "Switch Env", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("ctrl+c", "cancel_run", "Cancel Task", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    # ── Reactive state ──────────────────────────────────────────────────────
    current_env: reactive[str] = reactive("<none>")
    runner_busy: reactive[bool] = reactive(False)
    last_status_check: reactive[str] = reactive("never")
    active_tab: reactive[str] = reactive("status")

    def __init__(self):
        super().__init__()
        self.runner = Runner(self._stream_log)
        self._env_names: list[str] = []
        self._task_cache: dict[str, str] = {}
        self._health_data: list[ServiceHealth] = []
        self._status_timer: Optional[Timer] = None
        # Raw ansible-inventory --list cache (full dict, not just groups)
        self._inventory_cache: Optional[dict] = None
        self._inventory_cache_env: Optional[str] = None

    def _get_inventory(self) -> Optional[dict]:
        """Return cached raw inventory data, refreshing if env changed."""
        if self.current_env == "<none>":
            return None
        if self._inventory_cache_env != self.current_env:
            self._inventory_cache = get_inventory_data(self.current_env)
            self._inventory_cache_env = self.current_env
            if self._inventory_cache:
                self._stream_log(f"Loaded inventory for {self.current_env}")
            else:
                self._stream_log(
                    f"⚠  Could not load inventory for {self.current_env}"
                )
        return self._inventory_cache

    # ── Compose ─────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="env-bar"):
            yield Label("ENV:", id="env-prefix-label")
            yield Label(self.current_env, id="env-label")
            yield Label("│", id="env-sep")
            yield Label("", id="runner-status-label")
            yield Button("Switch Env", id="env-switch-btn", variant="default")
            yield Label("", id="timestamp-label")
        with TabbedContent(id="main-tabs"):
            with TabPane("⬡ Status", id="status"):
                with Vertical(id="status-page"):
                    with Horizontal(id="status-actions"):
                        yield Button(
                            "⟳ Refresh", id="btn-status-refresh", variant="primary"
                        )
                        yield Button(
                            "▶ Health Check", id="btn-health-check", variant="success"
                        )
                        yield Label("", id="poll-status-label")
                    yield Static("", id="status-summary")
                    yield DataTable(id="health-table")
            with TabPane("⚙ Admin", id="admin"):
                with Vertical(id="admin-page"):
                    with Horizontal(id="admin-actions"):
                        yield Button(
                            "⟳ Reload Tasks", id="btn-admin-reload", variant="default"
                        )
                        yield Button(
                            "▶ Execute Selected",
                            id="btn-admin-exec",
                            variant="success",
                        )
                    with Horizontal(id="admin-layout"):
                        with Vertical(id="admin-left"):
                            yield Label("Task Navigator", classes="section-label")
                            yield Tree("Tasks", id="task-tree")
                        with Vertical(id="admin-right"):
                            yield Label("Task Details", classes="section-label")
                            yield Static(
                                "Select a task from the tree to view details.\n\n"
                                "Use ↑↓ to navigate, Enter to expand/collapse.\n"
                                "Click 'Execute Selected' to run.",
                                id="admin-detail",
                            )
            with TabPane("🔧 Troubleshoot", id="troubleshoot"):
                with VerticalScroll(id="troubleshoot-content"):
                    yield Static(
                        "Quick diagnostics — select an action to run against "
                        "the current environment.",
                        id="troubleshoot-intro",
                    )
                    yield Rule()
                    with Vertical(classes="troubleshoot-section"):
                        yield Label("Connectivity")
                        with HorizontalGroup():
                            yield Button("Ping All Nodes", id="ts-ping-all")
                            yield Button("SSH Reachability", id="ts-ssh-check")
                            yield Button("DNS Resolution", id="ts-dns-check")
                    with Vertical(classes="troubleshoot-section"):
                        yield Label("Cluster Health")
                        with HorizontalGroup():
                            yield Button("RKE2 Status", id="ts-rke2-status")
                            yield Button("Pod Health", id="ts-pod-health")
                            yield Button("Node Resources", id="ts-node-resources")
                    with Vertical(classes="troubleshoot-section"):
                        yield Label("Services")
                        with HorizontalGroup():
                            yield Button("Harbor Registry", id="ts-harbor-check")
                            yield Button("Rancher API", id="ts-rancher-check")
                            yield Button("ArgoCD Sync", id="ts-argocd-check")
                    with Vertical(classes="troubleshoot-section"):
                        yield Label("System")
                        with HorizontalGroup():
                            yield Button("Disk Usage", id="ts-disk-usage")
                            yield Button("Cert Expiry", id="ts-cert-expiry")
                            yield Button("NTP Sync", id="ts-ntp-check")
                    with Vertical(classes="troubleshoot-section"):
                        yield Label("Log Collection")
                        with HorizontalGroup():
                            yield Button("Collect Journals", id="ts-collect-journals")
                            yield Button("K8s Events", id="ts-k8s-events")
            with TabPane("📄 Docs", id="docs"):
                with Vertical(id="docs-page"):
                    with Horizontal(id="docs-layout"):
                        with Vertical(id="docs-left"):
                            yield Label("Documents", classes="section-label")
                            yield OptionList(id="docs-file-list")
                        with VerticalScroll(id="docs-right"):
                            yield Label("Viewer", classes="section-label")
                            yield Markdown(
                                "Select a document from the list.",
                                id="docs-viewer",
                            )
            with TabPane("📋 Logs", id="logs"):
                with Vertical(id="log-pane-content"):
                    with Horizontal(id="log-actions"):
                        yield Button(
                            "Clear Log", id="btn-log-clear", variant="default"
                        )
                        yield Button(
                            "Cancel Task", id="btn-log-cancel", variant="error"
                        )
                        yield Label("", id="log-line-count")
                    yield RichLog(
                        id="exec-log",
                        wrap=True,
                        markup=True,
                        highlight=True,
                        max_lines=5000,
                    )
        yield Footer()

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self._init_health_table()
        self.run_worker(self._startup(), exclusive=True, group="startup")

    async def _startup(self) -> None:
        self._env_names = list_envs()
        self._stream_log("OpenSpace Lifecycle Manager starting...")
        self._stream_log(f"Root: {OPENSPACE_ROOT}")

        # Resolve environment
        current = get_current_env()
        if current:
            self.current_env = current
            self._stream_log(f"Environment: {current}")
        elif len(self._env_names) == 1:
            env = self._env_names[0]
            try:
                set_current_env(env)
            except Exception:
                pass
            self.current_env = env
            self._stream_log(f"Auto-selected environment: {env}")
        elif len(self._env_names) > 1:
            selected = await self.push_screen_wait(
                EnvironmentPickerScreen(self._env_names)
            )
            if selected:
                try:
                    set_current_env(selected)
                except Exception:
                    pass
                self.current_env = selected
                self._stream_log(f"Selected environment: {selected}")
            else:
                self._stream_log("⚠  No environment selected")
        else:
            self._stream_log(
                "⚠  No environments found in " + str(OPENSPACE_ROOT / "env")
            )

        self._reload_tasks()
        self._refresh_status_display()
        self._populate_docs_list()
        self._start_status_polling()
        self._stream_log("Ready.")

    def _start_status_polling(self) -> None:
        if self._status_timer:
            self._status_timer.stop()
        if STATUS_POLL_INTERVAL > 0:
            self._status_timer = self.set_interval(
                STATUS_POLL_INTERVAL, self._poll_status, name="status-poll"
            )
            try:
                self.query_one("#poll-status-label", Label).update(
                    f"Auto-refresh: {STATUS_POLL_INTERVAL}s"
                )
            except Exception:
                pass

    def _poll_status(self) -> None:
        self._refresh_status_display()

    # ── Environment management ──────────────────────────────────────────────
    def watch_current_env(self, value: str) -> None:
        try:
            self.query_one("#env-label", Label).update(value)
        except Exception:
            pass
        self._inventory_cache = None
        self._inventory_cache_env = None

    async def action_switch_env(self) -> None:
        self._env_names = list_envs()
        if not self._env_names:
            self.notify("No environments found", severity="warning")
            return
        selected = await self.push_screen_wait(
            EnvironmentPickerScreen(self._env_names, self.current_env)
        )
        if selected and selected != self.current_env:
            try:
                set_current_env(selected)
            except Exception:
                pass
            self.current_env = selected
            self._stream_log(f"Switched to environment: {selected}")
            self._reload_tasks()
            self._refresh_status_display()
            self.notify(f"Environment: {selected}", severity="information")

    @on(Button.Pressed, "#env-switch-btn")
    def on_env_switch_btn(self) -> None:
        self.run_worker(self.action_switch_env())

    # ── Tab navigation ──────────────────────────────────────────────────────
    def action_switch_tab(self, tab_id: str) -> None:
        tabs = self.query_one("#main-tabs", TabbedContent)
        tabs.active = tab_id
        self.active_tab = tab_id

    # ── Status tab ──────────────────────────────────────────────────────────
    def _init_health_table(self) -> None:
        try:
            table = self.query_one("#health-table", DataTable)
            table.add_columns("Component", "Status", "Detail", "Last Check")
            table.cursor_type = "row"
            table.zebra_stripes = True
        except Exception:
            pass

    def _refresh_status_display(self) -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self.last_status_check = now

        env_display = (
            self.current_env if self.current_env != "<none>" else "NOT SET"
        )
        task_count = len(self._task_cache)

        summary_lines = [
            f"Environment: {env_display}  │  "
            f"Environments available: {len(self._env_names)}  │  "
            f"Tasks loaded: {task_count}",
            f"Last check: {now}  │  "
            f"Runner: {'BUSY' if self.runner.busy else 'idle'}",
        ]

        if self.current_env != "<none>":
            tf = resolve_env_taskfile(self.current_env)
            inv = OPENSPACE_ROOT / "env" / self.current_env / "deployment.yml"
            summary_lines.append(
                f"Taskfile: {'✓' if tf else '✗'}  │  "
                f"Inventory: {'✓' if inv.exists() else '✗'}"
            )

        try:
            self.query_one("#status-summary", Static).update(
                "\n".join(summary_lines)
            )
        except Exception:
            pass

        self._health_data = self._gather_health_indicators()
        self._refresh_health_table()

        try:
            rl = self.query_one("#runner-status-label", Label)
            rl.update("⏳ RUNNING" if self.runner.busy else "● IDLE")
        except Exception:
            pass

    def _gather_health_indicators(self) -> list[ServiceHealth]:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        indicators: list[ServiceHealth] = []

        env_ok = self.current_env != "<none>"
        indicators.append(
            ServiceHealth(
                "Environment",
                "ok" if env_ok else "down",
                self.current_env if env_ok else "No environment selected",
                now,
            )
        )

        if not env_ok:
            return indicators

        env_dir = OPENSPACE_ROOT / "env" / self.current_env

        tf = env_dir / "Taskfile.yml"
        indicators.append(
            ServiceHealth(
                "Taskfile",
                "ok" if tf.exists() else "down",
                str(tf) if tf.exists() else "Missing",
                now,
            )
        )

        inv = env_dir / "deployment.yml"
        indicators.append(
            ServiceHealth(
                "Inventory",
                "ok" if inv.exists() else "down",
                str(inv) if inv.exists() else "Missing",
                now,
            )
        )

        # Discover inventory groups
        groups = self._get_inventory_groups()
        if groups:
            for group_name, hosts in sorted(groups.items()):
                if group_name in ("all", "ungrouped"):
                    continue
                host_preview = ", ".join(hosts[:3])
                if len(hosts) > 3:
                    host_preview += "..."
                indicators.append(
                    ServiceHealth(
                        f"Group: {group_name}",
                        "unknown",
                        f"{len(hosts)} host(s): {host_preview}",
                        now,
                    )
                )
        else:
            indicators.append(
                ServiceHealth(
                    "Inventory Groups", "unknown", "Could not parse inventory", now
                )
            )

        for label, path in [
            ("Automation Dir", LCM_DIR),
            ("Docs Dir", DOCS_DIR),
            ("Log Dir", LOG_DIR),
        ]:
            indicators.append(
                ServiceHealth(
                    label,
                    "ok" if path.exists() else "unknown",
                    str(path),
                    now,
                )
            )

        return indicators

    def _refresh_health_table(self) -> None:
        try:
            table = self.query_one("#health-table", DataTable)
            table.clear()
            for svc in self._health_data:
                status_display = {
                    "ok": "[green]● OK[/green]",
                    "degraded": "[yellow]◐ DEGRADED[/yellow]",
                    "down": "[red]● DOWN[/red]",
                    "unknown": "[dim]○ UNKNOWN[/dim]",
                }.get(svc.status, "[dim]?[/dim]")
                table.add_row(
                    svc.name,
                    Text.from_markup(status_display),
                    svc.detail,
                    svc.last_check,
                )
        except Exception:
            pass

    def _get_inventory_groups(self) -> dict[str, list[str]]:
        """Derive group -> hosts mapping from the raw inventory cache."""
        inv = self._get_inventory()
        if not inv:
            return {}
        groups: dict[str, list[str]] = {}
        for group_name, group_data in inv.items():
            if group_name == "_meta":
                continue
            if isinstance(group_data, dict) and "hosts" in group_data:
                hosts = group_data["hosts"]
                if hosts:
                    groups[group_name] = (
                        hosts if isinstance(hosts, list) else list(hosts)
                    )
        return groups

    @on(Button.Pressed, "#btn-status-refresh")
    def on_status_refresh(self) -> None:
        self._refresh_status_display()
        self.notify("Status refreshed", severity="information")

    @on(Button.Pressed, "#btn-health-check")
    def on_health_check(self) -> None:
        if self.current_env == "<none>":
            self.notify("Select an environment first", severity="warning")
            return
        if self.runner.busy:
            self.notify("A task is already running", severity="warning")
            return

        tf = resolve_env_taskfile(self.current_env)

        # Try to find a status task in the loaded tasks (case-insensitive)
        status_task = None
        if tf and self._task_cache:
            for t in self._task_cache:
                if t.lower() in ("status", "status:all", "health", "health:all"):
                    status_task = t
                    break

        if status_task and tf:
            self.runner.run(
                RunSpec("Health Check", ["task", "-t", str(tf), status_task]),
                on_done=lambda rc: self.call_from_thread(
                    self._refresh_status_display
                ),
            )
        elif STATUS_PLAYBOOK.exists():
            inv = OPENSPACE_ROOT / "env" / self.current_env / "deployment.yml"
            cmd = ["ansible-playbook", "-i", str(inv), str(STATUS_PLAYBOOK)]
            self.runner.run(
                RunSpec("Health Check", cmd),
                on_done=lambda rc: self.call_from_thread(
                    self._refresh_status_display
                ),
            )
        else:
            self.notify("No status task or playbook found", severity="warning")
            return
        self.action_switch_tab("logs")

    # ── Admin tab ───────────────────────────────────────────────────────────
    def _reload_tasks(self) -> None:
        self._task_cache = {}
        if self.current_env == "<none>":
            return
        tf = resolve_env_taskfile(self.current_env)
        if not tf:
            self._stream_log(f"⚠  No Taskfile.yml in {self.current_env}")
            return
        self._task_cache = task_descriptions(tf)
        self._stream_log(f"Loaded {len(self._task_cache)} tasks from {tf.name}")
        self._populate_task_tree()

    def _populate_task_tree(self) -> None:
        try:
            tree = self.query_one("#task-tree", Tree)
        except Exception:
            return
        tree.clear()
        tree.root.expand()
        if not self._task_cache:
            tree.root.add_leaf("(no tasks loaded)")
            return
        nested = build_task_tree(self._task_cache)
        self._add_tree_nodes(tree.root, nested)

    def _add_tree_nodes(self, parent: TreeNode, subtree: dict) -> None:
        for key, value in sorted(subtree.items()):
            if key.startswith("__"):
                continue
            if not isinstance(value, dict):
                continue
            desc = value.get("__desc__", "")
            task_name = value.get("__task__", "")
            children = {k: v for k, v in value.items() if not k.startswith("__")}

            if children:
                label = f"{key}  [dim]{desc}[/]" if desc else key
                node = parent.add(label, data={"task": task_name, "desc": desc})
                self._add_tree_nodes(node, children)
            else:
                label = f"{key}  [dim]{desc}[/]" if desc else key
                parent.add_leaf(
                    label, data={"task": task_name, "desc": desc}
                )

    @on(Tree.NodeHighlighted, "#task-tree")
    def on_task_tree_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Show task details in the side panel when a tree node is highlighted."""
        data = event.node.data
        if not data or not data.get("task"):
            try:
                self.query_one("#admin-detail", Static).update(
                    "Select a leaf task to view details and execute."
                )
            except Exception:
                pass
            return
        task_name = data["task"]
        desc = data.get("desc", "")

        required_vars: list[str] = []
        if self.current_env != "<none>":
            tf = resolve_env_taskfile(self.current_env)
            if tf:
                required_vars = get_task_required_vars(tf, task_name)

        # Show what choices will be offered for each var
        inv = self._get_inventory()
        resolver = VarResolver(inv) if inv else None
        var_details: list[str] = []
        for vn in required_vars:
            choices = resolver.get_choices(vn) if resolver else []
            if choices:
                var_details.append(
                    f"  {vn}: select from [{', '.join(choices[:5])}"
                    + (f", +{len(choices)-5} more" if len(choices) > 5 else "")
                    + "]"
                )
            else:
                var_details.append(f"  {vn}: free text input")

        lines = [
            f"Task:  {task_name}",
            f"Desc:  {desc}" if desc else "",
            "",
        ]
        if required_vars:
            lines.append(f"Required variables ({len(required_vars)}):")
            lines.extend(var_details)
        else:
            lines.append("Required variables: none")

        # Check if routing would apply
        for orig_task, var_name, match_val, alt_task, _alt_vars in self.TASK_ROUTES:
            if task_name == orig_task:
                lines.append("")
                lines.append(
                    f"Route: if {var_name} resolves to "
                    f"cluster_type={match_val} → {alt_task}"
                )

        lines.extend(["", "Press Enter or click 'Execute Selected' to run."])
        try:
            self.query_one("#admin-detail", Static).update(
                "\n".join(l for l in lines if l is not None)
            )
        except Exception:
            pass

    @on(Tree.NodeSelected, "#task-tree")
    def on_task_tree_enter(self, event: Tree.NodeSelected) -> None:
        """Execute a leaf task when Enter/click is pressed on it."""
        data = event.node.data
        if not data or not data.get("task"):
            return
        # Only execute on leaf nodes (branch nodes expand/collapse)
        if event.node.children:
            return
        self.run_worker(self._execute_selected_task())

    @on(Button.Pressed, "#btn-admin-reload")
    def on_admin_reload(self) -> None:
        self._reload_tasks()
        self.notify("Tasks reloaded", severity="information")

    @on(Button.Pressed, "#btn-admin-exec")
    def on_admin_exec(self) -> None:
        self.run_worker(self._execute_selected_task())

    # ── Task routing table ──────────────────────────────────────────────────
    # Routes redirect a task to a different task based on collected variable
    # values.  Each entry is:
    #   (original_task, var_name, match_value, redirect_task, override_vars)
    #
    # - match_value is compared case-insensitively against the group's
    #   cluster_type hostvar (looked up from inventory), or against the
    #   raw selected value.
    # - override_vars: vars to pass to the alternate task (empty dict = none).
    #
    # Checked AFTER all vars are collected but BEFORE execution.

    TASK_ROUTES: list[tuple[str, str, str, str, dict[str, str]]] = [
        # When deploying a cluster, if the selected group has
        # cluster_type == mcm, redirect to the onboarder task.
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

        For CLUSTER_TYPE, we look up the selected group's cluster_type from
        inventory.  ansible-inventory --list can place group vars in several
        locations depending on version and inventory plugin:

          1. Direct match: selected value IS the match value
          2. group_data["vars"]["cluster_type"] — some plugins
          3. _meta.hostvars[host]["cluster_type"] — merged onto hosts

        Returns (alternate_task, override_vars) or None.
        """
        inv = self._get_inventory()

        for orig_task, var_name, match_val, alt_task, alt_vars in self.TASK_ROUTES:
            if task_name != orig_task:
                continue
            selected = task_vars.get(var_name, "")
            if not selected:
                continue

            # Strategy 1: Direct match
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

    # ── Task execution with variable resolution ─────────────────────────────
    async def _execute_selected_task(self) -> None:
        """Resolve variables using VarResolver, check routes, then execute."""
        try:
            tree = self.query_one("#task-tree", Tree)
        except Exception:
            return

        node = tree.cursor_node
        if not node or not node.data or not node.data.get("task"):
            self.notify("Select a task first", severity="warning")
            return

        if self.runner.busy:
            self.notify("A task is already running", severity="warning")
            return

        task_name = node.data["task"]

        if self.current_env == "<none>":
            self.notify("Select an environment first", severity="warning")
            return

        tf = resolve_env_taskfile(self.current_env)
        if not tf:
            self.notify("No Taskfile found", severity="error")
            return

        # Set up VarResolver with inventory
        inv = self._get_inventory()
        resolver = VarResolver(inv)
        required_vars = get_task_required_vars(tf, task_name, debug_log=self._stream_log)

        self._stream_log(
            f"Task: {task_name}  "
            f"requires: {', '.join(required_vars) if required_vars else 'none'}"
        )

        if not required_vars:
            self._stream_log(
                "   (no vars found — skipping selection, running task directly)"
            )

        # Collect all required variables
        collected_vars: dict[str, str] = {}

        for var_name in required_vars:
            # Skip if already satisfied by a previous resolution
            if var_name in collected_vars:
                continue

            choices = resolver.get_choices(var_name)
            result = await self.push_screen_wait(
                VarResolverScreen(var_name, choices)
            )
            if result is None:
                self._stream_log(f"⚠  Cancelled — no value for {var_name}")
                return

            # Resolve may set multiple vars (e.g. CLUSTER_TYPE + CLUSTER_GROUP)
            resolved = resolver.resolve(var_name, result)
            collected_vars.update(resolved)

            extra = {k: v for k, v in resolved.items() if k != var_name}
            self._stream_log(
                f"  {var_name} = {result}"
                + (
                    f" (also set: {', '.join(f'{k}={v}' for k, v in extra.items())})"
                    if extra
                    else ""
                )
            )

        # Check for task routing (e.g. mcm → Onboarder)
        routed = self._check_task_route(task_name, collected_vars)
        if routed:
            task_name, collected_vars = routed[0], routed[1]
            self._stream_log(f"  Routed → {task_name}")

        # Build confirmation message
        confirm_body = f"Run: {task_name}\nEnvironment: {self.current_env}"
        if collected_vars:
            var_summary = "\n".join(
                f"  {k} = {v}" for k, v in collected_vars.items()
            )
            confirm_body += f"\nVariables:\n{var_summary}"

        confirmed = await self.push_screen_wait(
            ConfirmScreen("Execute Task", confirm_body)
        )
        if not confirmed:
            return

        # Build go-task command
        cmd = ["task", "-t", str(tf), task_name]
        for k, v in collected_vars.items():
            cmd.append(f"{k}={v}")

        # Update detail panel to show execution status
        self._update_detail_running(task_name, collected_vars)

        self.runner.run(
            RunSpec(f"Task: {task_name}", cmd),
            on_done=lambda rc: self.call_from_thread(
                self._update_detail_done, task_name, rc
            ),
        )
        self.action_switch_tab("logs")

    def _update_detail_running(
        self, task_name: str, task_vars: dict[str, str]
    ) -> None:
        """Update the admin detail panel to show a task is running."""
        var_lines = (
            "\n".join(f"  {k} = {v}" for k, v in task_vars.items())
            if task_vars
            else "  (none)"
        )
        try:
            self.query_one("#admin-detail", Static).update(
                f"⏳ RUNNING\n\n"
                f"Task:  {task_name}\n"
                f"Env:   {self.current_env}\n\n"
                f"Variables:\n{var_lines}\n\n"
                f"Watch the Logs tab for output.\n"
                f"Press Ctrl+C to cancel."
            )
        except Exception:
            pass

    def _update_detail_done(self, task_name: str, rc: int) -> None:
        """Update the admin detail panel when a task finishes."""
        icon = "✓" if rc == 0 else "✗"
        status = "SUCCEEDED" if rc == 0 else f"FAILED (exit {rc})"
        try:
            self.query_one("#admin-detail", Static).update(
                f"{icon} {status}\n\n"
                f"Task:  {task_name}\n\n"
                f"Select another task to continue."
            )
        except Exception:
            pass

    # ── Troubleshoot tab ────────────────────────────────────────────────────
    @on(Button.Pressed, ".troubleshoot-section Button")
    def on_troubleshoot_action(self, event: Button.Pressed) -> None:
        if self.runner.busy:
            self.notify("A task is already running", severity="warning")
            return
        if self.current_env == "<none>":
            self.notify("Select an environment first", severity="warning")
            return

        btn_id = event.button.id or ""
        inv = OPENSPACE_ROOT / "env" / self.current_env / "deployment.yml"
        inv_arg = str(inv) if inv.exists() else ""

        # Each troubleshoot button maps to Taskfile task patterns (tried
        # in order) with a fallback ad-hoc Ansible command.
        TS_MAP: dict[str, dict[str, Any]] = {
            "ts-ping-all": {
                "patterns": ["Troubleshoot:Ping", "Debug:Ping", "Status:Ping"],
                "fallback": ["ansible", "all", "-i", inv_arg, "-m", "ping"],
                "label": "Ping All Nodes",
            },
            "ts-ssh-check": {
                "patterns": ["Troubleshoot:SSH", "Debug:SSH"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "raw", "-a", "uptime",
                ],
                "label": "SSH Reachability",
            },
            "ts-dns-check": {
                "patterns": ["Troubleshoot:DNS", "Debug:DNS"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a", "nslookup $(hostname)",
                ],
                "label": "DNS Resolution",
            },
            "ts-rke2-status": {
                "patterns": ["Troubleshoot:RKE2", "Status:RKE2", "Debug:RKE2"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a",
                    "systemctl status rke2-server rke2-agent 2>/dev/null "
                    "|| echo 'RKE2 not installed'",
                ],
                "label": "RKE2 Status",
            },
            "ts-pod-health": {
                "patterns": ["Troubleshoot:Pods", "Status:Pods", "Debug:Pods"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a",
                    "kubectl get pods -A --no-headers 2>/dev/null "
                    "| grep -v Running | head -20 "
                    "|| echo 'kubectl not available'",
                ],
                "label": "Pod Health",
            },
            "ts-node-resources": {
                "patterns": ["Troubleshoot:Resources", "Status:Resources"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a",
                    "echo '--- CPU ---' && nproc && "
                    "echo '--- MEM ---' && free -h | head -2 && "
                    "echo '--- DISK ---' && df -h / | tail -1",
                ],
                "label": "Node Resources",
            },
            "ts-harbor-check": {
                "patterns": ["Troubleshoot:Harbor", "Status:Harbor"],
                "fallback": ["echo", "No Harbor check task configured — add Troubleshoot:Harbor to your Taskfile"],
                "label": "Harbor Registry",
            },
            "ts-rancher-check": {
                "patterns": ["Troubleshoot:Rancher", "Status:Rancher"],
                "fallback": ["echo", "No Rancher check task configured — add Troubleshoot:Rancher to your Taskfile"],
                "label": "Rancher API",
            },
            "ts-argocd-check": {
                "patterns": ["Troubleshoot:ArgoCD", "Status:ArgoCD"],
                "fallback": ["echo", "No ArgoCD check task configured — add Troubleshoot:ArgoCD to your Taskfile"],
                "label": "ArgoCD Sync Status",
            },
            "ts-disk-usage": {
                "patterns": ["Troubleshoot:Disk", "Status:Disk"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a",
                    "df -h | head -1 && df -h | grep -E '^/dev' "
                    "| sort -k5 -rn",
                ],
                "label": "Disk Usage",
            },
            "ts-cert-expiry": {
                "patterns": ["Troubleshoot:Certs", "Status:Certs"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a",
                    "find /etc/ssl /etc/pki -name '*.crt' "
                    "-exec openssl x509 -enddate -noout -in {} \\; "
                    "2>/dev/null | head -10 || echo 'No certs found'",
                ],
                "label": "Certificate Expiry",
            },
            "ts-ntp-check": {
                "patterns": ["Troubleshoot:NTP", "Status:NTP"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a", "chronyc tracking 2>/dev/null || timedatectl status",
                ],
                "label": "NTP Sync",
            },
            "ts-collect-journals": {
                "patterns": ["Troubleshoot:Journals", "Debug:Journals"],
                "fallback": [
                    "ansible", "all", "-i", inv_arg, "-m", "shell",
                    "-a",
                    "journalctl --since '1 hour ago' --priority err "
                    "--no-pager | tail -30",
                ],
                "label": "Collect Journals",
            },
            "ts-k8s-events": {
                "patterns": ["Troubleshoot:Events", "Status:Events"],
                "fallback": [
                    "echo",
                    "No K8s events task configured — add Troubleshoot:Events to your Taskfile",
                ],
                "label": "K8s Events",
            },
        }

        mapping = TS_MAP.get(btn_id)
        if not mapping:
            self.notify(f"Unknown action: {btn_id}", severity="error")
            return

        label = mapping["label"]
        tf = resolve_env_taskfile(self.current_env)

        # Try matching Taskfile tasks first
        if tf and self._task_cache:
            for pattern in mapping["patterns"]:
                for loaded_task in self._task_cache:
                    if loaded_task.lower() == pattern.lower():
                        self.runner.run(
                            RunSpec(label, ["task", "-t", str(tf), loaded_task])
                        )
                        self.action_switch_tab("logs")
                        return

        # Fallback to ad-hoc command
        cmd = mapping["fallback"]
        if not inv_arg:
            self.notify("No inventory file found", severity="warning")
            return
        self.runner.run(RunSpec(label, cmd))
        self.action_switch_tab("logs")

    # ── Docs tab ────────────────────────────────────────────────────────────
    def _populate_docs_list(self) -> None:
        try:
            ol = self.query_one("#docs-file-list", OptionList)
        except Exception:
            return
        ol.clear_options()
        docs = discover_docs()
        if not docs:
            ol.add_option(Option("(no documents found)", id="__none__"))
            return
        for doc in docs:
            try:
                rel = doc.relative_to(OPENSPACE_ROOT)
                display = str(rel)
            except ValueError:
                display = doc.name
            ol.add_option(Option(display, id=str(doc)))

    @on(OptionList.OptionSelected, "#docs-file-list")
    def on_doc_selected(self, event: OptionList.OptionSelected) -> None:
        filepath = event.option.id
        if not filepath or filepath == "__none__":
            return
        path = Path(str(filepath))
        if not path.exists():
            self.notify(f"File not found: {filepath}", severity="error")
            return
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            self.query_one("#docs-viewer", Markdown).update(content)
        except Exception as e:
            self.notify(f"Error reading file: {e}", severity="error")

    # ── Logs tab ────────────────────────────────────────────────────────────
    def _stream_log(self, message: str) -> None:
        """Write a line to the execution log. Thread-safe."""

        def _write():
            try:
                log = self.query_one("#exec-log", RichLog)
                ts = datetime.now().strftime("%H:%M:%S")
                log.write(f"[dim]{ts}[/dim] {rich_escape(message)}")
                try:
                    self.query_one("#log-line-count", Label).update(
                        f"Lines: {log.line_count}"
                    )
                except Exception:
                    pass
            except Exception:
                pass

        try:
            self.call_from_thread(_write)
        except RuntimeError:
            # Called from main thread during startup
            try:
                log = self.query_one("#exec-log", RichLog)
                ts = datetime.now().strftime("%H:%M:%S")
                log.write(f"[dim]{ts}[/dim] {rich_escape(message)}")
            except Exception:
                pass

    @on(Button.Pressed, "#btn-log-clear")
    def on_log_clear(self) -> None:
        try:
            self.query_one("#exec-log", RichLog).clear()
            self.notify("Log cleared", severity="information")
        except Exception:
            pass

    @on(Button.Pressed, "#btn-log-cancel")
    def on_log_cancel(self) -> None:
        if self.runner.busy:
            self.runner.cancel()
            self.notify("Task cancelled", severity="warning")
        else:
            self.notify("No task running", severity="information")

    # ── Global actions ──────────────────────────────────────────────────────
    def action_refresh(self) -> None:
        tab = self.active_tab
        if tab == "status":
            self._refresh_status_display()
        elif tab == "admin":
            self._reload_tasks()
        elif tab == "docs":
            self._populate_docs_list()
        self.notify("Refreshed", severity="information")

    def action_cancel_run(self) -> None:
        if self.runner.busy:
            self.runner.cancel()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    app = OpenSpaceTUI()
    app.run()


if __name__ == "__main__":
    main()