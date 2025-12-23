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

class StateManager:
    """Manages deployment state for resume capability"""
    
    def __init__(self):
        # Always use /docker-workspace/config/install
        self.state_file = Path("/docker-workspace/config/install/.cache/state.json")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
    
    def _load_state(self) -> Dict[str, Any]:
        """Load existing state or create new"""
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                return json.load(f)
        return {
            "tasks": {},
            "last_run": None,
            "status": "not_started"
        }
    
    def _save_state(self):
        """Persist state to disk"""
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)
    
    def is_completed(self, task_id: str) -> bool:
        """Check if task is already completed"""
        return self.state["tasks"].get(task_id, {}).get("status") == "completed"
    
    def mark_started(self, task_id: str):
        """Mark task as started"""
        self.state["tasks"][task_id] = {
            "status": "running",
            "started_at": datetime.now().isoformat()
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
    """Handles logging for task execution"""
    
    # ANSI color codes
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    NC = '\033[0m'  # No Color
    
    def __init__(self, task_id: str):
        self.task_id = task_id
        
        # Setup log directory - always use install path
        log_dir = Path("/docker-workspace/config/install/.cache/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Setup logger
        self.logger = logging.getLogger(f"task.{task_id}")
        self.logger.setLevel(logging.INFO)
        
        # File handler for task-specific log
        task_log = log_dir / f"{task_id}.log"
        fh = logging.FileHandler(task_log)
        fh.setLevel(logging.INFO)
        
        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        
        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)
    
    def print_banner(self, title: str, width: int = 68):
        """Print a fancy banner for task separation with centered text"""
        # Calculate padding for centered text
        content_width = width - 4  # Account for "║  " and "  ║"
        title_len = len(title)
        
        if title_len > content_width:
            title = title[:content_width]
            title_len = content_width
        
        # Calculate left and right padding for centering
        total_padding = content_width - title_len
        left_padding = total_padding // 2
        right_padding = total_padding - left_padding
        
        # Build the banner
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
        self.data_dir = Path("/docker-workspace/data")
        self.install_dir = Path("/docker-workspace/config/install")
    
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
        # Set environment variables for subprocess
        env_vars = os.environ.copy()
        env_vars['ANSIBLE_CONFIG'] = str(self.data_dir / 'ansible.cfg')
        
        # Build ansible-playbook command
        inventory_path = self.install_dir / "inventory.yml"
        playbook_path = Path(file)
        
        cmd = [
            'ansible-playbook',
            '-i', str(inventory_path),
            str(playbook_path),
            '-e', f'target_hosts={hosts}',
            '-e', f'env_name=install',  # Always use 'install' as env name
        ]
        
        # Add additional args if provided
        if args:
            cmd.extend(args.split())
        
        self.logger.info(f"Running: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                env=env_vars,
                check=True,
                text=True
            )
            self.logger.info(f"Task {task_id} completed successfully")
            self.logger.info(result.stdout)
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Task {task_id} failed with exit code {e.returncode}")
            self.logger.error(e.stdout)
            self.logger.error(e.stderr)
            raise
    
    def _execute_shell(self, task_id: str, command: str, **kwargs):
        """Execute a shell command"""
        self.logger.info(f"Running shell command: {command}")
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                check=True,
                text=True
            )
            self.logger.info(f"Task {task_id} completed successfully")
            self.logger.info(result.stdout)
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Task {task_id} failed with exit code {e.returncode}")
            self.logger.error(e.stdout)
            self.logger.error(e.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description='Run deployment tasks with state management')
    parser.add_argument('--task-id', help='Task ID to execute')
    parser.add_argument('--kind', help='Task kind (ansible, shell)')
    parser.add_argument('--hosts', help='Target hosts for ansible')
    parser.add_argument('--file', help='Ansible playbook file path')
    parser.add_argument('--args', default='', help='Additional arguments')
    parser.add_argument('--command', help='Shell command to execute')
    parser.add_argument('--resume', action='store_true', help='Resume from last checkpoint')
    
    args = parser.parse_args()
    
    # Initialize state manager
    state = StateManager()
    
    # Initialize logger
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
        print(f"Task already completed, skipping...")
        return 0
    
    # Print task banner
    logger.print_banner(f"Executing Task: {args.task_id}")
    
    # Initialize executor
    executor = TaskExecutor(logger)
    
    # Mark task as started
    state.mark_started(args.task_id)
    
    try:
        # Execute the task
        executor.execute(
            task_id=args.task_id,
            kind=args.kind,
            hosts=args.hosts,
            file=args.file,
            args=args.args,
            command=args.command
        )
        
        # Mark as completed
        state.mark_completed(args.task_id)
        logger.print_separator()
        logger.print_success(f"Task {args.task_id} completed successfully")
        logger.print_separator()
        return 0
        
    except Exception as e:
        # Mark as failed
        state.mark_failed(args.task_id, str(e))
        logger.print_separator()
        logger.print_error(f"Task {args.task_id} failed: {str(e)}")
        logger.print_separator()
        return 1


if __name__ == "__main__":
    sys.exit(main())
