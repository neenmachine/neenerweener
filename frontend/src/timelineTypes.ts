// The public timeline entry, as /api/timeline and the create endpoint return it.
export type Media = { id: string; width: number; height: number; alt: string };
// The server names the Toronto zone of each time; Intl can print "GMT-5" instead, so the client never derives it.
export type Zone = "EST" | "EDT";
export type Entry = {
  id: number;
  html: string;
  created_at: string;
  created_zone: Zone;
  edited_at: string | null;
  edited_zone: Zone | null;
  media: Media[];
};
// A page of /api/timeline; now is the server's clock for every relative time on the page.
export type Timeline = { entries: Entry[]; next_cursor: number | null; now: string };
// GET /api/profile: the poster shown on every entry.
export type Profile = { display_name: string; handle: string | null; avatar_id: string | null; avatar_initial: string };
