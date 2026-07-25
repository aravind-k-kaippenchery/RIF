import { useEffect, useMemo, useState } from 'react';
import { BookOpen, Boxes, Database, KeyRound, Link2, RefreshCw, ShieldCheck, X } from 'lucide-react';
import { api } from '../lib/api';
import type { LocalUser } from '../lib/types';
import { Empty, SectionHeading, StatusBadge, TableSkeleton, asRecord, stringValue } from '../App';
import type { Toast } from '../App';

type Props = { user: LocalUser; toast: (message: string, type?: Toast['type']) => void };

const NODE_W = 208;
const NODE_H = 152;
const GAP_X = 56;
const GAP_Y = 64;
const COLS = 4;
const PAD = 30;

export default function SchemaGraphPage({ user, toast }: Props) {
  const [loading, setLoading] = useState(true);
  const [tables, setTables] = useState<any[]>([]);
  const [relationships, setRelationships] = useState<any[]>([]);
  const [glossary, setGlossary] = useState<any[]>([]);
  const [selected, setSelected] = useState<any | null>(null);
  const [showGlossary, setShowGlossary] = useState(false);

  useEffect(() => {
    void (async () => {
      setLoading(true);
      try {
        const [contract, glossaryResponse] = await Promise.all([
          api.get('/api/schema', user.role),
          api.get('/api/schema/glossary', user.role),
        ]);
        const data = asRecord(contract.data);
        const ordered = [...(data.business_tables || []), ...(data.operational_tables || [])];
        const byName = new Map((data.tables || []).map((table: any) => [table.table_name, table]));
        setTables(ordered.map((name) => byName.get(name)).filter(Boolean));
        setRelationships((data.relationships || []) as any[]);
        setGlossary((glossaryResponse.data?.entries || []) as any[]);
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Could not load the schema contract.', 'error');
      } finally {
        setLoading(false);
      }
    })();
  }, [user.role]);

  const positions = useMemo(() => {
    const map = new Map<string, { x: number; y: number }>();
    tables.forEach((table, index) => {
      const col = index % COLS;
      const row = Math.floor(index / COLS);
      map.set(table.table_name, { x: PAD + col * (NODE_W + GAP_X), y: PAD + row * (NODE_H + GAP_Y) });
    });
    return map;
  }, [tables]);

  const rows = Math.ceil(tables.length / COLS) || 1;
  const canvasWidth = PAD * 2 + COLS * NODE_W + (COLS - 1) * GAP_X;
  const canvasHeight = PAD * 2 + rows * NODE_H + (rows - 1) * GAP_Y;

  const edges = relationships.map((relation: any, index: number) => {
    const from = positions.get(relation.from_table);
    const to = positions.get(relation.to_table);
    if (!from || !to) return null;
    const x1 = from.x + NODE_W / 2; const y1 = from.y + NODE_H / 2;
    const x2 = to.x + NODE_W / 2; const y2 = to.y + NODE_H / 2;
    const midX = (x1 + x2) / 2; const midY = (y1 + y2) / 2 - 24;
    return { key: `${relation.from_table}-${relation.to_table}-${index}`, path: `M ${x1} ${y1} Q ${midX} ${midY} ${x2} ${y2}`, relation };
  }).filter(Boolean) as { key: string; path: string; relation: any }[];

  return (
    <div className="workflow-page page-enter">
      <section className="workflow-hero glow-panel">
        <div className="hero-grid" />
        <div className="workflow-hero-inner">
          <div className="eyebrow compact"><i /> CONTROLLED SCHEMA CONTRACT</div>
          <h2>Every table, column, and <em>relationship.</em></h2>
          <p>This graph is generated directly from the approved SQLAlchemy schema contract the LLM is allowed to reason over — nothing here is guessed.</p>
          <div className="mode-row">
            <span>{tables.length} approved tables · {relationships.length} relationships</span>
            <button className={`mode-button ${showGlossary ? 'selected' : ''}`} onClick={() => setShowGlossary((value) => !value)}><BookOpen size={15} /> Business glossary</button>
          </div>
        </div>
      </section>

      {showGlossary && (
        <section className="surface-card">
          <SectionHeading title="Business term → schema target" eyebrow={`${glossary.length} MAPPED TERMS`} />
          {glossary.length ? (
            <div className="chunk-grid">
              {glossary.map((entry: any, index: number) => (
                <article className="chunk chunk-card" key={entry.term || index}>
                  <b>{entry.term || entry.business_term}</b>
                  <p>→ {entry.target_name || entry.target} <small className="mono">({entry.target_type || 'schema target'})</small></p>
                </article>
              ))}
            </div>
          ) : <Empty icon={<BookOpen size={20} />} title="No glossary entries" text="No business glossary mappings are configured yet." />}
        </section>
      )}

      <section className="surface-card erd-panel">
        <SectionHeading title="Entity relationship graph" eyebrow="CLICK A TABLE FOR DETAIL" action={<button className="text-button" onClick={() => window.location.reload()}><RefreshCw size={15} /> Reload</button>} />
        {loading ? <TableSkeleton /> : tables.length ? (
          <div className="erd-canvas-scroll">
            <div className="erd-canvas" style={{ width: canvasWidth, height: canvasHeight }}>
              <svg width={canvasWidth} height={canvasHeight} className="erd-svg">
                <defs>
                  <marker id="erd-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#4da3ff" /></marker>
                </defs>
                {edges.map((edge) => <path key={edge.key} d={edge.path} className="erd-edge" markerEnd="url(#erd-arrow)" />)}
              </svg>
              {tables.map((table: any) => {
                const position = positions.get(table.table_name);
                if (!position) return null;
                const previewColumns = (table.columns || []).slice(0, 4);
                return (
                  <button
                    key={table.table_name}
                    className={`erd-node ${table.category === 'business' ? 'business' : 'operational'}`}
                    style={{ left: position.x, top: position.y, width: NODE_W, height: NODE_H }}
                    onClick={() => setSelected(table)}
                  >
                    <div className="erd-node-head"><Database size={14} /><b>{table.table_name}</b></div>
                    <ul>
                      {previewColumns.map((column: any) => (
                        <li key={column.name}>{column.primary_key ? <KeyRound size={10} /> : <span className="erd-dot" />} {column.name}</li>
                      ))}
                      {table.columns?.length > previewColumns.length && <li className="erd-more">+{table.columns.length - previewColumns.length} more</li>}
                    </ul>
                  </button>
                );
              })}
            </div>
          </div>
        ) : <Empty icon={<Boxes size={24} />} title="Schema contract unavailable" text="Start the backend, then reload this workspace." />}
      </section>

      {selected && (
        <div className="drawer-backdrop">
          <aside className="side-drawer">
            <button className="drawer-close" onClick={() => setSelected(null)}><X size={19} /></button>
            <div className="eyebrow compact"><i /> TABLE DETAIL</div>
            <h2>{selected.table_name}</h2>
            <StatusBadge status={selected.category} />
            <div className="detail-list" style={{ marginTop: 16 }}>
              {(selected.columns || []).map((column: any) => (
                <div key={column.name}>
                  <span>{column.name}{column.primary_key ? ' (PK)' : ''}{column.unique ? ' · unique' : ''}</span>
                  <b className="mono">{column.type}{column.nullable ? ', nullable' : ', required'}</b>
                </div>
              ))}
            </div>
            {(selected.relationships || []).length > 0 && (
              <div className="evidence-block">
                <h4><Link2 size={13} /> Relationships</h4>
                {selected.relationships.map((relation: any, index: number) => (
                  <div className="evidence-source" key={index}><ShieldCheck size={14} /><div><b>{relation.from_column} → {relation.to_table}.{relation.to_column}</b><small>{relation.relationship_type} · delete rule {stringValue(relation.delete_rule, 'none')}</small></div></div>
                ))}
              </div>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
