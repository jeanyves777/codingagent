"""State migrations between application versions.

User state (config/, data/) carries a schema version. A new application version declares the
schema it needs and the steps from older schemas. Updates back up state first, then run the new
version's migrations; a failed migration restores the backup. Steps must be idempotent.
"""
from .config import SCHEMA_VERSION, load, save
from .paths import Layout

# from-version -> function(layout, config) -> config, producing from-version + 1
STEPS = {}


def migrate(layout: Layout) -> dict:
    config = load(layout)
    start = int(config.get("schema_version", 1))
    if start > SCHEMA_VERSION:
        raise RuntimeError(f"State schema {start} is newer than this version supports ({SCHEMA_VERSION}); "
                           "update Coding Brain or restore a backup")
    applied = []
    for version in range(start, SCHEMA_VERSION):
        config = STEPS[version](layout, config)
        config["schema_version"] = version + 1
        applied.append(version + 1)
    config["schema_version"] = SCHEMA_VERSION
    save(layout, config)
    from .memory import migrate_all
    return {"from": start, "to": SCHEMA_VERSION, "applied": applied, "memory_stores": len(migrate_all(layout))}
