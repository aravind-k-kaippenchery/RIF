import { FormEvent, useEffect, useMemo, useState } from 'react';
import { Check, Copy, LoaderCircle, ShieldAlert } from 'lucide-react';
import { api } from '../lib/api';
import type { ApiEnvelope, LocalUser } from '../lib/types';
import { SectionHeading, StatusBadge, asRecord } from '../App';
import type { Toast } from '../App';

type Props = { user: LocalUser; toast: (message: string, type?: Toast['type']) => void };

type TableCapability = {
  table_name: string;
  category: string;
  unique_constraints?: Array<{ name?: string | null; columns: string[] }>;
};

export default function DuplicateCenterPage({ user, toast }: Props) {
  const [tables, setTables] = useState<TableCapability[]>([]);
  const [table, setTable] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [loadingTables, setLoadingTables] = useState(true);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<ApiEnvelope<any> | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const response = await api.get('/api/tables', user.role);
        const reflected = ((response.data?.tables || []) as TableCapability[])
          .filter((entry) => entry.category === 'business' && (entry.unique_constraints || []).length > 0);
        setTables(reflected);
        setTable((current) => current || reflected[0]?.table_name || '');
      } catch (error) {
        toast(error instanceof Error ? error.message : 'Could not load reflected unique constraints.', 'error');
      } finally {
        setLoadingTables(false);
      }
    })();
  }, [user.role]);

  const uniqueKeySets = useMemo(
    () => tables.find((entry) => entry.table_name === table)?.unique_constraints?.map((item) => item.columns) || [],
    [table, tables],
  );
  const fields = useMemo(() => Array.from(new Set(uniqueKeySets.flat())), [uniqueKeySets]);
  const completeKeySets = uniqueKeySets.filter((keys) => keys.every((field) => values[field]?.trim()));

  const check = async (event: FormEvent) => {
    event.preventDefault();
    setChecking(true);
    setResult(null);
    try {
      const record = Object.fromEntries(Object.entries(values).filter(([, value]) => value.trim() !== ''));
      const response = await api.post('/api/crud/check-duplicates', user.role, {
        target_table: table,
        values: record,
      });
      setResult(response);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'The duplicate check could not be completed.', 'error');
    } finally {
      setChecking(false);
    }
  };

  const data = asRecord(result?.data);
  const duplicates = (data.duplicate_matches || []) as any[];

  return (
    <div className="workflow-page page-enter">
      <section className="workflow-hero glow-panel">
        <div className="hero-grid" />
        <div className="workflow-hero-inner">
          <div className="eyebrow compact"><i /> ADVISORY SAFETY CHECK</div>
          <h2>Check before you <em>propose.</em></h2>
          <p>Enter candidate field values for a business table and see which existing records match a live PostgreSQL unique constraint. This is a read-only check and never creates a pending write.</p>
        </div>
      </section>

      <section className="surface-card crud-form-card">
        <SectionHeading title="Candidate record" eyebrow="ADVISORY · POSTGRESQL REMAINS FINAL PROTECTION" />
        <div className="mode-row">
          {tables.map((entry) => (
            <button key={entry.table_name} className={`mode-button ${table === entry.table_name ? 'selected' : ''}`} onClick={() => { setTable(entry.table_name); setValues({}); setResult(null); }}>{entry.table_name}</button>
          ))}
        </div>
        {!loadingTables && !tables.length && <div className="inline-alert">No reflected business table currently has a unique constraint to check.</div>}
        <form className="crud-form" onSubmit={check}>
          {fields.map((field) => (
            <input key={field} value={values[field] || ''} onChange={(event) => setValues((current) => ({ ...current, [field]: event.target.value }))} placeholder={field.replace(/_/g, ' ')} />
          ))}
          <button className="button primary" disabled={checking || !completeKeySets.length}>{checking ? <LoaderCircle className="spin" size={16} /> : <Copy size={16} />} {checking ? 'Checking…' : 'Check for duplicates'}</button>
        </form>
        <div className="duplicate-key-hint">
          {uniqueKeySets.map((set, index) => <span key={index}>{set.join(' + ')}</span>)}
        </div>
      </section>

      {result && (
        <section className="surface-card">
          <SectionHeading title="Result" eyebrow="READ-ONLY CHECK · NO DATA CHANGED" action={<StatusBadge status={duplicates.length ? 'Duplicate detected' : 'Clear'} />} />
          {duplicates.length ? (
            <div className="duplicate-panel">
              <div className="duplicate-panel-head"><ShieldAlert size={18} /><b>{duplicates.length} matching unique key(s) found</b></div>
              <p>These existing records already use the same business key you entered.</p>
              {duplicates.map((match: any, index: number) => (
                <div className="duplicate-match" key={index}>
                  <span>Matched on <b>{(match.key_fields || []).join(', ')}</b></span>
                  <pre>{JSON.stringify(match.records, null, 2)}</pre>
                </div>
              ))}
            </div>
          ) : (
            <div className="inline-alert success"><Check size={16} /> No existing record matches the unique keys you entered. This candidate looks safe to propose.</div>
          )}
        </section>
      )}

      <section className="surface-card">
        <SectionHeading title="How this works" eyebrow="TRANSPARENCY" />
        <p className="settings-copy">The table list and key combinations come from live PostgreSQL reflection. The read-only <code>/api/crud/check-duplicates</code> endpoint tests a combination only after every field in that unique constraint is filled. PostgreSQL's constraint remains the final concurrency-safe protection when a confirmed write executes.</p>
      </section>
    </div>
  );
}
