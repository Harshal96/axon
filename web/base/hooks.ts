/* Axon base — shared React hooks (copied from Arc UI/base). A hook earns its place here only
   when two or more components need it; one-offs stay local. */
import { useCallback, useEffect, useRef, useState } from "react";

/** Escape + outside-click dismissal for popovers/menus/modals.
    Attach the returned ref to the floating element. */
export function useDismiss<T extends HTMLElement>(open: boolean, onDismiss: () => void) {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onDismiss(); };
    const onPointer = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onDismiss();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer, true);
    };
  }, [open, onDismiss]);
  return ref;
}

/** Live `(prefers-reduced-motion: reduce)` — JS-side twin of the CSS rule. */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/** One-shot reduced-motion read (event handlers, non-render code). */
export const prefersReducedMotion = (): boolean =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** useState persisted to localStorage (theme, sidebar, view prefs). */
export function useStoredState<T extends string>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => (localStorage.getItem(key) as T) || initial);
  const set = useCallback((v: T) => { setValue(v); localStorage.setItem(key, v); }, [key]);
  return [value, set];
}

/** Animated number that eases toward its target (honours reduced motion). */
export function useAnimatedNumber(target: number, durationMs = 350): number {
  const [value, setValue] = useState(target);
  const fromRef = useRef(target);
  useEffect(() => {
    if (prefersReducedMotion() || fromRef.current === target) { fromRef.current = target; setValue(target); return; }
    const from = fromRef.current, start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3);
      const v = from + (target - from) * eased;
      fromRef.current = v; setValue(v);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, durationMs]);
  return value;
}
