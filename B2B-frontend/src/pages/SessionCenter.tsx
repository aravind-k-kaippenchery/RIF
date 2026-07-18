import { FormEvent, useState } from 'react';
import { AlertTriangle, Clock, History, Plus, RefreshCw, ShieldCheck, Trash2, X } from 'lucide-react';
import { api } from '../lib/api';
import type { ApiEnvelope, LocalUser } from '../lib/types';
import {
  Empty, SectionHeading, StatusBadge, TableSkeleton, RecordTable, asRecord, stringValue, formatTime,
  SESSION_STORAGE_KEY,
} from '../App';
import type { Toast } from '../App';

type Props = { user: LocalUser; toast: (message: string, type?: Toast['type']) => void };

export default function SessionCenterPage({ user, toast }: Props) {
  const [sessionId, setSessionId] = useState<string | null>(() => sessionStorage.getItem(SESSION_STORAGE_KEY));
  const [session, setSession] = useState<ApiEnvelope<any> | null>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [pendingActions, setPendingActions] = useState<any[]>([]);
  const [tab, setTab] = useState<'history' | 'pending'>('history');
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [ttl, setTtl] = useState(120);
  const [manualId, setManualId] = useState('');
  const [selectedAction, setSelectedAction] = useState<any | null>(null);

  const loadSession = async (id: string) => {
    setLoading(true);
    try {
      const [detail, historyResponse, pendingResponse] = await Promise.all([
        api.get(`/api/sessions/${id}`, user.role),
        api.get(`/api/sessions/${id}/history?limit=30`, user.role),
        api.get(`/api/sessions/${id}/pending-actions`, user.role),
      ]);
      setSession(detail);
      setHistory((historyResponse.data?.entries || historyResponse.data?.history || historyResponse.data?.messages || []) as any[]);
      setPendingActions((pendingResponse.data?.pending_actions || []) as any[]);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not load this session.', 'error');
    } finally {
      setLoading(false);
    }
  };

  const createSession = async (event?: FormEvent) => {
    event?.preventDefault();
    setCreating(true);
    try {
      const response = await api.post('/api/sessions', user.role, { ttl_minutes: ttl });
      const id = String(response.data?.session_id || response.data?.id);
      sessionStorage.setItem(SESSION_STORAGE_KEY, id);
      setSessionId(id);
      toast('Short-term session created.', 'success');
      await loadSession(id);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not create a session.', 'error');
    } finally {
      setCreating(false);
    }
  };

  const openManual = async (event: FormEvent) => {
    event.preventDefault();
    if (!manualId.trim()) return;
    sessionStorage.setItem(SESSION_STORAGE_KEY, manualId.trim());
    setSessionId(manualId.trim());
    await loadSession(manualId.trim());
  };

  const expireAction = async (actionId: string) => {
    if (!sessionId) return;
    try {
      await api.post(`/api/sessions/${sessionId}/pending-actions/${actionId}/expire`, user.role);
      toast('Pending action expired.', 'info');
      await loadSession(sessionId);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not expire this pending action.', 'error');
    }
  };

  const closeSession = async () => {
    if (!sessionId) return;
    try {
      const response = await api.del(`/api/sessions/${sessionId}`, user.role);
      toast(response.answer || 'Session closed.', 'success');
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
      setSessionId(null);
      setSession(null);
      setHistory([]);
      setPendingActions([]);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not close this session.', 'error');
    }
  };

  const sessionData = asRecord(session?.data);

  return (
    <div className="workflow-page page-enter">
      <section className="workflow-hero glow-panel">
        <div className="hero-grid" />
        <div className="workflow-hero-inner">
          <div className="eyebrow compact"><i /> SHORT-TERM SESSION MEMORY</div>
          <h2>Inspect one workspace <em>session</em> end to end.</h2>
          <p>Create a short-term session, or open one already in use, to review its bounded conversation history and any pending write actions attached to it.</p>
          <div className="workflow-toolbar">
            <form className="inline-form" onSubmit={createSession}>
              <label>TTL minutes<input type="number" min={5} max={1440} value={ttl} onChange={(event) => setTtl(Number(event.target.value) || 120)} /></label>
              <button className="button primary" disabled={creating}><Plus size={16} /> {creating ? 'Creating…' : 'Create session'}</button>
            </form>
            <form className="inline-form" onSubmit={openManual}>
              <label>Session ID<input value={manualId} onChange={(event) => setManualId(event.target.value)} placeholder="Paste a session UUID" /></label>
              <button className="button secondary" type="submit">Open</button>
            </form>
          </div>
        </div>
      </section>

      {!sessionId ? (
        <Empty icon={<History size={24} />} title="No session selected" text="Create a new session, or open an existing session ID, to inspect its bounded history and pending actions." />
      ) : (
        <>
          <section className="surface-card session-summary">
            <SectionHeading title="Active session" eyebrow={sessionId} action={<div className="drawer-actions"><button className="text-button" onClick={() => void loadSession(sessionId)}><RefreshCw size={15} /> Refresh</button><button className="button danger small" onClick={() => void closeSession()}><Trash2 size={15} /> Close session</button></div>} />
            {loading && !session ? <TableSkeleton /> : (
              <div className="detail-list">
                {Object.entries(sessionData).filter(([key]) => !['history', 'pending_actions'].includes(key)).slice(0, 8).map(([key, value]) => (
                  <div key={key}><span>{key.replace(/_/g, ' ')}</span><b className={key.includes('id') ? 'mono' : ''}>{stringValue(value)}</b></div>
                ))}
              </div>
            )}
          </section>

          <div className="tab-row">
            <button className={tab === 'history' ? 'selected' : ''} onClick={() => setTab('history')}><Clock size={14} /> Conversation history</button>
            <button className={tab === 'pending' ? 'selected' : ''} onClick={() => setTab('pending')}><ShieldCheck size={14} /> Pending actions ({pendingActions.length})</button>
          </div>

          {tab === 'history' ? (
            <section className="surface-card">
              <SectionHeading title="Bounded short-term history" eyebrow="MOST RECENT FIRST" />
              {loading ? <TableSkeleton /> : history.length ? (
                <div className="session-history-list">
                  {history.map((entry: any, index: number) => (
                    <div className="session-history-row" key={entry.id || index}>
                      <StatusBadge status={entry.role || entry.route || 'entry'} />
                      <div>
                        <b>{entry.question || entry.prompt || entry.message || 'Recorded turn'}</b>
                        <small>{entry.answer || entry.response || ''}</small>
                        <small className="mono">{formatTime(entry.created_at || entry.timestamp)}</small>
                      </div>
                    </div>
                  ))}
                </div>
              ) : <Empty icon={<Clock size={22} />} title="No history recorded yet" text="Ask a question through the Assistant using this session to populate bounded conversation memory." />}
            </section>
          ) : (
            <section className="surface-card">
              <SectionHeading title="Pending actions attached to this session" eyebrow="CONFIRM · CANCEL · EXPIRE" />
              {loading ? <TableSkeleton /> : pendingActions.length ? (
                <RecordTable rows={pendingActions} onRow={setSelectedAction} />
              ) : <Empty icon={<ShieldCheck size={22} />} title="No pending actions" text="Pending actions created from the CRUD Queue or Assistant for this session will appear here." />}
            </section>
          )}
        </>
      )}

      {selectedAction && (
        <div className="drawer-backdrop">
          <aside className="side-drawer">
            <button className="drawer-close" onClick={() => setSelectedAction(null)}><X size={19} /></button>
            <div className="eyebrow compact"><i /> PENDING ACTION DETAIL</div>
            <h2>{selectedAction.action_type || 'Action'}</h2>
            <StatusBadge status={selectedAction.status || 'pending'} />
            <div className="detail-list">
              {Object.entries(selectedAction).map(([key, value]) => (
                <div key={key}><span>{key.replace(/_/g, ' ')}</span><b>{stringValue(value)}</b></div>
              ))}
            </div>
            <div className="drawer-actions">
              <button className="button secondary" onClick={() => void expireAction(selectedAction.id || selectedAction.pending_action_id)}><AlertTriangle size={15} /> Expire safely</button>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
