// Text changes the editor makes itself (an inserted image, a preview move or resize) go in as if typed: only the
// changed range is replaced, as one step of the browser's own undo history. Writing textarea.value instead leaves the
// browser's undo entries pointing at old text, so Cmd+Z then puts stale text into the post.

export type Change = { start: number; end: number; insert: string };

// The smallest range of prev to replace to get next (common prefix and suffix kept), never splitting a surrogate pair.
export function changedRange(prev: string, next: string): Change {
  const most = Math.min(prev.length, next.length);
  let start = 0;
  while (start < most && prev[start] === next[start]) start++;
  if (start > 0 && isHigh(prev.charCodeAt(start - 1))) start--;
  let tail = 0;
  while (tail < most - start && prev[prev.length - 1 - tail] === next[next.length - 1 - tail]) tail++;
  if (tail > 0 && isLow(prev.charCodeAt(prev.length - tail))) tail--;
  return { start, end: prev.length - tail, insert: next.slice(start, next.length - tail) };
}

const isHigh = (c: number) => c >= 0xd800 && c <= 0xdbff;
const isLow = (c: number) => c >= 0xdc00 && c <= 0xdfff;

// Make el's text next as one undoable edit, and leave focus where it was. The caller syncs its state from el.value.
export function replaceText(el: HTMLTextAreaElement, next: string): void {
  if (el.value === next) return;
  const { start, end, insert } = changedRange(el.value, next);
  const focused = document.activeElement;
  const { inert, readOnly } = el;
  const { scrollX, scrollY } = window;
  const { scrollTop } = el;
  // A covered (inert, hidden) or read-only textarea takes no focus or typing; let it, for this one edit (no paint
  // happens in between).
  try {
    el.inert = false;
    el.readOnly = false;
    el.style.visibility = "visible";
    el.focus({ preventScroll: true });
    el.setSelectionRange(start, end);
    // Where the browser has no undoable insert, the text still changes (without an undo step).
    if (!document.execCommand(insert ? "insertText" : "delete", false, insert) || el.value !== next) el.value = next;
  } finally {
    el.inert = inert;
    el.readOnly = readOnly;
    el.style.visibility = "";
  }
  if (focused instanceof HTMLElement && focused !== el && focused !== document.body && focused.isConnected) {
    focused.focus({ preventScroll: true });
  } else if (inert) {
    // WebKit keeps sending keys to a focused hidden textarea even after body.focus(): drop its focus.
    el.blur();
  }
  if (inert) {
    // The edit scrolls to the hidden caret (Firefox does it after the call returns); the reader does not see it, so
    // keep the page and the textarea where they were.
    const keep = () => {
      window.scrollTo(scrollX, scrollY);
      el.scrollTop = scrollTop;
    };
    keep();
    requestAnimationFrame(keep);
  }
}
