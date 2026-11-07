// The public timeline entry, as /api/timeline and the create endpoint return it.
export type Media = { id: string; width: number; height: number; alt: string };
export type Entry = { id: number; html: string; created_at: string; edited_at: string | null; media: Media[] };
