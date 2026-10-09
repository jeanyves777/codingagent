"""Command line entry point."""
import sys

from .settings import load_settings


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    settings = load_settings(argv[0] if argv else "deploy.toml")
    print(f"{settings.region} x{settings.replicas}")
    return 0
