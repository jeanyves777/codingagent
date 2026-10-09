"""Attachments for local tasks: ingestion, analysis, retention and durable memory.

    data/projects/<id>/attachments.sqlite3     checksums, provenance, extractions, task links
    data/projects/<id>/attachments/<sha16>/    private copies while the retention policy allows

Images and documents are never put in memory records or in every model context: memory keeps
the checksum, origin, extracted text summary and the findings, with provenance. Sensitive
attachments (--sensitive, or retention "none") leave nothing behind but their checksum. A
re-extraction (newer parser or model) adds a new extraction; records the user approved stay.
"""
import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ..attachments import LIMITS, Attachment, AttachmentError, evidence, ingest, requirements_from
from . import config as settings

SCHEMA_VERSION = 1


class AttachmentStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS attachments (sha256 TEXT PRIMARY KEY, name TEXT, origin TEXT, kind TEXT,
                    format TEXT, size INTEGER, first_seen REAL, last_used REAL, retention TEXT, retained_path TEXT,
                    sensitive INTEGER DEFAULT 0, status TEXT DEFAULT 'active');
                CREATE TABLE IF NOT EXISTS extractions (id INTEGER PRIMARY KEY, sha256 TEXT, extractor_version TEXT,
                    created_at REAL, processing TEXT, data TEXT, approved INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS links (sha256 TEXT, task_id TEXT, goal TEXT, at REAL, outcome TEXT,
                    PRIMARY KEY (sha256, task_id));
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            version = int(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Attachment store schema {version} is newer than this version supports")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def record(self, attachment: Attachment, retention: str, retained: Path | None, sensitive: bool,
               processing: list) -> int:
        now = time.time()
        stored = attachment.as_dict()
        stored.pop("images", None)
        if sensitive:  # only the checksum and kind survive
            stored = {key: stored[key] for key in ("sha256", "kind", "format", "size", "extractor_version")}
        with self.connect() as db:
            db.execute("INSERT INTO attachments (sha256, name, origin, kind, format, size, first_seen, last_used, "
                       "retention, retained_path, sensitive) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sha256) DO "
                       "UPDATE SET last_used=excluded.last_used, retention=excluded.retention, "
                       "retained_path=excluded.retained_path, sensitive=excluded.sensitive, status='active', "
                       "name=excluded.name, origin=excluded.origin",
                       (attachment.sha256, "(sensitive)" if sensitive else attachment.name,
                        "(sensitive)" if sensitive else attachment.origin, attachment.kind, attachment.format,
                        attachment.size, now, now, retention, str(retained) if retained else None, int(sensitive)))
            cursor = db.execute("INSERT INTO extractions (sha256, extractor_version, created_at, processing, data) "
                                "VALUES (?,?,?,?,?)", (attachment.sha256, attachment.extractor_version, now,
                                                       json.dumps(processing)[:20000], json.dumps(stored)[:2_000_000]))
            return cursor.lastrowid

    def link(self, sha256: str, task_id: str, goal: str, outcome: str = "submitted"):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?)", (sha256, task_id, goal[:300], time.time(), outcome))

    def outcome(self, task_id: str, outcome: str):
        with self.connect() as db:
            db.execute("UPDATE links SET outcome=? WHERE task_id=?", (outcome, task_id))

    def entries(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT a.*, (SELECT COUNT(*) FROM extractions e WHERE e.sha256=a.sha256) AS extractions, "
                              "(SELECT COUNT(*) FROM links l WHERE l.sha256=a.sha256) AS tasks FROM attachments a "
                              "WHERE status != 'purged' ORDER BY last_used DESC").fetchall()
            return [dict(row) for row in rows]

    def get(self, prefix: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM attachments WHERE sha256 LIKE ?", (prefix + "%",)).fetchone()
            return dict(row) if row else None

    def extractions(self, sha256: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM extractions WHERE sha256=? ORDER BY id", (sha256,))]

    def tasks(self, sha256: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM links WHERE sha256=?", (sha256,))]

    def approve(self, sha256: str):
        with self.connect() as db:
            db.execute("UPDATE extractions SET approved=1 WHERE id=(SELECT MAX(id) FROM extractions WHERE sha256=?)",
                       (sha256,))

    def purge(self, sha256: str | None = None, task_id: str | None = None, policy: str | None = None) -> int:
        """Delete retained copies; the checksum, provenance and findings stay."""
        with self.connect() as db:
            query, values = "SELECT sha256, retained_path FROM attachments WHERE retained_path IS NOT NULL", []
            if sha256:
                query, values = query + " AND sha256=?", [sha256]
            if policy:
                query, values = query + " AND retention=?", values + [policy]
            if task_id:
                query += " AND sha256 IN (SELECT sha256 FROM links WHERE task_id=?) AND sha256 NOT IN " \
                         "(SELECT sha256 FROM links WHERE task_id != ? AND outcome NOT IN ('accepted','failed','cancelled'))"
                values += [task_id, task_id]
            rows = db.execute(query, values).fetchall()
            for row in rows:
                shutil.rmtree(row["retained_path"], ignore_errors=True)
                db.execute("UPDATE attachments SET retained_path=NULL WHERE sha256=?", (row["sha256"],))
            return len(rows)


def analyzer_for(config: dict, allow_premium: bool, layout=None, log=print):
    """The OCR engine and vision providers the settings name. Nothing loads unless used."""
    from ..multimodal import Analyzer
    from ..ocr import TesseractOCR
    from ..vision import PremiumCLIVision, local_provider
    ocr_settings = config["ocr"]
    ocr = TesseractOCR(ocr_settings.get("command") or None, ocr_settings.get("languages") or "eng") \
        if ocr_settings.get("engine", "tesseract") == "tesseract" else None
    vision = local_provider(settings.vision_settings(config))
    premium = None
    choice = config["vision"].get("premium", "off")
    if allow_premium and choice in {"claude", "codex"}:
        from ..subscriptions import ClaudeCodeSupervisor, CodexSupervisor
        from ..supervision import SupervisorLedger
        supervisor = (ClaudeCodeSupervisor if choice == "claude" else CodexSupervisor)(
            choice, (config["supervisors"].get(choice) or {}).get("model"))
        ledger = SupervisorLedger(layout.data / "supervision.sqlite3") if layout else None
        premium = PremiumCLIVision(supervisor, approved=True, ledger=ledger,
                                   daily_limit=config["budgets"].get("daily_limit", 20))
    elif allow_premium:
        log("Premium vision was allowed for this task, but none is selected (codingbrain setup --premium-vision claude).")
    return Analyzer(ocr=ocr, vision=vision, premium=premium, budgets=config.get("multimodal_budgets"), log=log)


def prepare(context, paths: list[str], goal: str, allow_premium=False, sensitive=False, analyzer=None,
            log=print) -> tuple[list[Attachment], dict, list]:
    """Ingest and analyze the files the user named for one goal. Returns the attachments, the
    evidence packet for planning, and what processing ran."""
    import asyncio
    config = context.config
    limits = {**LIMITS, **config["attachments"].get("limits", {})}
    if len(paths) > limits["max_files"]:
        raise AttachmentError(f"at most {limits['max_files']} attachments per task")
    work = context.data / "attachments"
    attachments, total = [], 0
    for raw in paths:
        attachment = ingest(raw, work, limits, config["attachments"].get("allowed_roots") or (),
                            forbidden_roots=(context.layout.home,))
        total += attachment.size
        if total > limits["max_total_mb"] * 1_000_000:
            raise AttachmentError("the attachments together exceed the total size limit")
        attachments.append(attachment)
    if sensitive and allow_premium:
        log("Sensitive attachments are never sent to premium (cloud) vision; using local models only.")
        allow_premium = False
    analyzer = analyzer or analyzer_for(config, allow_premium, context.layout, log)
    asyncio.run(analyzer.analyze(attachments, goal))
    return attachments, evidence(attachments), analyzer.processing


def retain(context, attachments: list[Attachment], processing: list, sensitive: bool) -> list[int]:
    """Record attachments with their retention policy, and remember useful findings."""
    store = AttachmentStore(context.data / "attachments.sqlite3")
    retention = "none" if sensitive else context.config["attachments"].get("retention", "task")
    ids = []
    for attachment in attachments:
        folder = context.data / "attachments" / attachment.sha256[:16]
        retained = folder if retention != "none" and folder.exists() else None
        ids.append(store.record(attachment, retention, retained, sensitive,
                                [item for item in processing if item["attachment"] == attachment.id]))
        if not sensitive and context.config["attachments"].get("remember", True):
            remember_findings(context, attachment)
    return ids


def discard_private_copies(context, attachments: list[Attachment]):
    for attachment in attachments:
        shutil.rmtree(context.data / "attachments" / attachment.sha256[:16], ignore_errors=True)


def remember_findings(context, attachment: Attachment) -> int:
    """Documented requirements and design descriptions as unverified, provenance-tagged memory.
    They become verified only through an accepted, tested task or the user's approval."""
    memory = context.memory
    count = 0
    ref = f"attachment:{attachment.sha256[:12]} {attachment.name}"
    for item in requirements_from(attachment, limit=25):
        memory.store.add("requirement", f"{item['text']} [{attachment.name}, {item['loc']}]", "attachment", 6,
                         "unverified", f"{ref}#{item['loc']}")
        count += 1
    for finding in attachment.vision[:3]:
        data = finding.get("findings") or {}
        if data.get("content_type") in {"ui_design", "app_screenshot"} and data.get("summary"):
            memory.store.add("design", f"Design reference {attachment.name} (sha256 {attachment.sha256[:12]}): "
                             f"{data['summary'][:300]} [vision {finding.get('model')}, unverified]", "attachment", 6,
                             "unverified", ref)
            count += 1
        elif data.get("content_type") in {"diagram", "flowchart"} and data.get("summary"):
            memory.store.add("architecture", f"Diagram {attachment.name}: {data['summary'][:300]} "
                             f"[vision {finding.get('model')}, unverified]", "attachment", 6, "unverified", ref)
            count += 1
        elif data.get("content_type") == "error_screenshot" and data.get("summary"):
            memory.store.add("bug", f"Screenshot of a defect {attachment.name}: {data['summary'][:300]}",
                             "attachment", 6, "unverified", ref)
            count += 1
    return count


def remember_outcome(context, task: dict):
    """After acceptance: the design reference and visual corrections become verified history."""
    packet = task.get("attachments") or {}
    visual = task.get("visual_verification") or {}
    passed = visual.get("status") == "passed"
    store = AttachmentStore(context.data / "attachments.sqlite3")
    store.outcome(task["id"], "accepted")
    for item in packet.get("attachments", []):
        if item.get("kind") == "image" and task.get("visual"):
            context.memory.remember(
                "design", f"Accepted design reference {item['name']} (sha256 {item['sha256'][:12]}) implemented for "
                f"'{task['goal'][:160]}'; visual verification {visual.get('status', 'not run')} at "
                f"{', '.join(visual.get('viewports') or []) or 'no viewports'}", verified=passed,
                ref=f"task:{task['id'][:12]}")
    if task.get("visual_repairs"):
        context.memory.remember("repair", f"Visual corrections for '{task['goal'][:160]}': "
                                f"{task['visual_repairs']} repair round(s); {visual.get('statement', '')[:200]}",
                                verified=passed, ref=f"task:{task['id'][:12]}")
    if context.config["attachments"].get("retention", "task") == "task":
        store.purge(task_id=task["id"], policy="task")
