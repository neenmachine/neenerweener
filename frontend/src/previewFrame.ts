// The one script of the admin post preview. It runs only inside the sandboxed preview frame (opaque origin, see
// postPreview.ts), loaded by the frame's one nonce script; nothing else there may run. It draws mermaid diagrams and
// gives each managed web-post image move and resize controls. It never edits the post itself: it tells the parent
// "move image X before block N" or "make image X N px wide"; the server rewrites only that image's source.
// The parent imports this module only for its URL; importing it does nothing.

export type FrameImage = { id: string; block: number; standalone: boolean; width: number | null };
export type FrameStart = {
  token: string; // this preview's channel: the parent drops a message without it
  origin: string;
  blocks: number;
  images: FrameImage[];
  scroll: number;
  focus: { id: string; control: Control } | null; // the control to focus again after the edit that made this preview
  // Images pasted since the last save, as data: URLs: /media serves an image only once a saved post uses it.
  local: Record<string, string>;
};
export type Control = "earlier" | "later" | "smaller" | "larger" | "width";
export type FrameEdit = { token: string; id: string; move_to?: number; width?: number; scroll: number; control: Control | null };

const LABELS: [Control, string][] = [
  ["earlier", "move earlier"],
  ["later", "move later"],
  ["smaller", "smaller"],
  ["larger", "larger"],
];
const MIN_WIDTH = 16;

function start(s: FrameStart) {
  const diagrams = document.querySelectorAll<HTMLElement>("pre.mermaid");
  if (diagrams.length) {
    // The same mermaid set-up as the post page (App.tsx).
    import("mermaid").then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "neutral",
        fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
      });
      mermaid.run({ nodes: diagrams });
    });
  }
  const article = document.querySelector("article");
  if (!article) return;
  const post = (edit: Omit<FrameEdit, "token" | "scroll">) => parent.postMessage({ ...edit, token: s.token, scroll: scrollY }, s.origin);

  // Where an image can land: before each top-level block, marked by the server with <!--ween-block:N-->.
  const markers: { block: number; y: () => number }[] = [];
  const walker = document.createTreeWalker(article, NodeFilter.SHOW_COMMENT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const found = /^ween-block:(\d+)$/.exec(node.nodeValue ?? "");
    if (found) markers.push({ block: Number(found[1]), y: edgeOf(node) });
  }

  // Escape cancels whatever drag or resize is running.
  let cancel: (() => void) | null = null;
  addEventListener("keydown", (e) => {
    if (e.key === "Escape" && cancel) {
      e.preventDefault();
      cancel();
    }
  });

  for (const meta of s.images) {
    const img = [...article.querySelectorAll("img")].find((i) => i.getAttribute("src") === `/media/${meta.id}/lg.webp`);
    if (!img) continue;
    if (s.local[meta.id]) img.src = s.local[meta.id];
    // Each move lands just before block `earlier` or `later`; for an image alone in its block, landing next to
    // itself is no move.
    const earlier = meta.standalone ? meta.block - 1 : meta.block;
    const later = meta.standalone ? meta.block + 2 : meta.block + 1;
    const canLand = (block: number) => block >= 0 && block <= s.blocks && !(meta.standalone && (block === meta.block || block === meta.block + 1));

    const box = document.createElement("span");
    box.className = "ween-image";
    box.tabIndex = 0;
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", img.alt || "image");
    img.replaceWith(box);
    box.append(img);
    img.draggable = false;

    const widest = () => Math.max(MIN_WIDTH, Math.min(img.naturalWidth || Infinity, article.clientWidth));
    const shown = () => img.clientWidth; // the CSS width, without the border
    const step = () => Math.max(1, Math.round(widest() / 10));
    // A size step waits for the image: until it loads there is no width to start from.
    const loaded = (then: () => void) => (img.complete && img.naturalWidth ? then() : img.addEventListener("load", then, { once: true }));
    const resize = (width: number, control: Control) => {
      const next = Math.round(Math.min(widest(), Math.max(MIN_WIDTH, width)));
      if (next !== (meta.width ?? shown())) post({ id: meta.id, width: next, control });
    };

    const tools = document.createElement("span");
    tools.className = "ween-tools";
    for (const [control, label] of LABELS) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.dataset.control = control;
      button.disabled = (control === "earlier" && !canLand(earlier)) || (control === "later" && !canLand(later));
      button.addEventListener("click", () => {
        if (control === "earlier" || control === "later") post({ id: meta.id, move_to: control === "earlier" ? earlier : later, control });
        else loaded(() => resize(shown() + (control === "larger" ? step() : -step()), control));
      });
      tools.append(button);
    }

    const handle = document.createElement("span");
    handle.className = "ween-handle";
    handle.tabIndex = 0;
    handle.dataset.control = "width";
    handle.setAttribute("role", "slider");
    handle.setAttribute("aria-label", "width");
    const describe = () => {
      handle.setAttribute("aria-valuemin", String(MIN_WIDTH));
      handle.setAttribute("aria-valuemax", String(widest()));
      handle.setAttribute("aria-valuenow", String(shown()));
    };
    if (img.complete) describe();
    else img.addEventListener("load", describe, { once: true });
    handle.addEventListener("keydown", (e) => {
      const sign = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[e.key];
      if (!sign) return;
      e.preventDefault();
      loaded(() => resize(shown() + sign * step(), "width"));
    });
    box.append(tools, handle);

    // Resize: the corner handle follows the pointer; one width is sent on release.
    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      const [x, from] = [e.clientX, shown()];
      let width = from;
      const move = (m: PointerEvent) => {
        width = Math.min(widest(), Math.max(MIN_WIDTH, from + m.clientX - x));
        img.style.width = `${width}px`;
      };
      const end = (commit: boolean) => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", lost);
        cancel = null;
        if (commit && Math.round(width) !== from) resize(width, "width");
        else img.style.width = "";
      };
      const up = () => end(true);
      const lost = () => end(false);
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", lost);
      cancel = lost;
    });

    // Move: drag the image; a line shows the nearest place it can land, and releasing there moves it.
    img.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      img.setPointerCapture(e.pointerId);
      const [x, y] = [e.clientX, e.clientY];
      let target: number | null = null;
      let line: HTMLElement | null = null;
      const move = (m: PointerEvent) => {
        if (!line && Math.hypot(m.clientX - x, m.clientY - y) < 4) return;
        // Near the frame's top or bottom edge the preview scrolls, so the image can go anywhere in a long post.
        if (m.clientY < 40) scrollBy(0, -24);
        else if (m.clientY > innerHeight - 40) scrollBy(0, 24);
        if (!line) {
          line = document.body.appendChild(document.createElement("div"));
          line.className = "ween-drop";
          box.classList.add("ween-dragging");
        }
        const places = markers.filter((p) => canLand(p.block)).map((p) => ({ block: p.block, y: p.y() }));
        const near = places.reduce<(typeof places)[number] | null>((best, p) => (!best || Math.abs(p.y - m.clientY) < Math.abs(best.y - m.clientY) ? p : best), null);
        target = near?.block ?? null;
        line.hidden = !near;
        if (near) {
          const r = article.getBoundingClientRect();
          Object.assign(line.style, { top: `${near.y + scrollY - 1}px`, left: `${r.left + scrollX}px`, width: `${r.width}px` });
        }
      };
      const end = (commit: boolean) => {
        img.removeEventListener("pointermove", move);
        img.removeEventListener("pointerup", up);
        img.removeEventListener("pointercancel", lost);
        cancel = null;
        line?.remove();
        box.classList.remove("ween-dragging");
        if (commit && target !== null) post({ id: meta.id, move_to: target, control: null });
      };
      const up = () => end(true);
      const lost = () => end(false);
      img.addEventListener("pointermove", move);
      img.addEventListener("pointerup", up);
      img.addEventListener("pointercancel", lost);
      cancel = lost;
    });

    if (s.focus?.id === meta.id) {
      const again = s.focus.control === "width" ? handle : tools.querySelector<HTMLButtonElement>(`[data-control="${s.focus.control}"]`);
      (again && !(again as HTMLButtonElement).disabled ? again : box).focus();
    }
  }
  scrollTo(0, s.scroll);
}

// The page y of the gap a block marker stands for: the top of the next element, or the bottom of the one before.
function edgeOf(marker: Node): () => number {
  let next = marker.nextSibling;
  while (next && !(next instanceof Element)) next = next.nextSibling;
  let before = marker.previousSibling;
  while (before && !(before instanceof Element)) before = before.previousSibling;
  return () => {
    if (next) return next.getBoundingClientRect().top - Math.min(8, parseFloat(getComputedStyle(next).marginTop) / 2);
    if (before) return before.getBoundingClientRect().bottom + Math.min(8, parseFloat(getComputedStyle(before).marginBottom) / 2);
    return marker.parentElement?.getBoundingClientRect().top ?? 0;
  };
}

// One object, so the bundler keeps start in this chunk although only the frame calls it.
export const frame = { url: import.meta.url, start };
