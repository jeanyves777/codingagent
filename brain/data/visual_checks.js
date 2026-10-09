// Coding Brain's own read-only page measurements: layout problems, visible elements and basic
// accessibility checks. Runs in the rendered page; it never clicks, types or changes anything.
() => {
  const viewport = { width: window.innerWidth, height: window.innerHeight };
  const root = document.documentElement;
  const visible = (el) => {
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const describe = (el) => {
    const box = el.getBoundingClientRect();
    const id = el.id ? "#" + el.id : "";
    const classes = typeof el.className === "string" && el.className.trim()
      ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".") : "";
    return {
      selector: el.tagName.toLowerCase() + id + classes,
      text: (el.innerText || el.getAttribute("aria-label") || el.getAttribute("alt") || el.value || "").trim().slice(0, 80),
      box: [Math.round(box.left + scrollX), Math.round(box.top + scrollY), Math.round(box.width), Math.round(box.height)],
    };
  };
  const parse = (color) => {
    const m = color.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const parts = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const luminance = ({ r, g, b }) => {
    const c = [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const background = (el) => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.backgroundImage && style.backgroundImage !== "none") return null; // unknown: image or gradient
      const color = parse(style.backgroundColor);
      if (color && color.a >= 0.99) return color;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };

  const layout = [];
  const issues = [];
  const all = Array.from(document.body ? document.body.querySelectorAll("*") : []);
  const meta = document.querySelector('meta[name="viewport"]');
  if (viewport.width < 600 && !(meta && /width\s*=\s*device-width/.test(meta.content || ""))) {
    issues.push({ kind: "missing_viewport_meta", severity: "major",
      detail: "no <meta name=viewport content=width=device-width>: phones render a zoomed-out desktop page" });
  }
  if (root.scrollWidth > viewport.width + 1) {
    issues.push({ kind: "horizontal_overflow", severity: "major",
      detail: `page is ${root.scrollWidth}px wide in a ${viewport.width}px viewport` });
  }
  let overflowing = 0;
  for (const el of all) {
    if (!visible(el)) continue;
    const box = el.getBoundingClientRect();
    if (box.right > viewport.width + 1 && overflowing < 8 && getComputedStyle(el).position !== "fixed") {
      const parentBox = el.parentElement ? el.parentElement.getBoundingClientRect() : null;
      if (!parentBox || parentBox.right <= viewport.width + 1) {
        overflowing++;
        issues.push({ kind: "element_outside_viewport", severity: "major", element: describe(el),
          detail: `extends to x=${Math.round(box.right)} beyond the ${viewport.width}px viewport` });
      }
    }
    const style = getComputedStyle(el);
    if (["hidden", "clip"].includes(style.overflowX) && el.scrollWidth > el.clientWidth + 2 && el.innerText) {
      issues.push({ kind: "clipped_text", severity: "minor", element: describe(el),
        detail: `content ${el.scrollWidth}px wide is cut to ${el.clientWidth}px` });
    }
  }
  for (const img of document.images) {
    if (visible(img) && img.complete && img.naturalWidth === 0) {
      issues.push({ kind: "broken_image", severity: "major", element: describe(img), detail: img.getAttribute("src") || "" });
    }
  }
  const interactive = all.filter((el) => visible(el) && el.matches("a[href], button, input, select, textarea, [role=button]"));
  for (let i = 0; i < interactive.length && i < 150; i++) {
    for (let j = i + 1; j < interactive.length && j < 150; j++) {
      const a = interactive[i].getBoundingClientRect(), b = interactive[j].getBoundingClientRect();
      if (interactive[i].contains(interactive[j]) || interactive[j].contains(interactive[i])) continue;
      const overlapX = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const overlapY = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (overlapX > 4 && overlapY > 4) {
        issues.push({ kind: "overlapping_controls", severity: "major",
          element: describe(interactive[i]), other: describe(interactive[j]) });
      }
    }
  }
  for (const el of all) {
    if (layout.length >= 120) break;
    if (visible(el) && el.matches("h1,h2,h3,h4,nav,header,footer,main,aside,section,form,table,img,button,a[href],input,select,textarea,label,[role]")) {
      const item = describe(el);
      const style = getComputedStyle(el);
      item.style = { color: style.color, background: style.backgroundColor, font: `${style.fontWeight} ${style.fontSize} ${style.fontFamily.split(",")[0]}` };
      layout.push(item);
    }
  }

  // Accessibility (a subset of WCAG checks that can be decided from the DOM alone).
  const a11y = [];
  const name = (el) => (el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText || el.value ||
    (el.getAttribute("aria-labelledby") && document.getElementById(el.getAttribute("aria-labelledby"))?.innerText) || "").trim();
  if (!root.getAttribute("lang")) a11y.push({ rule: "html-lang", severity: "serious", detail: "<html> has no lang attribute" });
  if (!document.title.trim()) a11y.push({ rule: "document-title", severity: "serious", detail: "the page has no <title>" });
  for (const img of document.querySelectorAll("img")) {
    if (!img.hasAttribute("alt")) a11y.push({ rule: "image-alt", severity: "critical", element: describe(img) });
  }
  for (const el of document.querySelectorAll("button, a[href], [role=button]")) {
    if (visible(el) && !name(el) && !el.querySelector("img[alt]:not([alt=''])")) {
      a11y.push({ rule: "control-name", severity: "critical", element: describe(el), detail: "no accessible name" });
    }
  }
  for (const el of document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea")) {
    const labelled = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest("label") ||
      el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || el.getAttribute("title");
    if (visible(el) && !labelled) a11y.push({ rule: "form-label", severity: "critical", element: describe(el) });
  }
  const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6")).filter(visible);
  if (!headings.some((h) => h.tagName === "H1")) a11y.push({ rule: "page-has-heading-one", severity: "moderate" });
  for (let i = 1; i < headings.length; i++) {
    const jump = Number(headings[i].tagName[1]) - Number(headings[i - 1].tagName[1]);
    if (jump > 1) a11y.push({ rule: "heading-order", severity: "moderate", element: describe(headings[i]) });
  }
  for (const el of document.querySelectorAll("[tabindex]")) {
    if (Number(el.getAttribute("tabindex")) > 0) a11y.push({ rule: "tabindex", severity: "serious", element: describe(el) });
  }
  let contrastChecked = 0;
  for (const el of all) {
    if (contrastChecked > 400 || !visible(el)) continue;
    const ownText = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim().length > 1);
    if (!ownText) continue;
    contrastChecked++;
    const style = getComputedStyle(el);
    const fg = parse(style.color), bg = background(el);
    if (!fg || !bg || fg.a < 0.99) continue;
    const l1 = luminance(fg), l2 = luminance(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(style.fontSize), bold = Number(style.fontWeight) >= 700;
    const required = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio < required) {
      a11y.push({ rule: "color-contrast", severity: "serious", element: describe(el),
        detail: `contrast ${ratio.toFixed(2)}:1, needs ${required}:1` });
    }
  }
  return {
    window: viewport, title: document.title, page_height: root.scrollHeight, page_width: root.scrollWidth,
    issues: issues.slice(0, 40), layout, accessibility: a11y.slice(0, 60),
    interactive_count: interactive.length,
  };
}
