"""Standalone durable Coding Brain worker."""
import argparse
import asyncio
from .durable_queue import QueueWorker
from .factory import build_brain_from_env


async def run(once=False, poll_seconds=0.5):
    brain = build_brain_from_env(require_queue=True)
    worker = QueueWorker(brain.queue, brain.dispatch_job)
    while True:
        handled = await worker.run_once()
        brain.dispatch_events()
        if once:
            return 0 if handled else 1
        if not handled:
            await asyncio.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.once)))


if __name__ == "__main__":
    main()
