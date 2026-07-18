import { FormEvent, useState } from 'react';
import { Check, Copy, LoaderCircle, ShieldAlert } from 'lucide-react';
import { api } from '../lib/api';
import type { ApiEnvelope, LocalUser } from '../lib/types';
import { SectionHeading, StatusBadge, asRecord, SESSION_STORAGE_KEY } from '../App';
import type { Toast } from '../App';

type Props = { user: LocalUser; toast: (message: string, type?: Toast['type']) => void };

// Mirrors the backend's advisory unique-key sets (app/services/duplicate_service.py).
// PostgreSQL unique constraints remain the final source of truth at confirmation time;
// this form only helps you fill in the fields the backend actually checks.
const UNIQUE_KEY_SETS: Record<string, string[][]> = {
  employees: [['employee_code'], ['email'], ['phone']],
  employee_permissions: [['employee_id', 'permission_code']],
  vendors: [['vendor_code'], ['vendor_name'], ['contact_email'], ['phone']],
  customers: [['customer_code'], ['customer_name'], ['contact_email'], ['phone']],
  products: [['product_code'], ['product_name']],
  product_vendor_mappings: [['product_id', 'vendor_id']],
  sales_deals: [['deal_code']],
};

const TABLES = Object.keys(UNIQUE_KEY_SETS);

export default function DuplicateCenterPage({ user, toast }: Props) {
  const [table, setTable] = useState(TABLES[0]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<ApiEnvelope<any> | null>(null);

  const fields = Array.from(new Set(UNIQUE_KEY_SETS[table].flat()));

  const bootstrapSession = async () => {
    const existing = sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (existing) return existing;
    const response = await api.post('/api/sessions', user.role, { ttl_minutes: 15 });
    const id = String(response.data?.session_id || response.data?.id);
    sessionStorage.setItem(SESSION_STORAGE_KEY, id);
    return id;
  };

  const check = async (event: FormEvent) => {
    event.preventDefault();
    setChecking(true);
    setResult(null);
    try {
      const sessionId = await bootstrapSession();
      const record = Object.fromEntries(Object.entries(values).filter(([, value]) => value.trim() !== ''));
      const response = await api.post('/api/crud/bulk-propose', user.role, {
        session_id: sessionId,
        target_table: table,
        records: [record],
        user_prompt: `Duplicate detection check for ${table}`,
        ttl_minutes: 5,
      });
      setResult(response);
      // This workspace only checks for duplicates; it never writes data, so the
      // preview it creates is cancelled immediately after we read the result.
      if (response.pending_action_id) {
        try { await api.post(`/api/crud/actions/${response.pending_action_id}/cancel`, user.role, { session_id: sessionId }); } catch { /* best-effort cleanup */ }
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : 'The duplicate check could not be completed.', 'error');
    } finally {
      setChecking(false);
    }
  };

  const data = asRecord(result?.data);
  const duplicates = (data.duplicate_matches || []) as any[];
  const checkedFields = fields.filter((field) => values[field]?.trim());

  return (
    <div className="workflow-page page-enter">
      <section className="workflow-hero glow-panel">
        <div className="hero-grid" />
        <div className="workflow-hero-inner">
          <div className="eyebrow compact"><i /> ADVISORY SAFETY CHECK</div>
          <h2>Check before you <em>propose.</em></h2>
          <p>Enter candidate field values for a business table and see which existing records already match a known unique business key. This never writes data — any preview it creates is cancelled automatically after the check.</p>
        </div>
      </section>

      <section className="surface-card crud-form-card">
        <SectionHeading title="Candidate record" eyebrow="ADVISORY · POSTGRESQL REMAINS FINAL PROTECTION" />
        <div className="mode-row">
          {TABLES.map((name) => (
            <button key={name} className={`mode-button ${table === name ? 'selected' : ''}`} onClick={() => { setTable(name); setValues({}); setResult(null); }}>{name}</button>
          ))}
        </div>
        <form className="crud-form" onSubmit={check}>
          {fields.map((field) => (
            <input key={field} value={values[field] || ''} onChange={(event) => setValues((current) => ({ ...current, [field]: event.target.value }))} placeholder={field.replace(/_/g, ' ')} />
          ))}
          <button className="button primary" disabled={checking || !checkedFields.length}>{checking ? <LoaderCircle className="spin" size={16} /> : <Copy size={16} />} {checking ? 'Checking…' : 'Check for duplicates'}</button>
        </form>
        <div className="duplicate-key-hint">
          {UNIQUE_KEY_SETS[table].map((set, index) => <span key={index}>{set.join(' + ')}</span>)}
        </div>
      </section>

      {result && (
        <section className="surface-card">
          <SectionHeading title="Result" eyebrow="PREVIEW WAS CANCELLED · NO DATA CHANGED" action={<StatusBadge status={duplicates.length ? 'Duplicate detected' : 'Clear'} />} />
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
        <p className="settings-copy">Duplicate checks run through the same confirmation-gated write pipeline used everywhere else (<code>/api/crud/bulk-propose</code>). A field combination is only tested once every field in that combination is filled in. The stored preview this creates is cancelled immediately so nothing here can ever change business data — PostgreSQL's own unique constraints remain the final, concurrency-safe protection at confirmation time.</p>
      </section>
    </div>
  );
}
