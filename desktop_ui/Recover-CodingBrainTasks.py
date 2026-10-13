"""Recover abandoned task states using the checksum-verified target engine wheel.

Runs on the existing engine interpreter, including versions with the stale-task
update guard. Uses only the standard library; it never executes a task.
"""
import argparse
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--wheel', type=Path, required=True)
parser.add_argument('--home', type=Path, required=True)
args = parser.parse_args()
wheel = args.wheel.resolve(strict=True)
sys.path.insert(0, str(wheel))
from brain.local.paths import Layout
from brain.local.session import active_sessions, recover_interrupted_tasks, running_tasks

layout = Layout(args.home.resolve())
sessions = active_sessions(layout)
if sessions:
    raise SystemExit('A Coding Brain session is still running. Close it before updating.')
for task in recover_interrupted_tasks(layout):
    print(f"Recovered interrupted task {task['project']}:{task['id'][:8]}; work preserved, explicit retry required.")
remaining = running_tasks(layout)
if remaining:
    raise SystemExit('Tasks with live or unverified owners still block updating: ' +
                     ', '.join(f"{t['project']}:{t['id'][:8]} ({t['status']})" for t in remaining))
print('Task ownership verified: no live coding work blocks the update.')
