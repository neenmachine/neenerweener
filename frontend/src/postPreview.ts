// The admin post preview document: the server's rendered html inside a page that may only show it. It is shown only
// as the srcDoc of <iframe sandbox="allow-scripts"> (no allow-same-origin): an opaque origin, so nothing in the
// html can reach the admin document, its cookies or its API. The meta CSP is enforced: no script runs (the image
// controller of the next change gets a per-preview nonce), and only this site's styles, fonts and images load.

const escapeAttr = (s: string) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");

export async function previewDocument(html: string): Promise<string> {
  const origin = location.origin;
  const styles = [...document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]')].map((l) => l.href);
  // Math is drawn in the browser on the public page; the sandbox runs no script, so draw it here, in an inert
  // parsed document (no script runs and nothing loads in it), only when the post has math.
  if (html.includes('class="math')) {
    const [{ default: katex }, { default: katexCss }] = await Promise.all([import("katex"), import("katex/dist/katex.min.css?url")]);
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll(".math").forEach((el) => {
      el.innerHTML = katex.renderToString(el.textContent ?? "", { displayMode: el.classList.contains("block"), throwOnError: false });
    });
    html = doc.body.innerHTML;
    styles.push(new URL(katexCss, origin).href);
  }
  const csp = `default-src 'none'; img-src ${origin} data: blob:; style-src ${origin} 'unsafe-inline'; font-src ${origin}; base-uri ${origin}; form-action 'none'`;
  // target=_blank without allow-popups: a link click does nothing, so the preview never navigates away.
  return `<!doctype html><html lang="en"><head><meta http-equiv="Content-Security-Policy" content="${csp}"><meta charset="utf-8"><meta name="color-scheme" content="light dark"><base href="${origin}/" target="_blank">${styles.map((h) => `<link rel="stylesheet" href="${escapeAttr(h)}">`).join("")}<style>main{padding:1.25rem}</style></head><body><main><article>${html}</article></main></body></html>`;
}
