import type { Zone } from "./timelineTypes";

// The site's one time zone; compact_timestamp and full_timestamp in backend/time_format.py mirror these fields.
const toronto = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Toronto",
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
});
const fields = (iso: string) =>
  Object.fromEntries(toronto.formatToParts(new Date(iso)).map((part) => [part.type, part.value])) as Record<Intl.DateTimeFormatPartTypes, string>;

export function compact(iso: string, now: string) {
  const age = (Date.parse(now) - Date.parse(iso)) / 1000;
  if (age < 60) return "now";
  if (age < 3600) return `${Math.floor(age / 60)}m`;
  if (age < 86400) return `${Math.floor(age / 3600)}h`;
  const date = fields(iso);
  return `${date.month.slice(0, 3).toLowerCase()} ${date.day}${date.year === fields(now).year ? "" : `, ${date.year}`}`;
}

export function full(iso: string, zone: Zone) {
  const date = fields(iso);
  return `${date.month} ${date.day}, ${date.year} at ${date.hour}:${date.minute} ${date.dayPeriod} ${zone}`;
}
