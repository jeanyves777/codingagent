"""Capability routing for attachments: parser, OCR, vision, premium vision.

    document parser   text, pages, cells and tables (always; brain.attachments)
    OCR (local)       images and scanned pages without reliable embedded text
    vision (local)    images, rendered figure pages and embedded images, when the task has any
    premium vision    only if approved for this task and the free vision model is unavailable or
                      failed; within the premium daily limit
    text/code model   planning and implementation, unchanged; it receives the findings as evidence

Nothing here runs for a task without attachments, so ordinary coding never loads a vision model.
"""
import time

from .attachments import Attachment, flag_untrusted
from .ocr import OCRUnavailable
from .vision import VisionUnavailable

BUDGETS = {"max_ocr_images": 12, "max_vision_images": 6, "max_premium_vision_calls": 2}


class Analyzer:
    def __init__(self, ocr=None, vision=None, premium=None, budgets: dict | None = None, log=print):
        self.ocr, self.vision, self.premium = ocr, vision, premium
        self.budgets = {**BUDGETS, **(budgets or {})}
        self.log = log
        self.processing = []  # what ran: capability, provider, model, seconds, outcome

    def _note(self, capability, attachment, loc, provider, model, started, outcome, **extra):
        self.processing.append({"capability": capability, "attachment": attachment.id, "loc": loc,
                                "provider": provider, "model": model, "outcome": outcome,
                                "seconds": round(time.monotonic() - started, 2), **extra})

    async def analyze(self, attachments: list[Attachment], goal: str) -> list[Attachment]:
        ocr_left, vision_left = self.budgets["max_ocr_images"], self.budgets["max_vision_images"]
        premium_left = self.budgets["max_premium_vision_calls"]
        vision_reason = None
        for attachment in attachments:
            for image in attachment.images:
                if image.get("needs_ocr") and ocr_left > 0:
                    ocr_left -= 1
                    started = time.monotonic()
                    if self.ocr is None or not self.ocr.available:
                        attachment.metadata["ocr_unavailable"] = "Tesseract OCR is not installed"
                        self._note("ocr", attachment, image["loc"], "tesseract", None, started, "unavailable")
                    else:
                        try:
                            result = self.ocr.recognize(image["path"], image["loc"])
                            if result["text"]:
                                attachment.ocr.append(result)
                            self._note("ocr", attachment, image["loc"], "tesseract", None, started, "ok",
                                       confidence=result["confidence"])
                        except OCRUnavailable as error:
                            attachment.warnings.append(f"OCR failed on {image['loc']}: {error}")
                            self._note("ocr", attachment, image["loc"], "tesseract", None, started, "failed")
                if vision_left <= 0:
                    continue
                vision_left -= 1
                finding, reason = await self._see(attachment, image, goal, premium_left > 0)
                if finding:
                    if finding.get("premium"):
                        premium_left -= 1
                    attachment.vision.append(finding)
                else:
                    vision_reason = reason
                    attachment.metadata["vision_unavailable"] = reason
            flag_untrusted(attachment)
        if vision_reason:
            self.log(f"Vision: {vision_reason}. Images are used through OCR text and metadata only.")
        return attachments

    async def _see(self, attachment, image, goal, premium_allowed):
        reasons = []
        for provider in (self.vision, self.premium if premium_allowed else None):
            if provider is None:
                continue
            started = time.monotonic()
            try:
                finding = await provider.describe(image["path"], goal, image["loc"])
                self._note("vision", attachment, image["loc"], provider.provider, finding["model"], started, "ok",
                           premium=provider.premium, prompt_tokens=finding.get("prompt_tokens"),
                           output_tokens=finding.get("output_tokens"))
                return finding, None
            except VisionUnavailable as error:
                reasons.append(str(error))
                self._note("vision", attachment, image["loc"], provider.provider, provider.model, started,
                           "unavailable", reason=str(error)[:200])
            except Exception as error:  # a model failure must not stop the task
                reasons.append(f"{provider.model} failed: {type(error).__name__}: {str(error)[:160]}")
                self._note("vision", attachment, image["loc"], provider.provider, provider.model, started, "failed",
                           reason=str(error)[:200])
        if self.vision is None and not reasons:
            reasons.append("no vision model configured (codingbrain setup --vision-model <ollama vision model>)")
        return None, "; ".join(reasons) or "vision unavailable"


def is_visual_goal(goal: str, attachments: list[Attachment]) -> bool:
    """Whether the goal asks to match or fix something visual, so visual verification applies."""
    import re
    has_image = any(attachment.kind == "image" for attachment in attachments)
    return has_image and bool(re.search(
        r"(?i)\b(ui|layout|design|screenshot|look|style|css|interface|page|screen|dashboard|component|match|"
        r"recreate|pixel|responsive|mobile|visual|align|spacing|color|colour|font|button|header|navbar|form)\b",
        goal))
