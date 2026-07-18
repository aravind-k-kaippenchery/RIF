import { FormEvent, useEffect, useState } from 'react';
import {
  ArrowRight, Check, Code2, LoaderCircle, RefreshCw, ShieldAlert, ShieldCheck, Sparkles, WandSparkles, X,
} from 'lucide-react';
import { api } from '../lib/api';
import type { ApiEnvelope, LocalUser } from '../lib/types';
import {
  Empty, SectionHeading, StatusBadge, TableSkeleton, asRecord, SESSION_STORAGE_KEY,
} from '../App';
import type { Toast } from '../App';

type Props = { user: LocalUser; toast: (message: string, type?: Toast['type']) => void };

function ensureSession(_user: LocalUser): string | null {
  return sessionStorage.getItem(SESSION_STORAGE_KEY);
}

export default function CrudQueuePage({ user, toast }: Props) {
  const [sessionId, setSessionId] = useState<string | null>(() => ensureSession(user));
  const [mode, setMode] = useState<'prompt' | 'sql'>('prompt');
  const [prompt, setPrompt] = useState('');
  const [sql, setSql] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [proposal, setProposal] = useState<ApiEnvelope<any> | null>(null);
  const [queue, setQueue] = useState<any[]>([]);
  const [loadingQueue, setLoadingQueue] = useState(false);
  const [statusFilter, setStatusFilter] = useState<'all' | 'pending' | 'confirmed' | 'cancelled'>('all');
  const [busyId, setBusyId] = useState<string | null>(null);

  const bootstrapSession = async () => {
    if (sessionId) return sessionId;
    try {
      const response = await api.post('/api/sessions', user.role, { ttl_minutes: 60 });
      const id = String(response.data?.session_id || response.data?.id);
      sessionStorage.setItem(SESSION_STORAGE_KEY, id);
      setSessionId(id);
      return id;
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not open a session for this proposal.', 'error');
      return null;
    }
  };

  const loadQueue = async (id: string | null) => {
    if (!id) return;
    setLoadingQueue(true);
    try {
      const response = await api.get(`/api/sessions/${id}/pending-actions`, user.role);
      setQueue((response.data?.pending_actions || []) as any[]);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not load the confirmation queue.', 'error');
    } finally {
      setLoadingQueue(false);
    }
  };

  useEffect(() => { void loadQueue(sessionId); }, [sessionId, user.role]);

  const submitProposal = async (event: FormEvent) => {
    event.preventDefault();
    const id = await bootstrapSession();
    if (!id) return;
    if (mode === 'prompt' && !prompt.trim()) return;
    if (mode === 'sql' && !sql.trim()) return;
    setSubmitting(true);
    try {
      const response = mode === 'prompt'
        ? await api.post('/api/crud/propose-from-prompt', user.role, { session_id: id, question: prompt.trim(), ttl_minutes: 30 })
        : await api.post('/api/crud/propose', user.role, { session_id: id, sql: sql.trim(), user_prompt: prompt.trim() || undefined, ttl_minutes: 30 });
      setProposal(response);
      toast(response.answer || 'Write preview created.', 'success');
      await loadQueue(id);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'The write proposal could not be created.', 'error');
    } finally {
      setSubmitting(false);
    }
  };

  const mutate = async (pendingActionId: string, action: 'confirm' | 'cancel') => {
    if (!sessionId) return;
    setBusyId(pendingActionId);
    try {
      const response = await api.post(`/api/crud/actions/${pendingActionId}/${action}`, user.role, { session_id: sessionId });
      toast(response.answer || `Action ${action}ed.`, action === 'confirm' ? 'success' : 'info');
      await loadQueue(sessionId);
      if (proposal?.pending_action_id === pendingActionId) setProposal(null);
    } catch (e) {
      toast(e instanceof Error ? e.message : `The pending action could not be ${action}ed.`, 'error');
    } finally {
      setBusyId(null);
    }
  };

  const proposalData = asRecord(proposal?.data);
  const duplicates = (proposalData.duplicate_matches || []) as any[];
  const filteredQueue = queue.filter((item) => statusFilter === 'all' || String(item.status || '').toLowerCase() === statusFilter);

  return (
    <div className="workflow-page page-enter">
      <section className="workflow-hero glow-panel">
        <div className="hero-grid" />
        <div className="workflow-hero-inner">
          <div className="eyebrow compact"><i /> CONFIRMATION-GATED WRITES</div>
          <h2>Propose, review, then <em>confirm</em> the change.</h2>
          <p>Every insert, update, or delete is validated and stored as a preview first. No business-table row changes until you explicitly confirm the exact stored proposal.</p>
        </div>
      </section>

      <section className="surface-card crud-form-card">
        <SectionHeading title="Create a new write proposal" eyebrow={sessionId ? `SESSION ${sessionId.slice(0, 8)}…` : 'NO SESSION YET'} />
        <div className="mode-row">
          <button className={`mode-button ${mode === 'prompt' ? 'selected' : ''}`} onClick={() => setMode('prompt')}><WandSparkles size={15} /> From natural language</button>
          <button className={`mode-button ${mode === 'sql' ? 'selected' : ''}`} onClick={() => setMode('sql')}><Code2 size={15} /> From validated SQL</button>
        </div>
        <form className="crud-form" onSubmit={submitProposal}>
          {mode === 'prompt' ? (
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="e.g. Add a new vendor named Acme Textiles in Bangalore" rows={3} />
          ) : (
            <>
              <textarea value={sql} onChange={(event) => setSql(event.target.value)} placeholder="UPDATE employees SET department = 'Sales' WHERE employee_code = 'EMP0001'" rows={3} className="mono-area" />
              <input value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Optional note describing why this change is needed" />
            </>
          )}
          <button className="button primary" disabled={submitting}>{submitting ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />} {submitting ? 'Validating…' : 'Create preview'}</button>
        </form>
      </section>

      {proposal && (
        <section className="surface-card">
          <SectionHeading title="Stored preview" eyebrow="NO ROW HAS BEEN CHANGED YET" action={<StatusBadge status={proposal.status} />} />
          <p className="settings-copy">{proposal.answer}</p>
          {proposal.generated_sql && <div className="evidence-block"><h4>Validated SQL</h4><pre>{proposal.generated_sql}</pre></div>}
          {proposalData.preview && <div className="evidence-block"><h4>Preview data</h4><pre>{JSON.stringify(proposalData.preview, null, 2)}</pre></div>}
          {duplicates.length > 0 && (
            <div className="duplicate-panel">
              <div className="duplicate-panel-head"><ShieldAlert size={18} /><b>Possible duplicates detected</b></div>
              <p>These existing records share a unique business key with your proposal. Review before confirming.</p>
              {duplicates.map((match: any, index: number) => (
                <div className="duplicate-match" key={index}>
                  <span>Matched on <b>{(match.key_fields || []).join(', ')}</b></span>
                  <pre>{JSON.stringify(match.records, null, 2)}</pre>
                </div>
              ))}
            </div>
          )}
          {proposal.pending_action_id && (
            <div className="pending-actions">
              <button className="button secondary" onClick={() => void mutate(proposal.pending_action_id as string, 'cancel')} disabled={busyId === proposal.pending_action_id}>Cancel</button>
              <button className="button warning" onClick={() => void mutate(proposal.pending_action_id as string, 'confirm')} disabled={busyId === proposal.pending_action_id}>{busyId === proposal.pending_action_id ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />} Confirm and execute</button>
            </div>
          )}
        </section>
      )}

      <section className="surface-card">
        <SectionHeading title="Confirmation queue for this session" eyebrow="PENDING · CONFIRMED · CANCELLED" action={<button className="text-button" onClick={() => void loadQueue(sessionId)}><RefreshCw size={15} /> Refresh</button>} />
        <div className="tab-row">
          {(['all', 'pending', 'confirmed', 'cancelled'] as const).map((value) => (
            <button key={value} className={statusFilter === value ? 'selected' : ''} onClick={() => setStatusFilter(value)}>{value[0].toUpperCase() + value.slice(1)}</button>
          ))}
        </div>
        {loadingQueue ? <TableSkeleton /> : filteredQueue.length ? (
          <div className="queue-list">
            {filteredQueue.map((item: any) => {
              const id = item.id || item.pending_action_id;
              const isPending = String(item.status || '').toLowerCase().includes('pending');
              return (
                <div className="queue-row" key={id}>
                  <div>
                    <StatusBadge status={item.status || 'pending'} />
                    <b>{item.action_type || 'write'} · {item.target_table || 'business data'}</b>
                    <small className="mono">{id}</small>
                  </div>
                  {isPending && (
                    <div className="pending-actions">
                      <button className="button secondary small" onClick={() => void mutate(id, 'cancel')} disabled={busyId === id}><X size={14} /> Cancel</button>
                      <button className="button warning small" onClick={() => void mutate(id, 'confirm')} disabled={busyId === id}><Check size={14} /> Confirm</button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : <Empty icon={<Sparkles size={22} />} title="No queued actions" text="Create a write proposal above, or from the Assistant, to see it staged here for confirmation." />}
      </section>
    </div>
  );
}
