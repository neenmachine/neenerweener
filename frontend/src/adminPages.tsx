import { type ComponentType, lazy } from "react";
import type { Result } from "./adminApi";
import type { Send } from "./EntryEditor";
import type { Profile } from "./timelineTypes";

// The admin pages: the one client list of path, nav label and page. Admin.tsx draws the nav from it and shows the
// page for the current path. A new page is one row here, one page file, the same path in ADMIN_PAGES in
// backend/routes.py and one admin_pages row in route_cases.json (tests hold all three equal).

// What the shell gives every page. profile is null until the first loadProfile() answers.
export type AdminPageProps = {
  send: Send;
  expire: () => void;
  profile: Profile | null;
  loadProfile: () => Promise<Result<Profile>>;
};

export type AdminPage = { path: string; label: string; Page: ComponentType<AdminPageProps> };

// Each page is its own lazy chunk, loaded the first time it is shown.
export const adminPages: AdminPage[] = [
  { path: "/admin", label: "overview", Page: lazy(() => import("./AdminOverview")) },
  { path: "/admin/timeline", label: "timeline", Page: lazy(() => import("./AdminTimeline")) },
  { path: "/admin/profile", label: "profile", Page: lazy(() => import("./AdminProfile")) },
];
