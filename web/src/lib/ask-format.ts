import type { Citation } from '../../base/types.ts';

export function toFacts(answer: string): { headline: string; bullets: string[] } {
  const parts = answer.split(/(?<=[.!?])\s+(?=[A-Z(])/).map(s => s.trim()).filter(Boolean);
  return { headline: parts[0] ?? '', bullets: parts.slice(1) };
}

export function formatDuration(seconds: number): string {
  return seconds < 1 ? `${Math.max(1, Math.round(seconds * 1000))} ms` : `${seconds.toFixed(1)} s`;
}

export function citationParts(citation: Citation): { method: string; path: string; service: string } {
  const parts = citation.node_id.split('::');
  if (parts[0] === 'endpoint') {
    const operation = parts.slice(3).join('::');
    const colon = operation.indexOf(':');
    return { method: operation.slice(0, colon), path: operation.slice(colon + 1), service: parts[2] ?? '' };
  }
  return { method: '', path: citation.label, service: '' };
}

export function interpretation(bindings: Record<string, string>[] = []): string {
  return bindings.filter(b => b.kind === 'service')
    .sort((a, b) => (a.role === 'target' ? 1 : 0) - (b.role === 'target' ? 1 : 0))
    .map(b => b.id).join(' → ')
    || bindings.map(b => b.span || b.id).join(', ');
}
