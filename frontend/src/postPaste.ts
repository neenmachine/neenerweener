// Images pasted or dropped into the post markdown: which files to upload, and where their markdown goes.

// The image files of a paste or a drop, in their order. None: the browser's own paste or drop (plain text) goes on.
export const imageFiles = (data: DataTransfer | null): File[] => [...(data?.files ?? [])].filter((f) => f.type.startsWith("image/"));

// Empty alt on purpose: Henry writes it himself.
export const imageMarkdown = (id: string) => `![](/media/${id}/lg.webp)`;

// The text and selection when the images came. Henry may type while they upload, so insertAt places the markdown
// in the text as it is now: over the selection if nothing changed, else at the same place relative to what he typed.
export type Spot = { text: string; start: number; end: number };

export function insertAt(now: string, spot: Spot, insert: string): { text: string; caret: number } {
  const { text, start, end } = spot;
  const before = text.slice(0, start);
  const after = text.slice(end);
  const at =
    now === text
      ? start
      : now.startsWith(before) // the change came after the spot
        ? start
        : now.endsWith(after) // the change came before it
          ? now.length - after.length
          : now.length;
  const until = now === text ? end : at;
  return { text: now.slice(0, at) + insert + now.slice(until), caret: at + insert.length };
}
