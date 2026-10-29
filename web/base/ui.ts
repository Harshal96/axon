/* Axon base — UI-only types (copied from Arc UI/base) & tone vocabulary.
   Domain data lives in types.ts; anything that only describes how
   something LOOKS or is routed belongs here so components never have
   to import UI types from each other. */

/** Meaning tones — map 1:1 to token colours in tokens.css. */
export type Tone = "ok" | "risk" | "off" | "info" | "accent" | "neutral";

export const toneColor: Record<Tone, string> = {
  ok: "var(--ok)", risk: "var(--risk)", off: "var(--off)",
  info: "var(--info)", accent: "var(--accent)", neutral: "var(--tx-2)",
};

/** Colour-mixed translucent fill for a tone (badge/dot backgrounds). */
export const toneBg = (tone: Tone, pct = 15): string =>
  `color-mix(in srgb, ${toneColor[tone]} ${pct}%, transparent)`;

export type Size = "sm" | "md" | "lg";

/** Theme preference. "system" = no data-theme attribute (gr8r rule).
    "colour" = system luminance + one user-picked hue hint (Arc THEME.md). */
export type Theme = "light" | "dark" | "system" | "colour";
export const THEME_STORAGE_KEY = "axon-theme";
export const THEME_HUE_STORAGE_KEY = "axon-hue";

/** Default hint hue — Arc green, the hue of the stock --accent (#3ddc97). */
export const DEFAULT_HUE = 154;

/** Curated hint swatches. Only the HUE is ever stored: lightness and
    saturation are clamped per luminance in tokens.css, so every hue —
    curated or custom — stays readable. Names are for aria-labels. */
export const HUE_SWATCHES: { name: string; hue: number }[] = [
  { name: "Axon green", hue: 154 },
  { name: "Teal", hue: 185 },
  { name: "Sky", hue: 208 },
  { name: "Blue", hue: 228 },
  { name: "Violet", hue: 265 },
  { name: "Magenta", hue: 318 },
  { name: "Rose", hue: 348 },
  { name: "Amber", hue: 36 },
];

/** Normalise any stored/entered hue to an integer in [0, 360). */
export const clampHue = (h: number): number => ((Math.round(h) % 360) + 360) % 360;

/** "#3ddc97" → 154. Grey inputs (no hue) fall back to the default. */
export function hexToHue(hex: string): number {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return DEFAULT_HUE;
  const n = parseInt(m[1], 16);
  const r = (n >> 16 & 255) / 255, g = (n >> 8 & 255) / 255, b = (n & 255) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  if (d === 0) return DEFAULT_HUE;
  const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return clampHue(h * 60);
}

/** Hue → hex at the dark-mode clamp (for the native colour input value). */
export function hueToHex(hue: number, s = 0.7, l = 0.55): string {
  const h = clampHue(hue) / 360;
  const f = (p: number, q: number, t: number) => {
    const x = ((t % 1) + 1) % 1;
    return x < 1 / 6 ? p + (q - p) * 6 * x : x < 1 / 2 ? q : x < 2 / 3 ? p + (q - p) * (2 / 3 - x) * 6 : p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const to = (v: number) => Math.round(v * 255).toString(16).padStart(2, "0");
  return `#${to(f(p, q, h + 1 / 3))}${to(f(p, q, h))}${to(f(p, q, h - 1 / 3))}`;
}

/** A CSS colour for a swatch at the dark clamp (picker dots). */
export const swatchColor = (hue: number): string => `hsl(${clampHue(hue)} 70% 55%)`;

/** Apply theme + hue to <html>. System removes data-theme; colour sets
    data-theme="colour" and always publishes --hint-h (other modes
    simply never read it, so switching back is instant and exact). */
export function applyTheme(theme: Theme, hue: number = DEFAULT_HUE): void {
  const r = document.documentElement;
  if (theme === "system") r.removeAttribute("data-theme");
  else r.setAttribute("data-theme", theme);
  r.style.setProperty("--hint-h", String(clampHue(hue)));
}
