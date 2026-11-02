/**
 * Small inline-SVG charts for the Workers pages. One series each, in the --chart token (validated
 * against the card surface in both themes), recessive hairline baseline, hover tooltips.
 */
import { useId, useState } from 'react';

const compact = (n: number) =>
  n >= 10_000 ? `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1).replace(/\.0$/, '')}K` : n.toLocaleString('en-US');

/** A 48-point (or any) sparkline: 2px line over a 10% wash. Values only, no axes. */
export function Sparkline({ values, width = 120, height = 26, label }: { values: number[]; width?: number; height?: number; label: string }) {
  const max = Math.max(1, ...values);
  const n = values.length;
  const x = (i: number) => (n <= 1 ? width : (i / (n - 1)) * (width - 2) + 1);
  const y = (v: number) => height - 2 - (v / max) * (height - 5);
  const line = values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
  const total = values.reduce((a, b) => a + b, 0);
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      <title>{label}</title>
      <line x1={0} x2={width} y1={height - 1.5} y2={height - 1.5} className="axis" />
      {total > 0 && (
        <>
          <path d={`${line}L${x(n - 1).toFixed(1)},${height - 1.5}L${x(0).toFixed(1)},${height - 1.5}Z`} className="wash" />
          <path d={line} className="line" />
        </>
      )}
    </svg>
  );
}

export interface ColumnDatum {
  key: string;
  label: string;
  value: number;
}

/**
 * Column chart over time (e.g. claims per hour). Columns are capped at 24px, with a 4px rounded
 * data end and a square baseline; hovering a slot shows its label and value.
 */
export function Columns({ data, height = 140, unit, ticks }: { data: ColumnDatum[]; height?: number; unit: string; ticks: (i: number) => string | null }) {
  const [hover, setHover] = useState<number | null>(null);
  const id = useId();
  const W = 640;
  const padT = 14;
  const padB = 20;
  const padL = 34;
  const plotH = height - padT - padB;
  const max = Math.max(1, ...data.map((d) => d.value));
  const nice = niceMax(max);
  const slot = (W - padL) / Math.max(1, data.length);
  const bw = Math.min(24, Math.max(2, slot - 2));
  const yOf = (v: number) => padT + plotH - (v / nice) * plotH;
  const h = hover !== null ? data[hover] : null;
  return (
    <div className="colchart">
      <svg viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-labelledby={`${id}-t`} onMouseLeave={() => setHover(null)}>
        <title id={`${id}-t`}>{`${data.reduce((a, d) => a + d.value, 0).toLocaleString('en-US')} ${unit} over ${data.length} buckets; peak ${max.toLocaleString('en-US')}`}</title>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={padL} x2={W} y1={yOf(nice * f)} y2={yOf(nice * f)} className={f === 0 ? 'axis' : 'grid'} />
            <text x={padL - 6} y={yOf(nice * f) + 3.5} className="tick" textAnchor="end">
              {compact(nice * f)}
            </text>
          </g>
        ))}
        {data.map((d, i) => {
          const x = padL + i * slot + (slot - bw) / 2;
          const top = yOf(d.value);
          const bh = padT + plotH - top;
          const r = Math.min(4, bw / 2, bh);
          const tick = ticks(i);
          return (
            <g key={d.key} onMouseEnter={() => setHover(i)}>
              <rect x={padL + i * slot} y={padT} width={slot} height={plotH + padB} fill="transparent" />
              {d.value > 0 && (
                <path
                  className={`col${hover === i ? ' on' : ''}`}
                  d={`M${x},${padT + plotH}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${padT + plotH}Z`}
                />
              )}
              {tick && (
                <text x={x + bw / 2} y={height - 5} className="tick" textAnchor="middle">
                  {tick}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="chart-tip" aria-live="polite" style={{ visibility: h ? 'visible' : 'hidden' }}>
        {h ? (
          <>
            <b className="num">{h.value.toLocaleString('en-US')}</b> {unit} · {h.label}
          </>
        ) : (
          ' '
        )}
      </div>
    </div>
  );
}

function niceMax(v: number): number {
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

/** Horizontal bars for a ranked list (e.g. claims by predicate), value printed at the tip. */
export function HBars({ rows, label }: { rows: { key: string; label: string; value: number }[]; label: string }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="hbars" aria-label={label}>
      {rows.map((r) => (
        <li key={r.key}>
          <span className="k mono" title={r.label}>
            {r.label}
          </span>
          <span className="track">
            <i style={{ width: `${Math.max(1, (r.value / max) * 100)}%` }} />
          </span>
          <span className="v num mono">{r.value.toLocaleString('en-US')}</span>
        </li>
      ))}
    </ul>
  );
}
