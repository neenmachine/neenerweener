// The admin post preview document: the server's rendered html inside a page that may only show it. It is shown only
// as the srcDoc of <iframe sandbox="allow-scripts"> (no allow-same-origin): an opaque origin, so nothing in the
// html can reach the admin document, its cookies or its API. The meta CSP is enforced: the one script that may run
// is the frame controller (previewFrame.ts), started by a script carrying this preview's random nonce, and the
// modules it imports. Authored scripts and event handlers in the post have no nonce, so they never run. Only this
// site's styles, fonts and images load.

import type { FrameImage, FrameStart } from "./previewFrame";

const escapeAttr = (s: string) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const random = () => [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");

// The controls the frame controller adds around each managed image (previewFrame.ts).
const CONTROLLER_CSS = `
.ween-image{position:relative;display:inline-block;max-width:100%;vertical-align:top;touch-action:none;border-radius:6px}
.ween-image img{display:block;cursor:grab;margin:0}
.ween-image.ween-dragging img{cursor:grabbing;opacity:.6}
.ween-image:focus-visible{outline:2px solid var(--accent,Highlight);outline-offset:2px}
.ween-tools{position:absolute;top:6px;left:6px;display:none;flex-wrap:wrap;gap:4px}
.ween-image:hover .ween-tools,.ween-image:focus-within .ween-tools{display:flex}
.ween-tools button{font:12px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;padding:2px 8px;border-radius:4px;border:1px solid color-mix(in srgb,CanvasText 25%,transparent);background:Canvas;color:CanvasText;cursor:pointer}
.ween-tools button:disabled{opacity:.45;cursor:default}
.ween-handle{position:absolute;right:-7px;bottom:-7px;width:14px;height:14px;border-radius:4px;background:var(--accent,Highlight);border:2px solid Canvas;cursor:nwse-resize;touch-action:none}
.ween-handle:focus-visible{outline:2px solid CanvasText;outline-offset:1px}
.ween-drop{position:absolute;height:3px;border-radius:2px;background:var(--accent,Highlight);pointer-events:none}`;

export type Preview = { html: string; blocks: number; images: FrameImage[] };

// The document and its channel token, which the frame's messages must carry. restore: scroll and focus to keep
// across an edit.
export async function previewDocument(p: Preview, restore: Pick<FrameStart, "scroll" | "focus">): Promise<{ doc: string; token: string }> {
  const token = random();
  const origin = location.origin;
  let html = p.html;
  const styles = [...document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]')].map((l) => l.href);
  // Math is drawn in the browser on the public page; draw it here, in an inert parsed document (no script runs and
  // nothing loads in it), only when the post has math.
  if (html.includes('class="math')) {
    const [{ default: katex }, { default: katexCss }] = await Promise.all([import("katex"), import("katex/dist/katex.min.css?url")]);
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll(".math").forEach((el) => {
      el.innerHTML = katex.renderToString(el.textContent ?? "", { displayMode: el.classList.contains("block"), throwOnError: false });
    });
    html = doc.body.innerHTML;
    styles.push(new URL(katexCss, origin).href);
  }
  // The controller runs only when there is something for it to do: a diagram to draw or an image to move.
  let script = "";
  const nonce = random();
  if (p.images.length || html.includes('class="mermaid"')) {
    const { url } = (await import("./previewFrame")).frame;
    const start: FrameStart = { token, origin, blocks: p.blocks, images: p.images, ...restore };
    // JSON in a script: < escaped, so no text in it can close the script element.
    const data = (v: unknown) => JSON.stringify(v).replace(/</g, "\\u003c");
    // In the head, before the post's html, so no unclosed element in the post can swallow it; a module runs after parsing.
    script = `<script type="module" nonce="${nonce}">import(${data(url)}).then((m) => m.frame.start(${data(start)}))</script>`;
  }
  const csp = `default-src 'none'; script-src 'nonce-${nonce}' 'strict-dynamic'; img-src ${origin} data: blob:; style-src ${origin} 'unsafe-inline'; font-src ${origin} data:; base-uri ${origin}; form-action 'none'`;
  // target=_blank without allow-popups: a link click does nothing, so the preview never navigates away.
  const doc = `<!doctype html><html lang="en"><head><meta http-equiv="Content-Security-Policy" content="${csp}"><meta charset="utf-8"><meta property="csp-nonce" nonce="${nonce}"><meta name="color-scheme" content="light dark"><base href="${origin}/" target="_blank">${styles.map((h) => `<link rel="stylesheet" href="${escapeAttr(h)}">`).join("")}<style>main{padding:1.25rem}${CONTROLLER_CSS}</style>${script}</head><body><main><article>${html}</article></main></body></html>`;
  return { doc, token };
}
