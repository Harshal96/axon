/* Axon base — tiny shared utilities (copied from Arc UI/base) (no React, no DOM assumptions
   beyond what each function states). Keep this file boring. */

/** Join class names, dropping falsy parts: cx("btn", on && "primary"). */
export const cx = (...parts: (string | false | null | undefined)[]): string =>
  parts.filter(Boolean).join(" ");

export const clamp = (v: number, lo: number, hi: number): number =>
  Math.min(hi, Math.max(lo, v));

/** 1234 → "1,234"; safe for counts everywhere. */
export const fmtNum = (n: number): string => n.toLocaleString("en-US");

/** Tokens in K with one decimal below 10K: 8400 → "8.4k". */
export const fmtK = (tokens: number): string =>
  tokens >= 10_000 ? `${Math.round(tokens / 1000)}k` : `${(tokens / 1000).toFixed(1)}k`;

/** USD: $4 / $4.20 (drops trailing .00 like the model picker). */
export const fmtUsd = (n: number): string => `$${n % 1 ? n.toFixed(2) : n.toFixed(0)}`;

/** Seconds → "45s" / "2m 5s". */
export const fmtSec = (sec: number): string =>
  sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m ${sec % 60}s`;

/** Two-letter initials for avatars: "Harshal Parekh" → "HP". */
export const initials = (name: string): string =>
  name.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]!.toUpperCase()).join("");

export const plural = (n: number, noun: string): string => `${n} ${noun}${n === 1 ? "" : "s"}`;
