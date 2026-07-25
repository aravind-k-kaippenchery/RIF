import { FormEvent, useEffect, useState } from 'react';
import {
  ArrowRight, Bot, Boxes, Database, FileSearch, LoaderCircle, Network, Send, ShieldCheck, Sparkles, UserRound,
} from 'lucide-react';
import { api } from '../lib/api';
import type { ApiEnvelope, LocalUser } from '../lib/types';
import { DataPreview, Empty, RouteBadge, SectionHeading, StatusBadge, asRecord, SESSION_STORAGE_KEY } from '../App';
import type { Toast } from '../App';

type Props = { user: LocalUser; toast: (message: string, type?: Toast['type']) => void };

const STAGES = [
  { key: 'query', label: 'Query', icon: UserRound },
  { key: 'router', label: 'Router', icon: Network },
  { key: 'mcp', label: 'MCP boundary', icon: Boxes },
  { key: 'database', label: 'Database', icon: Database },
  { key: 'rag', label: 'Document RAG', icon: FileSearch },
  { key: 'fusion', label: 'Evidence fusion', icon: Sparkles },
  { key: 'answer', label: 'Answer', icon: Bot },
];

function activeStages(route?: string | null): string[] {
  const value = (route || '').toLowerCase();
  const base = ['query', 'router', 'mcp'];
  if (value.includes('hybrid')) return [...base, 'database', 'rag', 'fusion', 'answer'];
  if (value.includes('document') || value.includes('rag')) return [...base, 'rag', 'answer'];
  if (value.includes('structured') || value.includes('database') || value.includes('crud')) return [...base, 'database', 'answer'];
  return [...base, 'answer'];
}

export default function PipelineConsolePage({ user, toast }: Props) {
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<ApiEnvelope<any> | null>(null);
  const [hybridStatus, setHybridStatus] = useState<Record<string, any>>({});
  const [agentStatus, setAgentStatus] = useState<Record<string, any>>({});

  useEffect(() => {
    void (async () => {
      try {
        const [hybrid, agent] = await Promise.all([api.get('/api/hybrid/status', user.role), api.get('/api/agent/status', user.role)]);
        setHybridStatus(asRecord(hybrid.data));
        setAgentStatus(asRecord(agent.data));
      } catch { /* status is informational only */ }
    })();
  }, [user.role]);

  const ask = async (event: FormEvent) => {
    event.preventDefault();
    if (!question.trim() || loading) return;
    setLoading(true);
    try {
      const sessionId = sessionStorage.getItem(SESSION_STORAGE_KEY);
      const result = await api.post('/api/query', user.role, { question: question.trim(), session_id: sessionId, top_k: 4 });
      const nextSession = asRecord(result.data).session?.session_id || result.session_id;
      if (nextSession) sessionStorage.setItem(SESSION_STORAGE_KEY, String(nextSession));
      setResponse(result);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'The routed query failed.', 'error');
    } finally {
      setLoading(false);
    }
  };

  const data = asRecord(response?.data);
  const rows = Array.isArray(data.rows) ? data.rows : Array.isArray(data.database_evidence) ? data.database_evidence : [];
  const chunks = Array.isArray(data.retrieved_chunks) ? data.retrieved_chunks : Array.isArray(data.matches) ? data.matches : [];
  const stages = activeStages(response?.route);

  return (
    <div className="workflow-page page-enter">
      <section className="workflow-hero glow-panel">
        <div className="hero-grid" />
        <div className="workflow-hero-inner">
          <div className="eyebrow compact"><i /> ROUTING &amp; HYBRID EVIDENCE</div>
          <h2>Watch the request <em>find its path.</em></h2>
          <p>Ask a question and see exactly which route the LangGraph agent selected, then inspect verified database and document evidence side by side.</p>
          {hybridStatus.notes && <ul className="pipeline-notes">{(hybridStatus.notes as string[]).map((note) => <li key={note}>{note}</li>)}</ul>}
          {agentStatus.supported_routes && <div className="mode-row"><span>Supported routes: {(agentStatus.supported_routes as string[]).join(', ')}</span></div>}
        </div>
      </section>

      <form className="knowledge-search surface-card" onSubmit={ask}>
        <Send size={18} />
        <input value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask a database, document, or combined evidence question…" />
        <button className="button primary" disabled={!question.trim() || loading}>{loading ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />} Route</button>
      </form>

      <section className="surface-card pipeline-panel">
        <SectionHeading title="Execution path" eyebrow={response ? `ROUTE: ${(response.route || 'system').toUpperCase()}` : 'AWAITING A QUESTION'} />
        <div className="pipeline-flow">
          {STAGES.map((stage, index) => {
            const active = stages.includes(stage.key);
            const Icon = stage.icon;
            return (
              <div className="pipeline-node-wrap" key={stage.key}>
                <div className={`pipeline-node ${active ? 'active' : ''}`}><Icon size={18} /><span>{stage.label}</span></div>
                {index < STAGES.length - 1 && <i className={`pipeline-link ${active && stages.includes(STAGES[index + 1].key) ? 'active' : ''}`} />}
              </div>
            );
          })}
        </div>
      </section>

      {!response ? (
        <Empty icon={<Network size={24} />} title="No routed query yet" text="Ask a question above to see the live route and its supporting evidence." />
      ) : (
        <section className="hybrid-evidence-grid">
          <article className="surface-card evidence-column">
            <SectionHeading title="Database evidence" eyebrow="STRUCTURED · MCP READ-ONLY" />
            {rows.length ? <DataPreview rows={rows.slice(0, 6)} /> : <Empty icon={<Database size={20} />} title="No database evidence" text="This route did not use structured database evidence." />}
          </article>
          <article className="surface-card evidence-column">
            <SectionHeading title="Document evidence" eyebrow="LOCAL CHROMADB RETRIEVAL" />
            {chunks.length ? chunks.slice(0, 6).map((chunk: any, index: number) => (
              <div className="chunk" key={chunk.reference || index}><b>{chunk.reference || chunk.filename || `Chunk ${index + 1}`}</b><p>{chunk.text_preview || chunk.text || chunk.content}</p></div>
            )) : <Empty icon={<FileSearch size={20} />} title="No document evidence" text="This route did not use retrieved document evidence." />}
          </article>
          <article className="surface-card evidence-column answer-column">
            <SectionHeading title="Final answer" eyebrow="FUSED & GROUNDED" action={<StatusBadge status={response.status} />} />
            <p className="answer-text">{response.answer}</p>
            <div className="evidence-section"><span>Route</span><RouteBadge route={response.route} /></div>
            {response.sources?.length ? <div className="source-chip-row">{response.sources.map((source, index) => <span className="source-chip" key={`${source.reference}-${index}`}><ShieldCheck size={12} /> {source.reference}</span>)}</div> : null}
            {response.generated_sql && <div className="evidence-block"><h4>Validated query</h4><pre>{response.generated_sql}</pre></div>}
          </article>
        </section>
      )}
    </div>
  );
}
