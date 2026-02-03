#!/usr/bin/env python3
import argparse
import json
import sys
import os
from pathlib import Path
from datetime import datetime


class WorkflowRunner:
    # ANSI colors
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    NC = "\033[0m"

    def __init__(self):
        work_dir = Path(os.environ.get("OLM_WORK_DIR", ".")).expanduser().resolve()
        self.state_file = work_dir / "workflow_state.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def print_banner(self, title: str, width: int = 70):
        """Print a fancy banner"""
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
        print(f"{self.CYAN}║ {' ' * left_padding}{self.BOLD}{title}{self.NC}{self.CYAN}{' ' * right_padding} ║{self.NC}")
        print(f"{self.CYAN}╚{border}╝{self.NC}")
        print("")

    def load_state(self):
        if not self.state_file.exists():
            return {"workflows": {}}
        with open(self.state_file) as f:
            return json.load(f)

    def save_state(self, state):
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def get_workflow_status(self, workflow_name):
        state = self.load_state()
        return state.get("workflows", {}).get(workflow_name, {})

    def mark_started(self, workflow_name):
        state = self.load_state()
        if "workflows" not in state:
            state["workflows"] = {}
        
        state["workflows"][workflow_name] = {
            "status": "running",
            "started_at": datetime.now().isoformat()
        }
        self.save_state(state)

    def mark_completed(self, workflow_name):
        state = self.load_state()
        if "workflows" not in state:
            state["workflows"] = {}
        
        state["workflows"][workflow_name] = {
            "status": "completed",
            "completed_at": datetime.now().isoformat()
        }
        self.save_state(state)

    def clear_workflow(self, workflow_name):
        state = self.load_state()
        if workflow_name in state.get("workflows", {}):
            del state["workflows"][workflow_name]
            self.save_state(state)

    def prompt_user(self, workflow_name, status):
        """Interactive prompt for workflow state"""
        print(f"\n{self.YELLOW}⚠️  Workflow '{workflow_name}' is already {status['status']}{self.NC}")
        
        if status['status'] == 'completed':
            completed_at = status.get('completed_at', 'unknown time')
            print(f"{self.YELLOW}   Completed at: {completed_at}{self.NC}")
            print("\nWhat would you like to do?")
            print("  [S] Skip - Don't run the workflow")
            print("  [R] Restart - Clear state and run from beginning")
            print("  [Q] Quit - Exit now")
        elif status['status'] == 'running':
            started_at = status.get('started_at', 'unknown time')
            print(f"{self.YELLOW}   Started at: {started_at}{self.NC}")
            print("\nWhat would you like to do?")
            print("  [C] Continue - Resume from where it left off")
            print("  [R] Restart - Clear state and run from beginning")
            print("  [Q] Quit - Exit now")
        
        while True:
            choice = input("\nChoice: ").strip().upper()
            
            if choice == 'S' and status['status'] == 'completed':
                print("Skipping workflow execution.")
                return 'skip'
            elif choice == 'C' and status['status'] == 'running':
                print("Continuing workflow...")
                return 'continue'
            elif choice == 'R':
                print("Restarting workflow from beginning...")
                self.clear_workflow(workflow_name)
                return 'restart'
            elif choice == 'Q':
                print("Exiting.")
                return 'quit'
            else:
                print("Invalid choice. Please try again.")

    def start_workflow(self, workflow_name, non_interactive=False):
        """Handle workflow start with state checking"""
        status = self.get_workflow_status(workflow_name)
        
        if status:
            if non_interactive:
                if status['status'] == 'completed':
                    print(f"{self.GREEN}✓ Workflow '{workflow_name}' already completed{self.NC}")
                    sys.exit(1)
            else:
                action = self.prompt_user(workflow_name, status)
                
                if action == 'skip':
                    sys.exit(1)
                elif action == 'quit':
                    sys.exit(2)
                elif action == 'restart':
                    pass
                elif action == 'continue':
                    sys.exit(0)
        
        # Mark workflow as started and show banner
        self.mark_started(workflow_name)
        self.print_banner(f"WORKFLOW: {workflow_name}")
        print(f"{self.CYAN}▶ Starting workflow execution...{self.NC}\n")
        sys.exit(0)

    def complete_workflow(self, workflow_name):
        """Mark workflow as completed"""
        self.mark_completed(workflow_name)
        print("")
        print(f"{self.GREEN}{'─' * 70}{self.NC}")
        print(f"{self.GREEN}✓ Workflow '{workflow_name}' completed successfully{self.NC}")
        print(f"{self.GREEN}{'─' * 70}{self.NC}")

    def show_state(self):
        """Display current workflow state"""
        state = self.load_state()
        
        if not state.get("workflows"):
            print("No workflows tracked yet")
            return
        
        print("\nWorkflow State:")
        print("=" * 70)
        for name, info in state.get("workflows", {}).items():
            status = info.get("status", "unknown")
            timestamp_key = "completed_at" if status == "completed" else "started_at"
            timestamp = info.get(timestamp_key, "unknown")
            
            status_icon = "✓" if status == "completed" else "▶" if status == "running" else "?"
            print(f"{status_icon} {name:30} {status:12} {timestamp}")


def main():
    parser = argparse.ArgumentParser(description="Workflow state manager")
    parser.add_argument("--start", help="Start/check workflow")
    parser.add_argument("--complete", help="Mark workflow as complete")
    parser.add_argument("--clear", help="Clear specific workflow state")
    parser.add_argument("--clear-all", action="store_true", help="Clear all workflow state")
    parser.add_argument("--show", action="store_true", help="Show current state")
    parser.add_argument("--non-interactive", action="store_true", help="Non-interactive mode")
    
    args = parser.parse_args()
    runner = WorkflowRunner()
    
    if args.start:
        runner.start_workflow(args.start, non_interactive=args.non_interactive)
    elif args.complete:
        runner.complete_workflow(args.complete)
    elif args.clear:
        runner.clear_workflow(args.clear)
        print(f"✓ Cleared workflow state: {args.clear}")
    elif args.clear_all:
        runner.save_state({"workflows": {}})
        print("✓ Cleared all workflow state")
    elif args.show:
        runner.show_state()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()