#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required ENV: {name}")
    return val


class StateManager:
    """Manages deployment state for resume capability"""

    def __init__(self):
        work_dir = Path(_require_env("OLM_WORK_DIR")).expanduser().resolve()

        # state.json is now directly in /openspace (work dir)
        self.state_file = work_dir / "state.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        """Load existing state or create new"""
        if self.state_file.exists():
            with open(self.state_file, "r") as f:
                return json.load(f)
        return {
            "tasks": {},
            "last_run": None,
            "status": "not_started",
        }

    def _save_state(self):
        """Persist state to disk"""
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def is_completed(self, task_id: str) -> bool:
        """Check if task is already completed"""
        return self.state["tasks"].get(task_id, {}).get("status") == "completed"

    def mark_started(self, task_id: str):
        """Mark task as started"""
        self.state["tasks"][task_id] = {
            "status": "running",
            "started_at": datetime.now().isoformat(),
        }
        self.state["last_run"] = task_id
        self._save_state()

    def mark_completed(self, task_id: str):
        """Mark task as completed"""
        self.state["tasks"][task_id]["status"] = "completed"
        self.state["tasks"][task_id]["completed_at"] = datetime.now().isoformat()
        self._save_state()

    def mark_failed(self, task_id: str, error: str):
        """Mark task as failed"""
        self.state["tasks"][task_id]["status"] = "failed"
        self.state["tasks"][task_id]["error"] = error
        self.state["tasks"][task_id]["failed_at"] = datetime.now().isoformat()
        self._save_state()

    def get_last_incomplete_task(self) -> Optional[str]:
        """Get the last task that wasn't completed"""
        return self.state.get("last_run")


class TaskLogger:
    """Handles console logging + pretty printing (NO file logs)"""

    # ANSI color codes
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    NC = "\033[0m"  # No Color

    def __init__(self, task_id: str):
        self.task_id = task_id

        # Setup console-only logger
        self.logger = logging.getLogger(f"task.{task_id}")
        self.logger.setLevel(logging.INFO)

        # Avoid duplicate handlers if multiple instances created
        if not self.logger.handlers:
            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)

            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            ch.setFormatter(formatter)

            self.logger.addHandler(ch)
            self.logger.propagate = False

    def print_banner(self, title: str, width: int = 68):
        """Print a fancy banner for task separation with centered text"""
        content_width = width - 4
        title_len = len(title)

        if title_len > content_width:
            title = title[:content_width]
            title_len = content_width

        total_padding = content_width - title_len
        left_padding = total_padding // 2
        right_padding = total_padding - left_padding

        border = "═" * (width - 2)
        print("")
        print(f"{self.CYAN}╔{border}╗{self.NC}")
        print(f"{self.CYAN}║ {' ' * left_padding}{title}{' ' * right_padding} ║{self.NC}")
        print(f"{self.CYAN}╚{border}╝{self.NC}")
        print("")

    def print_success(self, message: str):
        """Print success message"""
        print(f"{self.GREEN}✓ {message}{self.NC}")

    def print_error(self, message: str):
        """Print error message"""
        print(f"{self.RED}✗ {message}{self.NC}")

    def print_warning(self, message: str):
        """Print warning message"""
        print(f"{self.YELLOW}⚠ {message}{self.NC}")

    def print_separator(self):
        """Print a simple separator line"""
        print(f"{self.CYAN}{'─' * 68}{self.NC}")

    def info(self, msg: str):
        self.logger.info(msg)

    def error(self, msg: str):
        self.logger.error(msg)

    def warning(self, msg: str):
        self.logger.warning(msg)


class TaskExecutor:
    """Executes different types of tasks"""

    def __init__(self, logger: TaskLogger):
        self.logger = logger

        # Updated to ENV-based paths (same as before)
        self.data_dir = Path(_require_env("OLM_DATA_DIR")).expanduser().resolve()
        self.install_dir = Path(_require_env("OLM_WORK_DIR")).expanduser().resolve()

    def execute(self, task_id: str, kind: str, **kwargs):
        """Execute a task based on its kind"""
        self.logger.info(f"Executing task: {task_id} (kind: {kind})")

        if kind == "ansible":
            return self._execute_ansible(task_id, **kwargs)
        elif kind == "shell":
            return self._execute_shell(task_id, **kwargs)
        else:
            raise ValueError(f"Unknown task kind: {kind}")

    def _execute_ansible(self, task_id: str, hosts: str, file: str, args: str = "", **kwargs):
        """Execute an Ansible playbook"""
        env_vars = os.environ.copy()
        env_vars["ANSIBLE_CONFIG"] = str(Path(_require_env("OLM_ANSIBLE_CFG")).expanduser().resolve())

        inventory_path = Path(_require_env("OLM_INVENTORY_YAML")).expanduser().resolve()
        playbook_path = Path(file)

        cmd = [
            "ansible-playbook",
            "-i", str(inventory_path),
            str(playbook_path),
            "-e", f"target_hosts={hosts}",
            "-e", "env_name=install",
        ]

        if args:
            cmd.extend(args.split())

        self.logger.info(f"Running: {' '.join(cmd)}")

        try:
            subprocess.run(
                cmd,
                env=env_vars,
                check=True,
                text=True
            )
            self.logger.info(f"Task {task_id} completed successfully")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Task {task_id} failed with exit code {e.returncode}")
            raise

    def _execute_shell(self, task_id: str, command: str, **kwargs):
        """Execute a shell command"""
        self.logger.info(f"Running shell command: {command}")

        try:
            subprocess.run(
                command,
                shell=True,
                check=True,
                text=True
            )
            self.logger.info(f"Task {task_id} completed successfully")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Task {task_id} failed with exit code {e.returncode}")
            raise


def main():
    parser = argparse.ArgumentParser(description="Run deployment tasks with state management")
    parser.add_argument("--task-id", help="Task ID to execute")
    parser.add_argument("--kind", help="Task kind (ansible, shell)")
    parser.add_argument("--hosts", help="Target hosts for ansible")
    parser.add_argument("--file", help="Ansible playbook file path")
    parser.add_argument("--args", default="", help="Additional arguments")
    parser.add_argument("--command", help="Shell command to execute")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")

    args = parser.parse_args()

    # Initialize state manager
    state = StateManager()

    # Initialize logger (console only)
    logger = TaskLogger(args.task_id or "setup")

    # Handle resume
    if args.resume:
        last_task = state.get_last_incomplete_task()
        if not last_task:
            logger.print_banner("Resume Check")
            print("No incomplete tasks to resume")
            return 0
        logger.print_banner("Resume Failed")
        logger.print_error(f"Cannot auto-resume - please run the failed task manually: {last_task}")
        return 1

    # Validate task parameters
    if not args.task_id:
        logger.print_error("--task-id is required")
        return 1

    # Check if already completed
    if state.is_completed(args.task_id):
        logger.print_banner(f"Task: {args.task_id}")
        print("Task already completed, skipping...")
        return 0

    # Print task banner
    logger.print_banner(f"Executing Task: {args.task_id}")

    # Initialize executor
    executor = TaskExecutor(logger)

    # Mark task as started
    state.mark_started(args.task_id)

    try:
        executor.execute(
            task_id=args.task_id,
            kind=args.kind,
            hosts=args.hosts,
            file=args.file,
            args=args.args,
            command=args.command
        )

        state.mark_completed(args.task_id)
        logger.print_separator()
        logger.print_success(f"Task {args.task_id} completed successfully")
        logger.print_separator()
        return 0

    except Exception as e:
        state.mark_failed(args.task_id, str(e))
        logger.print_separator()
        logger.print_error(f"Task {args.task_id} failed: {str(e)}")
        logger.print_separator()
        return 1


if __name__ == "__main__":
    sys.exit(main())---
- name: Bootstrap mgmt-kvm server
  hosts: "{{ target_hosts | default('mgmt_kvm') }}"
  become: true

#  pre_tasks:
#    - name: Ping hosts
#      import_tasks: ../common/ping_hosts.yml
#      vars:
#        target_hosts: "{{ target_hosts }}" 

  tasks:
    - name: Ensure bridge connections exist and are configured
      community.general.nmcli:
        conn_name: "{{ item['con-name'] }}"
        ifname: "{{ item.ifname }}"
        type: "{{ item.type | default('bridge') }}"
        state: present
        autoconnect: yes
        method4: manual
        ip4: "{{ item.address | default(omit) }}"
        gw4: "{{ item.gateway | default(omit) }}"
        dns4: "{{ (item.dns is string) | ternary([item.dns], item.dns | default(omit)) }}"
        dns4_search: ["{{ domain_name }}"]
        method6: disabled
      loop: "{{ interfaces }}"

    - name: Create/attach bridge-slave profiles for member NICs
      community.general.nmcli:
        conn_name: "{{ item.0['con-name'] }}-{{ item.1 }}"
        ifname: "{{ item.1 }}"
        type: bridge-slave
        master: "{{ item.0['con-name'] }}"
        autoconnect: yes
        method4: disabled
        method6: disabled
        state: present
      loop: "{{ interfaces | subelements('interfaces', skip_missing=True) }}"

    - name: Remove existing physical interface connections
      community.general.nmcli:
        conn_name: "{{ item.1 }}"
        state: absent
      loop: "{{ interfaces | subelements('interfaces', skip_missing=True) }}"
      ignore_errors: yes

    - name: Create disabled physical interface connections
      community.general.nmcli:
        conn_name: "{{ item.1 }}"
        ifname: "{{ item.1 }}"
        type: ethernet
        method4: disabled
        method6: disabled
        state: present
      loop: "{{ interfaces | subelements('interfaces', skip_missing=True) }}"

    - name: Check if member NICs (slaves) are already active
      ansible.builtin.command:
        cmd: "nmcli -g GENERAL.STATE connection show {{ item.0['con-name'] }}-{{ item.1 }}"
      loop: "{{ interfaces | subelements('interfaces', skip_missing=True) }}"
      register: slave_states
      changed_when: false
      failed_when: false

    #- name: Bring up member NICs (slaves) if not already active
    #  ansible.builtin.command:
    #    cmd: "nmcli connection up {{ item.item.0['con-name'] }}-{{ item.item.1 }}"
    #  loop: "{{ slave_states.results }}"
    #  when: item.rc == 0 and 'activated' not in item.stdout
    #  changed_when: true

    - name: Check if bridges are already active
      ansible.builtin.command:
        cmd: "nmcli -g GENERAL.STATE connection show {{ item['con-name'] }}"
      loop: "{{ interfaces }}"
      register: bridge_states
      changed_when: false
      failed_when: false

    - name: Bring up bridges if not already active
      ansible.builtin.command:
        cmd: "nmcli connection up {{ item.item['con-name'] }}"
      loop: "{{ bridge_states.results }}"
      when: item.rc == 0 and 'activated' not in item.stdout
      changed_when: true
        
    - name: Install required packages
      yum:
        name:
          - "@Development Tools"
          - "@Virtualization Platform"
          - "@Container Management"
          - "@Headless Management"
          - gcc
          - openssl-devel
          - bzip2-devel
          - libffi-devel
          - zlib-devel
          - xz-devel
          - readline-devel
          - sqlite-devel
          - wget
          - virt-install
          - cockpit-machines
          - libcdio
          - libguestfs
        state: present
        disable_gpg_check: yes

    - name: Start/Enable services
      systemd:
        name: "{{ item }}"
        state: started
        enabled: yes
      loop:
        - libvirtd
        - cockpit

    - name: Create sudoers entry for user
      copy:
        dest: /etc/sudoers.d/{{ ansible_user }}
        content: "{{ ansible_user }} ALL=(ALL) NOPASSWD:ALL\n"
        mode: '0440'
        validate: 'visudo -cf %s'