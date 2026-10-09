# Images, screenshots and documents (0.11)

Coding Brain can read the screenshots, designs, diagrams, PDFs, Word and Excel files you give it
with a goal, plan and implement from them, then render the result in a browser and check it
against the design before you accept it.

## Setup on Windows

`codingbrain update` installs this version over 0.9.0 or 0.10.0. Your settings, projects, memory
and saved tasks are kept, and the old version stays available through `codingbrain rollback`.
Then add the optional parts:

```powershell
# 1. A vision model. Your coding model (e.g. qwen2.5-coder) cannot see images.
ollama pull qwen2.5vl:7b                 # ~6 GB; qwen2.5vl:3b (~3 GB) for 8 GB RAM machines
codingbrain setup --vision-model qwen2.5vl:7b

# 2. OCR for screenshots and scanned PDFs (optional; vision works without it)
winget install UB-Mannheim.TesseractOCR

# 3. Check every capability
codingbrain doctor
```

`codingbrain setup --vision` asks the same questions interactively and asks before downloading
anything large. Visual checks use Microsoft Edge, which comes with Windows, so there is no browser
download.

`doctor` reports each capability on its own line:

| Line | Meaning |
| --- | --- |
| `local model` | the coding model |
| `vision` | the vision model, and whether Ollama says it accepts images |
| `ocr` | Tesseract |
| `documents` | the PDF, Word, Excel, CSV and image parsers |
| `browser` | the browser used for visual checks |
| `docker sandbox` | the offline test and build sandbox |
| `premium: claude` / `premium: codex` | signed-in premium CLIs |
| `premium vision` | whether premium vision is enabled |

## Commands

```powershell
cd C:\Projects\MyApp
codingbrain run "Fix the UI based on this screenshot" --attach "C:\Screenshots\error.png"
codingbrain run "Implement this specification" --attach "C:\Documents\requirements.pdf"
codingbrain run "Recreate this interface" --attach "C:\Designs\dashboard.png"
codingbrain run "Analyze these files" --attach "C:\Documents\spec.docx" --attach "C:\Documents\data.xlsx"
```

Options for `run`:

| Option | Effect |
| --- | --- |
| `--attach FILE` | Attach a file; repeat for more (up to 10 per task). |
| `--inspect-url http://localhost:3000` | Also capture your running app at desktop, tablet and mobile sizes, with its layout and accessibility findings, as evidence. |
| `--visual-check` / `--no-visual-check` | Force visual verification of the result on or off. It is on automatically when the goal is visual and an image is attached. |
| `--viewport desktop\|tablet\|mobile` | Choose the viewports (repeatable). |
| `--allow-premium-vision` | Allow the configured premium vision (Claude or Codex) for this task's images. |
| `--sensitive` | Use local models only, and after the task keep nothing but checksums. |

Other commands:

```powershell
codingbrain attachments preview C:\Docs\spec.pdf    # what would be extracted; keeps nothing
codingbrain attachments list                        # this project's attachments, retention, tasks
codingbrain attachments show <id>                   # provenance and the latest extraction
codingbrain attachments approve <id>                # approve the latest extraction
codingbrain attachments reprocess <id>              # re-extract with newer parsers/models
codingbrain attachments purge [<id>]                # delete kept copies; findings remain
codingbrain inspect-ui http://localhost:3000 --compare C:\Designs\dashboard.png
```

## What happens with an attachment

1. **Permission.** Only files you name are read. Every file is refused if it is a link or
   junction, a secret (`.env`, keys, credential files), Coding Brain's own data, or outside
   `attachments.allowed_roots` when you set it.
2. **Format.** The format is identified from the content, not the extension. Programs, archives,
   legacy `.doc`/`.xls`, password-protected and corrupted files are refused with a reason.
3. **Isolation.** The file is copied to a private folder and parsed in a separate isolated Python
   process, with a time limit and, on Linux and macOS, memory and CPU limits. The size limits are:
   25 MB per file, 100 MB per task, 40 megapixels per image, 60 PDF pages, and ZIP-bomb checks for
   Word and Excel files. All are configurable under `attachments.limits`.
4. **Extraction.** Each format keeps its locations:

   | Format | What is extracted |
   | --- | --- |
   | PDF | text per page; pages without text are rendered for OCR and vision |
   | Word | paragraphs with their heading path, tables with row numbers, embedded images |
   | Excel | cell references (`Prices!B2=2.5`) and formulas, which are listed but never evaluated |
   | CSV | cell references |
   | Text and code | line ranges |
   | Images | size and frames; animated GIF/WebP use the first, middle and last frame |

   Macros, scripts, external links and embedded objects are never executed or fetched.
5. **OCR** (Tesseract) runs on images and scanned pages only. PDFs and documents that contain
   real text are not OCRed. Results keep word boxes and confidence.
6. **Vision.** The vision model runs only when the task has images, and only if Ollama reports
   that the model accepts images. It returns structured findings: the content type, visible
   text, UI elements and where they are, layout, colors, defects, diagram nodes and edges, implied
   requirements, and what it is unsure about. Without a usable vision model, the task continues
   with OCR text and metadata, and says so.
7. **Preview.** Before acting, `run` shows what was extracted and asks whether to continue.
8. **Evidence.** The planner, requirement checks, premium planner and reviewers receive the
   extracted content labelled as untrusted evidence with checksums and locations. Images are
   never pasted into every context. Text that looks like instructions to the agent (for example
   "ignore previous instructions") is flagged and treated as content only.

## Visual verification

When the tests pass, a task with visual requirements is checked in a browser:

1. **Build.** The frontend is built in the offline Docker sandbox, or static files are used as
   they are. The project's `coding-brain.json` chooses this; the model cannot change it:

   ```json
   {"preview": {"build_command": ["npx", "vite", "build", "--outDir", "/out"]}}
   ```

   Without a `preview` entry, an `index.html` in the project (or in `public/`) is used.
2. **Serve.** The result is served read-only from `127.0.0.1`. The headless browser can reach
   only that address; everything else, such as web fonts and analytics, is blocked and listed.
3. **Measure.** At desktop (1440×900), tablet (834×1112) and mobile (390×844) sizes it:
   - takes full-page screenshots;
   - checks the layout: horizontal overflow, elements outside the viewport, overlapping controls,
     clipped text, broken images and a missing mobile viewport tag;
   - runs accessibility checks: alt text, labels, accessible names, contrast, headings, page
     language and title, tab order and visible focus;
   - collects console errors.
4. **Compare.** The screenshot is compared with your reference image in two ways:
   - **Measurements:** the share of changed pixels, the regions that differ, and the main colors.
   - **The vision model's list of differences:** a model judgment, labelled as such.
5. **Repair.** Blocking findings go back to the coding model with the source files that most
   likely produce them (matched by class names, ids and text). Visual repairs are limited to two
   by default (`visual.max_repairs`) and stop as soon as the same findings repeat. What remains
   is shown when you accept.

Coding Brain never claims pixel-perfect results from a model's opinion. The only exact statement
it makes is "identical pixels", when that is measured. Font substitution, blocked network assets
and scaling are reported as uncertainty.

## Memory and retention

- Each project has its own attachment store, `data\projects\<project>\attachments.sqlite3`. It
  holds each attachment's checksum, origin, size, every extraction (versioned), the processing
  log and the tasks that used it.
- Requirements stated in documents, design descriptions, diagrams and defect screenshots become
  **unverified** project memory with provenance (`attachment:<sha> file#page 3`). They become
  verified only through an accepted, tested task or your approval.
- Accepted design references and visual corrections are recorded as verified history.
- Design tokens from your CSS variables, Tailwind theme and token files are remembered, so new UI
  reuses them.
- Reprocessing adds a new extraction. It never overwrites approved records.
- Copies of attachments are kept until the task is accepted (`attachments.retention: "task"`).
  Use `"keep"` to keep them for reprocessing, or `"none"` to keep nothing. `--sensitive` keeps
  only checksums, and sends nothing to premium or cloud models.

## Premium vision

Premium vision is off by default. `codingbrain setup --premium-vision claude` (or `codex`)
enables it, and each task must also pass `--allow-premium-vision`. The images go to a private
temporary folder for the signed-in CLI, read-only, and each call counts against the premium daily
limit. Images are never uploaded automatically.

## Settings (config.json)

```json
"vision": {"provider": "ollama", "url": "", "model": "qwen2.5vl:7b", "supports_images": false,
           "api_key_env": "", "premium": "off"},
"ocr": {"engine": "tesseract", "command": "", "languages": "eng"},
"attachments": {"retention": "task", "allowed_roots": [], "limits": {}, "remember": true},
"visual": {"enabled": true, "viewports": ["desktop", "tablet", "mobile"], "max_repairs": 2,
           "browser_channel": "", "browser_executable": "", "accessibility_blocking": ["critical"],
           "pixel_threshold": 0.35},
"multimodal_budgets": {"max_ocr_images": 12, "max_vision_images": 6, "max_premium_vision_calls": 2}
```

For an OpenAI-compatible vision server (LM Studio, vLLM), set `"provider": "openai"`, its `url`
and `model`, and `"supports_images": true`. Only do this for a model that really accepts images:
there is no way to detect it automatically.

## Known limitations

- Visual verification checks single tasks. Orchestrated multi-assignment goals receive the
  attachment evidence, but are not rendered and compared.
- Previews are static: either a build in the sandbox, or plain HTML. Server-rendered apps that need
  a running backend can be inspected with `inspect-ui` or `--inspect-url` while you run them, but
  the worktree result is not served automatically.
- The accessibility checks are a DOM-based subset of WCAG. They do not replace a full audit.
- Vision models can misjudge differences. Measurements, browser checks and your acceptance remain
  the authority.
- PowerPoint, legacy Office formats, archives and password-protected PDFs are not supported.
- Memory and CPU limits for the parser process apply on Linux and macOS. On Windows, the time
  limit and the input size limits apply.
