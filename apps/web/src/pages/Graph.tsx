/** /e/:id/graph — 1-hop (optionally 2-hop) neighborhood drawn with d3-force in SVG. */
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationLinkDatum } from 'd3-force';
import { useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent, type WheelEvent } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { isRedirect, type EntityView } from '../api';
import { Icon, KindGlyph, Skel, entityPath, graphPath } from '../components/bits';
import { HANDLE_LABEL, avatarOf, displayName, handleOf, kindLabel, plural } from '../lib/format';
import { HOP2_MAX_DEGREE, buildGraph, degreeOf, edgesOf, type GLink, type GNode, type Graph } from '../lib/graph';
import { useApi, useAsync, useTitle } from '../lib/hooks';
import { labelOf } from '../lib/facts';

type SimLink = SimulationLinkDatum<GNode> & GLink;

const KIND_SET = new Set(['person', 'company', 'launch', 'event', 'place']);

/** Runs the simulation to rest up front, so the drawing appears settled (no jitter, no shift). */
function layout(graph: Graph, w: number, h: number): { nodes: GNode[]; links: SimLink[] } {
  const nodes = graph.nodes.map((n) => ({ ...n }));
  const center = nodes.find((n) => n.hop === 0);
  if (center) {
    center.fx = 0;
    center.fy = 0;
  }
  // Seed positions on rings by hop so the result is stable between renders.
  nodes.forEach((n, i) => {
    if (n.hop === 0) return;
    const a = (i / nodes.length) * Math.PI * 2;
    const r = n.hop === 1 ? 160 : 300;
    n.x = Math.cos(a) * r;
    n.y = Math.sin(a) * r;
  });
  const links: SimLink[] = graph.links.map((l) => ({ ...l }));
  const n = nodes.length;
  const sim = forceSimulation<GNode>(nodes)
    .force(
      'link',
      forceLink<GNode, SimLink>(links)
        .id((d) => d.id)
        .distance((l) => (l.hop === 1 ? 110 + Math.min(80, n * 1.5) : 70))
        .strength((l) => (l.hop === 1 ? 0.7 : 0.4)),
    )
    .force('charge', forceManyBody<GNode>().strength((d) => (d.hop === 0 ? -900 : d.hop === 1 ? -260 : -120)))
    .force('collide', forceCollide<GNode>().radius((d) => (d.hop === 0 ? 46 : d.more ? 30 : 26)))
    .force('x', forceX<GNode>(0).strength(0.04 * (h / w)))
    .force('y', forceY<GNode>(0).strength(0.06 * (w / h)))
    .force('center', forceCenter(0, 0))
    .stop();
  const ticks = Math.ceil(Math.log(sim.alphaMin()) / Math.log(1 - sim.alphaDecay()));
  for (let i = 0; i < ticks; i++) sim.tick();
  return { nodes, links };
}

function NodeShape({ n }: { n: GNode }) {
  const r = n.hop === 0 ? 13 : n.hop === 1 ? 7.5 : 5;
  if (n.more) return <circle r={15} />;
  switch (n.kind) {
    case 'company':
      return <rect x={-r} y={-r} width={r * 2} height={r * 2} rx={1.5} />;
    case 'launch':
      return <path d={`M0 ${-r * 1.15} L${r * 1.05} ${r * 0.8} L${-r * 1.05} ${r * 0.8} Z`} />;
    case 'event':
      return <rect x={-r * 0.85} y={-r * 0.85} width={r * 1.7} height={r * 1.7} transform="rotate(45)" />;
    default:
      return <circle r={r} />;
  }
}

/** An avatar clipped into the node's circle; the kind shape stays underneath as its ring. */
function NodeAvatar({ n }: { n: GNode }) {
  const r = n.hop === 0 ? 12 : n.hop === 1 ? 7 : 4.5;
  const clip = `av-${n.id.replace(/[^a-z0-9]/gi, '-')}`;
  return (
    <g className="g-avatar" aria-hidden="true">
      <clipPath id={clip}>
        <circle r={r} />
      </clipPath>
      <image href={avatarOf(n)!} x={-r} y={-r} width={r * 2} height={r * 2} clipPath={`url(#${clip})`} preserveAspectRatio="xMidYMid slice" />
    </g>
  );
}

async function fetchSecondHop(client: ReturnType<typeof useApi>, ids: string[], signal: AbortSignal): Promise<EntityView[]> {
  const out: EntityView[] = [];
  const queue = [...ids];
  const worker = async () => {
    while (queue.length) {
      const id = queue.shift()!;
      try {
        const r = await client.entity(id, { signal });
        out.push(isRedirect(r) ? r.entity : r);
      } catch {
        // a neighbor that fails to load just has no 2nd hop
      }
    }
  };
  await Promise.all([worker(), worker(), worker()]);
  return out;
}

export default function GraphPage() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const hops = params.get('hops') === '2' ? 2 : 1;
  const client = useApi();
  const navigate = useNavigate();

  const center = useAsync(`entity:${id}`, (signal) => client.entity(id, { signal }));
  const view = center.data ? (isRedirect(center.data) ? center.data.entity : center.data) : undefined;
  useTitle(view ? `${displayName(view)} — graph` : 'Graph');

  useEffect(() => {
    if (center.data && isRedirect(center.data)) navigate(`${graphPath(center.data.redirect)}${hops === 2 ? '?hops=2' : ''}`, { replace: true });
  }, [center.data, navigate, hops]);

  const degree = view ? degreeOf(view) : 0;
  const canHop2 = degree > 0 && degree <= HOP2_MAX_DEGREE;
  const neighborIds = useMemo(() => (view ? [...new Set(edgesOf(view).map((e) => e.other.id))] : []), [view]);
  const second = useAsync(view && hops === 2 && canHop2 ? `hop2:${view.id}` : null, (signal) => fetchSecondHop(client, neighborIds, signal));

  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 900, h: 600 });
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(([e]) => {
      if (e) setSize({ w: Math.max(280, e.contentRect.width), h: Math.max(320, e.contentRect.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const graph = useMemo(() => (view ? buildGraph(view, hops === 2 ? (second.data ?? []) : []) : null), [view, hops, second.data]);
  // Layout depends on the graph, and only coarsely on the aspect ratio.
  const aspect = Math.round((size.w / size.h) * 4) / 4;
  const laid = useMemo(() => (graph ? layout(graph, aspect * 600, 600) : null), [graph, aspect]);

  // Pan/zoom via the viewBox.
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  useEffect(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [id, hops]);
  const fit = useMemo(() => {
    if (!laid) return { w: size.w, h: size.h };
    let maxX = 120;
    let maxY = 120;
    for (const n of laid.nodes) {
      maxX = Math.max(maxX, Math.abs(n.x ?? 0) + 90);
      maxY = Math.max(maxY, Math.abs(n.y ?? 0) + 40);
    }
    const s = Math.max((maxX * 2) / size.w, (maxY * 2) / size.h, 0.6);
    return { w: size.w * s, h: size.h * s };
  }, [laid, size]);
  const vw = fit.w / zoom;
  const vh = fit.h / zoom;
  const viewBox = `${-vw / 2 + pan.x} ${-vh / 2 + pan.y} ${vw} ${vh}`;

  const drag = useRef<{ x: number; y: number; px: number; py: number; moved: boolean } | null>(null);
  const onPointerDown = (e: RPointerEvent<SVGSVGElement>) => {
    if ((e.target as Element).closest('.g-node')) return;
    drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: RPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const k = vw / size.w;
    if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) > 3) d.moved = true;
    setPan({ x: d.px - (e.clientX - d.x) * k, y: d.py - (e.clientY - d.y) * k });
  };
  const onPointerUp = () => {
    drag.current = null;
  };
  const onWheel = (e: WheelEvent<SVGSVGElement>) => {
    if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) < 40) return;
    setZoom((z) => Math.min(4, Math.max(0.4, z * (e.deltaY > 0 ? 0.9 : 1.1))));
  };

  const [hover, setHover] = useState<string | null>(null);
  const hl = useMemo(() => {
    if (!hover || !laid) return null;
    const ids = new Set([hover]);
    for (const l of laid.links) {
      const s = typeof l.source === 'object' ? (l.source as GNode).id : String(l.source);
      const t = typeof l.target === 'object' ? (l.target as GNode).id : String(l.target);
      if (s === hover) ids.add(t);
      if (t === hover) ids.add(s);
    }
    return ids;
  }, [hover, laid]);
  const hoverNode = laid?.nodes.find((n) => n.id === hover) ?? null;
  const hoverEdges = useMemo(() => {
    if (!hover || !laid || !view || hover === view.id) return [];
    return laid.links
      .filter((l) => {
        const s = (l.source as GNode).id;
        const t = (l.target as GNode).id;
        return (s === hover && t === view.id) || (t === hover && s === view.id);
      })
      .map((l) => labelOf(l.predicate));
  }, [hover, laid, view]);

  const open = (n: GNode) => {
    if (n.more) navigate(entityPath(n.more.owner));
    else navigate(graphPath(n.id) + (hops === 2 ? '?hops=2' : ''));
  };

  const kindsPresent = laid ? [...new Set(laid.nodes.filter((n) => !n.more).map((n) => n.kind))] : [];

  return (
    <div className="wrap graph-page">
      <div className="graph-bar">
        <Link to={entityPath(view?.id ?? id)} className="btn ghost sm" aria-label="Back to entity">
          <Icon name="back" />
        </Link>
        {view ? (
          <h1>
            <Link to={entityPath(view.id)}>{displayName(view)}</Link>
          </h1>
        ) : (
          <Skel w={220} h={24} />
        )}
        <span className="muted" style={{ fontSize: 12.5 }}>
          {view ? plural(degree, 'neighbor') : ''}
          {second.loading ? ' · loading 2nd hop…' : ''}
        </span>
        <span className="spacer" />
        <div className="seg" role="group" aria-label="Depth">
          <button type="button" aria-pressed={hops === 1} onClick={() => setParams({}, { replace: true })}>
            1 hop
          </button>
          <button
            type="button"
            aria-pressed={hops === 2}
            disabled={!canHop2}
            title={canHop2 ? 'Add neighbors of neighbors' : `2 hops is available up to ${HOP2_MAX_DEGREE} neighbors`}
            onClick={() => setParams({ hops: '2' }, { replace: true })}
          >
            2 hops
          </button>
        </div>
      </div>

      <div className="graph-canvas" ref={wrapRef} aria-busy={!laid}>
        {center.error && <p className="empty" style={{ padding: 20 }}>Could not load: {center.error.message}</p>}
        {laid && view && (
          <svg
            viewBox={viewBox}
            preserveAspectRatio="xMidYMid meet"
            role="img"
            aria-label={`Neighborhood of ${displayName(view)}`}
            className={hl ? 'dim' : ''}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onWheel={onWheel}
          >
            <g>
              {laid.links.map((l, i) => {
                const s = l.source as GNode;
                const t = l.target as GNode;
                const on = hl && hl.has(s.id) && hl.has(t.id) && (s.id === hover || t.id === hover);
                return (
                  <line
                    key={i}
                    className={`g-link${l.hop === 2 ? ' hop2' : ''}${on ? ' hl' : ''}`}
                    x1={s.x}
                    y1={s.y}
                    x2={t.x}
                    y2={t.y}
                  />
                );
              })}
            </g>
            {hover &&
              laid.links
                .filter((l) => (l.source as GNode).id === hover || (l.target as GNode).id === hover)
                .slice(0, 12)
                .map((l, i) => {
                  const s = l.source as GNode;
                  const t = l.target as GNode;
                  return (
                    <text key={`lbl${i}`} className="g-edge-label" x={((s.x ?? 0) + (t.x ?? 0)) / 2} y={((s.y ?? 0) + (t.y ?? 0)) / 2} textAnchor="middle">
                      {l.predicate}
                    </text>
                  );
                })}
            <g>
              {laid.nodes.map((n) => {
                const kind = n.more ? 'more' : KIND_SET.has(n.kind) ? n.kind : 'other';
                const handle = n.more ? null : handleOf(n);
                const label = n.more ? `+${n.more.count}` : handle ? `@${displayName(n).replace(/^@/, '')}` : displayName(n);
                const short = label.length > 26 ? `${label.slice(0, 25)}…` : label;
                return (
                  <g
                    key={n.id}
                    className={`g-node${n.hop === 0 ? ' center' : ''}${n.more ? ' more' : ''}${hl?.has(n.id) ? ' hl' : ''}`}
                    data-kind={kind}
                    transform={`translate(${n.x ?? 0},${n.y ?? 0})`}
                    tabIndex={0}
                    role="link"
                    aria-label={n.more ? `${n.more.count} more ${n.more.predicate}` : `${displayName(n)}, ${n.kind}`}
                    onClick={() => open(n)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        open(n);
                      }
                    }}
                    onMouseEnter={() => setHover(n.id)}
                    onMouseLeave={() => setHover(null)}
                    onFocus={() => setHover(n.id)}
                    onBlur={() => setHover(null)}
                  >
                    <NodeShape n={n} />
                    {!n.more && avatarOf(n) && <NodeAvatar n={n} />}
                    {n.more ? (
                      <text textAnchor="middle" dy="3.5">
                        {short}
                      </text>
                    ) : (
                      <text textAnchor="middle" dy={n.hop === 0 ? 32 : n.hop === 1 ? 21 : 16} style={n.hop === 2 ? { fontSize: 9.5 } : undefined}>
                        {short}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          </svg>
        )}
        {!laid && !center.error && (
          <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}>
            <Skel w={120} h={120} style={{ borderRadius: '50%' }} />
          </div>
        )}
        {kindsPresent.length > 0 && (
          <div className="graph-legend" aria-hidden="true">
            {kindsPresent.map((k) => (
              <span key={k} className="kind" data-kind={k}>
                <KindGlyph kind={k} />
                {kindLabel(k)}
              </span>
            ))}
          </div>
        )}
        <div className="graph-zoom">
          <button type="button" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(4, z * 1.25))}>
            +
          </button>
          <button type="button" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(0.4, z / 1.25))}>
            −
          </button>
          <button
            type="button"
            aria-label="Reset view"
            onClick={() => {
              setZoom(1);
              setPan({ x: 0, y: 0 });
            }}
          >
            ⌂
          </button>
        </div>
        {hoverNode && !hoverNode.more && (
          <div className="graph-hover">
            <b>{displayName(hoverNode)}</b>
            <span style={{ opacity: 0.7 }}>
              {handleOf(hoverNode) ? `${HANDLE_LABEL[handleOf(hoverNode)!]} · ` : ''}
              {hoverNode.kind}
              {hoverEdges.length ? ` · ${hoverEdges.join(', ')}` : ''}
            </span>
          </div>
        )}
        {hoverNode?.more && (
          <div className="graph-hover">
            <b>{plural(hoverNode.more.count, 'more')}</b>
            <span style={{ opacity: 0.7 }}>{hoverNode.more.predicate} — open the entity page for the full list</span>
          </div>
        )}
      </div>
      <p className="hint-line">
        <span>Click a node to re-center on it.</span>
        <span>Drag the background to pan; ctrl/⌘ + scroll or the buttons to zoom.</span>
        <span>Hubs show at most 12 neighbors per relation.</span>
      </p>
    </div>
  );
}
