/**
 * UI theme selection.
 *
 * A theme only redefines the token blocks in `styles.css`; no component is theme-aware.
 * `light` is the default VS Code-Light palette, `paper` is the warm reading palette.
 *
 * The in-product switch lives in the sidebar Settings menu (Appearance); `?theme=`
 * still wins for one session so a screenshot or a smoke run can pin a palette.
 */

export type ThemeName = "light" | "paper";

export const UI_THEME_STORAGE_KEY = "derivationlab.ui.theme.v1";

export const DEFAULT_THEME: ThemeName = "paper";

const THEME_NAMES: readonly string[] = ["light", "paper"];

function isThemeName(value: string | null | undefined): value is ThemeName {
  return typeof value === "string" && THEME_NAMES.includes(value);
}

/**
 * Resolve the theme for this session: an explicit `?theme=` wins over the stored
 * preference, and anything unrecognised falls back to the default. Never throws —
 * a malformed URL or unavailable storage degrades to the default theme.
 */
export function readStoredTheme(): ThemeName {
  if (typeof window === "undefined") return DEFAULT_THEME;
  try {
    const requested = new URL(window.location.href).searchParams.get("theme");
    if (isThemeName(requested)) return requested;
  } catch {
    /* malformed location: fall through to the stored preference */
  }
  try {
    const stored = window.localStorage.getItem(UI_THEME_STORAGE_KEY);
    if (isThemeName(stored)) return stored;
  } catch {
    /* storage unavailable (private mode, blocked cookies): use the default */
  }
  return DEFAULT_THEME;
}

/** Apply a theme by stamping `data-theme` on `<html>`, the same hook as `data-product-host`. */
export function applyTheme(name: ThemeName): ThemeName {
  if (typeof document !== "undefined") document.documentElement.dataset.theme = name;
  return name;
}

/** Persist a theme choice for the next session. Never throws when storage is unavailable. */
export function writeStoredTheme(name: ThemeName): void {
  try {
    window.localStorage.setItem(UI_THEME_STORAGE_KEY, name);
  } catch {
    /* storage unavailable: the choice applies to this session only */
  }
}
