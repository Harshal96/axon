/* Primitives — typed React wrappers over base/primitives.css classes.
   Rule: these add TYPE SAFETY + composition, never new visuals. The CSS
   class is the source of truth for look; if you need a new look, add a
   variant class in primitives.css first, then expose it here. */
import React from "react";
import { cx } from "../../base/utils";
import type { Tone } from "../../base/ui";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "primary" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
}
export function Button({ variant = "default", size = "md", className, type = "button", ...rest }: ButtonProps) {
  return <button type={type} className={cx("btn", variant !== "default" && variant, size !== "md" && size, className)} {...rest} />;
}

export interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Required: icon-only buttons must always name themselves. */
  label: string;
  ring?: boolean;
  size?: "sm" | "md";
}
export function IconButton({ label, ring, size = "md", className, type = "button", ...rest }: IconButtonProps) {
  return <button type={type} aria-label={label} title={label}
    className={cx("iconbtn", ring && "ring", size === "sm" && "sm", className)} {...rest} />;
}

export function Chip({ tone, mono = true, className, ...rest }: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone; mono?: boolean }) {
  return <span className={cx("chip", !mono && "sans", tone && tone !== "neutral" && `tone-${tone}`, className)} {...rest} />;
}

export function Pill({ on, className, type = "button", ...rest }: React.ButtonHTMLAttributes<HTMLButtonElement> & { on?: boolean }) {
  return <button type={type} aria-pressed={on} className={cx("pill", on && "on", className)} {...rest} />;
}

/** Filter/tab row of pills — the Active/Backlog/All pattern. */
export function PillTabs<T extends string>({ options, value, onChange, ariaLabel }: {
  options: readonly T[]; value: T; onChange: (v: T) => void; ariaLabel?: string;
}) {
  return (
    <span role="group" aria-label={ariaLabel} style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
      {options.map(o => <Pill key={o} on={value === o} onClick={() => onChange(o)}>{o}</Pill>)}
    </span>
  );
}

/** Segmented switch (.switch in primitives.css).
    `labels` maps values to display text; `block` stretches full-width. */
export function Segmented<T extends string>({ options, value, onChange, ariaLabel, labels, block }: {
  options: readonly T[]; value: T; onChange: (v: T) => void; ariaLabel: string;
  labels?: Partial<Record<T, string>>;
  block?: boolean;
}) {
  return (
    <span className="switch" role="group" aria-label={ariaLabel} style={block ? { display: "flex", width: "100%" } : undefined}>
      {options.map((o) => (
        <button key={o} type="button" aria-pressed={value === o} style={block ? { flex: 1 } : undefined}
          className={cx(value === o && "on")}
          onClick={() => onChange(o)}>{labels?.[o] ?? o}</button>
      ))}
    </span>
  );
}

export function Avatar({ name, color, size = "md" }: { name: string; color: string; size?: "md" | "lg" }) {
  const initial = name.trim()[0] ?? "?";
  return <span className={cx("avatar", size === "lg" && "lg")} style={{ background: color }} title={name} aria-label={name}>{initial}</span>;
}

export function Dot({ tone, color, size = 7 }: { tone?: Tone; color?: string; size?: number }) {
  return <span className={cx("dot", tone && tone !== "neutral" && `tone-${tone}`)}
    style={{ ...(color ? { background: color } : null), width: size, height: size }} aria-hidden />;
}

/** Spinner — wrap any lucide loader icon; uses the global .spin keyframes. */
export function Spinner({ children, label = "Loading" }: { children: React.ReactNode; label?: string }) {
  return <span className="spin" role="status" aria-label={label} style={{ display: "inline-flex" }}>{children}</span>;
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="key">{children}</kbd>;
}
