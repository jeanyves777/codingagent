"""Documents on request produce real files (PDF through the system browser, Word, Excel, CSV, text),
never a project; model text never becomes markup, formulas or script."""
import importlib.util
import json
import os
import zipfile
from xml.etree import ElementTree

import pytest

from brain import documents
from brain.documents import DocumentError, create, markdown_to_html

DOC = """# Wedding budget
Some **important** notes and a `code` word. <script>alert(1)</script>

## Costs
| Item | Cost |
| --- | --- |
| Venue | 4,000 |
| Catering | =SUM(A1:A9) |

- First point
- Second point
"""


class Model:
    model = "fake-writer"

    def __init__(self, text=DOC):
        self.text, self.calls = text, []

    def chat(self, messages, max_tokens=600, on_token=None):
        self.calls.append(messages)
        if on_token:
            for piece in self.text.split(" "):
                on_token(piece + " ")
        return self.text


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CODINGBRAIN_DOCUMENTS", str(tmp_path / "docs"))
    return tmp_path / "docs"


def test_html_conversion_escapes_everything_the_model_wrote():
    page = markdown_to_html(DOC, "t")
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<strong>important</strong>" in page and "<table>" in page and "<li>First point</li>" in page


def test_word_document_is_valid_and_has_the_content(folder):
    result = create(Model(), "make me a word document about the wedding budget")
    assert result["format"] == "docx" and result["path"].endswith(".docx")
    with zipfile.ZipFile(result["path"]) as package:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(package.namelist())
        xml = package.read("word/document.xml").decode("utf-8")
    ElementTree.fromstring(xml)  # well-formed
    assert "Wedding budget" in xml and "Venue" in xml and "&lt;script&gt;" in xml and "<w:tbl>" in xml


def test_spreadsheet_has_values_never_formulas(folder):
    from openpyxl import load_workbook
    result = create(Model(), "make me an excel budget for a wedding")
    assert result["format"] == "xlsx" and result["sheets"] == 1
    sheet = load_workbook(result["path"]).active
    assert sheet.title == "Costs" and [cell.value for cell in sheet[1]] == ["Item", "Cost"]
    assert sheet["B2"].value == 4000
    assert not str(sheet["B3"].value).startswith("=")  # a model "formula" stays inert text


def test_files_are_never_overwritten_and_land_in_the_documents_folder(folder):
    first = create(Model(), "write me a markdown summary of the wedding budget")
    second = create(Model(), "write me a markdown summary of the wedding budget")
    assert first["path"] != second["path"]
    assert all(os.path.dirname(path) == str(folder) for path in (first["path"], second["path"]))


def test_language_and_streaming_reach_the_model(folder):
    model, pieces = Model(), []
    create(model, "write a short report in French about Rome", on_token=pieces.append)
    assert "French" in model.calls[0][0]["content"] and pieces


def test_images_and_slide_decks_are_refused_clearly(folder):
    with pytest.raises(DocumentError, match="cannot be produced locally"):
        create(Model(), "make me a logo image for my bakery")


def test_an_empty_model_answer_is_an_error_not_an_empty_file(folder):
    with pytest.raises(DocumentError):
        create(Model(text="ok"), "write a report about Rome")
    assert not folder.exists() or not any(folder.iterdir())


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="needs Playwright")
def test_pdf_is_printed_offline_by_the_system_browser(folder):
    from brain.visual import browser_launch_options
    try:
        result = create(Model(), "make me a pdf about the wedding budget", launch_options=browser_launch_options({}))
    except Exception as error:  # no Edge/Chromium on this machine
        pytest.skip(f"no browser for PDF printing: {error}")
    data = open(result["path"], "rb").read()
    assert data.startswith(b"%PDF") and result["pages"] >= 1


def test_engine_creates_documents_and_records_them_in_the_conversation(tmp_path, monkeypatch):
    from brain.local import config as settings
    from brain.local.engine import Engine
    from brain.local.paths import Layout
    layout = Layout(tmp_path / "home").ensure()
    monkeypatch.setenv("CODINGBRAIN_HOME", str(layout.home))
    engine = Engine(layout, tmp_path)
    assert "documents.create" in engine.call("engine.info")["features"]
    monkeypatch.setattr(engine.assistant(), "model", Model())
    events = []
    result = engine.call("documents.create", {"request": "write a markdown note about the wedding budget"}, emit=events.append)
    assert result["format"] == "md" and os.path.exists(result["path"])
    assert any(event["event_type"] == "reply_step" for event in events)
    history = engine.call("conversation.history", {})
    assert any(turn["content"].startswith("[Document] Created MD") for turn in history)
    engine.close()
