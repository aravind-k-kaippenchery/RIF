import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  Bot,
  Boxes,
  BrainCircuit,
  Code2,
  Database,
  Network,
  RefreshCw,
} from 'lucide-react';

import type { UserRole } from '../lib/types';
import '../live-system-health.css';

type ServiceKey = 'fastapi' | 'postgres' | 'ollama' | 'chromadb' | 'langgraph' | 'mcp';

type ServiceHealth = {
  key: ServiceKey;
  name: string;
  detail: string;
  available: boolean;
  state: 'connected' | 'ready' | 'checking' | 'unknown' | 'attention' | 'unavailable' | string;
  message: string;
  latency_ms: number;
  kind: 'connection' | 'runtime' | string;
  metadata?: Record<string, unknown>;
};

type LiveHealthPayload = {
  live: boolean;
  checked_at: string;
  poll_after_seconds: number;
  overall_ready: boolean;
  services: ServiceHealth[];
};

type HealthEnvelope = {
  data?: LiveHealthPayload;
  error?: { message?: string } | null;
  answer?: string | null;
};

const API_BASE = String(
  import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000',
).replace(/\/$/, '');

const POLL_INTERVAL_MS = 5_000;
const REQUEST_TIMEOUT_MS = 3_000;
const STALE_AFTER_MS = 12_000;

const FALLBACK: ServiceHealth[] = [
  ['fastapi', 'FastAPI', 'Backend request surface', 'connection'],
  ['postgres', 'PostgreSQL', 'Structured business data', 'connection'],
  ['ollama', 'Ollama', 'Local model runtime', 'connection'],
  ['chromadb', 'ChromaDB', 'Local document knowledge', 'connection'],
  ['langgraph', 'LangGraph', 'Route orchestration', 'runtime'],
  ['mcp', 'MCP', 'Controlled tool boundary', 'runtime'],
].map(([key, name, detail, kind]) => ({
  key: key as ServiceKey,
  name,
  detail,
  available: false,
  state: 'checking',
  message: 'Waiting for the first live heartbeat.',
  latency_ms: 0,
  kind,
}));

const ICONS = {
  fastapi: Code2,
  postgres: Database,
  ollama: Bot,
  chromadb: BrainCircuit,
  langgraph: Network,
  mcp: Boxes,
};

function labelFor(service: ServiceHealth) {
  if (service.state === 'checking') return 'CHECKING';
  if (service.key === 'fastapi' && !service.available) return 'DISCONNECTED';
  if (service.state === 'unknown') return 'UNKNOWN';
  if (!service.available) return service.state === 'attention' ? 'ATTENTION' : 'NOT AVAILABLE';
  return service.kind === 'runtime' ? 'READY' : 'CONNECTED';
}

function toneFor(service: ServiceHealth) {
  if (service.state === 'checking' || service.state === 'unknown') return 'neutral';
  if (service.available) return 'success';
  if (service.state === 'attention') return 'warning';
  return 'danger';
}

function timeLabel(value: string | null) {
  if (!value) return 'Not checked yet';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}

function backendUnavailableServices(message: string): ServiceHealth[] {
  return FALLBACK.map((service) => {
    if (service.key === 'fastapi') {
      return {
        ...service,
        available: false,
        state: 'unavailable',
        message,
      };
    }
    return {
      ...service,
      available: false,
      state: 'unknown',
      message: 'Cannot verify this component because the FastAPI heartbeat is unavailable.',
    };
  });
}

export default function LiveSystemHealth({ role }: { role: UserRole }) {
  const [services, setServices] = useState<ServiceHealth[]>(FALLBACK);
  const [overallReady, setOverallReady] = useState(false);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const activeController = useRef<AbortController | null>(null);
  const lastCompletedProbeAt = useRef<number>(0);

  const markBackendUnavailable = useCallback((message: string) => {
    setServices(backendUnavailableServices(message));
    setOverallReady(false);
    setCheckedAt(new Date().toISOString());
    setError(message);
    lastCompletedProbeAt.current = Date.now();
  }, []);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;

    inFlight.current = true;
    setRefreshing(true);

    // Never keep showing a green FastAPI card while a new heartbeat is unresolved.
    setServices((current) => current.map((service) => (
      service.key === 'fastapi'
        ? {
            ...service,
            available: false,
            state: 'checking',
            message: 'Checking the FastAPI heartbeat now.',
            latency_ms: 0,
          }
        : service
    )));
    setOverallReady(false);

    const controller = new AbortController();
    activeController.current?.abort();
    activeController.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const started = performance.now();

    try {
      const response = await fetch(
        `${API_BASE}/api/system/live-health?_heartbeat=${Date.now()}`,
        {
          method: 'GET',
          headers: {
            'X-User-Role': role,
            'Cache-Control': 'no-cache',
            Pragma: 'no-cache',
          },
          cache: 'no-store',
          signal: controller.signal,
        },
      );

      if (!response.ok) {
        throw new Error(`FastAPI heartbeat returned HTTP ${response.status}.`);
      }

      const envelope = await response.json() as HealthEnvelope | LiveHealthPayload;
      const payload = 'data' in envelope && envelope.data
        ? envelope.data
        : envelope as LiveHealthPayload;

      if (!payload || payload.live !== true || !Array.isArray(payload.services)) {
        throw new Error('FastAPI returned an invalid live-health payload.');
      }

      const elapsed = Math.max(0, Math.round(performance.now() - started));
      const normalized = payload.services.map((service) => (
        service.key === 'fastapi'
          ? { ...service, available: true, state: 'connected', latency_ms: elapsed }
          : service
      ));

      setServices(normalized);
      setOverallReady(Boolean(payload.overall_ready));
      setCheckedAt(payload.checked_at || new Date().toISOString());
      setError(null);
      lastCompletedProbeAt.current = Date.now();
    } catch (cause) {
      const timedOut = cause instanceof DOMException && cause.name === 'AbortError';
      const message = timedOut
        ? `FastAPI did not answer within ${REQUEST_TIMEOUT_MS / 1000} seconds.`
        : cause instanceof Error
          ? cause.message
          : 'The FastAPI heartbeat could not be reached.';
      markBackendUnavailable(message);
    } finally {
      window.clearTimeout(timeout);
      if (activeController.current === controller) activeController.current = null;
      inFlight.current = false;
      setRefreshing(false);
    }
  }, [markBackendUnavailable, role]);

  useEffect(() => {
    void refresh();

    const pollTimer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    const staleTimer = window.setInterval(() => {
      const lastProbe = lastCompletedProbeAt.current;
      if (lastProbe > 0 && Date.now() - lastProbe > STALE_AFTER_MS) {
        activeController.current?.abort();
        markBackendUnavailable('The last FastAPI heartbeat is stale.');
      }
    }, 1_000);

    const onVisibility = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    const onOnline = () => void refresh();
    const onOffline = () => markBackendUnavailable('The browser is offline.');

    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);

    return () => {
      activeController.current?.abort();
      window.clearInterval(pollTimer);
      window.clearInterval(staleTimer);
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
    };
  }, [markBackendUnavailable, refresh]);

  return (
    <section className="live-health-section">
      <div className="section-heading live-health-heading">
        <div>
          <div className="live-health-title-row">
            <h3>Local system health</h3>
            <span className={`live-indicator ${overallReady ? 'ready' : 'attention'}`}>
              <i /> {overallReady ? 'ALL READY' : 'ATTENTION'}
            </span>
          </div>
          <small className="live-health-caption">
            <Activity size={12} /> Browser heartbeat every 5 seconds · 3-second timeout · last checked {timeLabel(checkedAt)}
          </small>
        </div>
        <button className="text-button" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw size={15} className={refreshing ? 'spin' : ''} />
          {refreshing ? 'Checking…' : 'Refresh now'}
        </button>
      </div>

      {error && <div className="live-health-error">{error}</div>}

      <div className="service-grid">
        {services.map((service) => {
          const Icon = ICONS[service.key] || Activity;
          const label = labelFor(service);
          const tone = toneFor(service);
          return (
            <article className={`service-card live-service-card ${tone}`} key={service.key} title={service.message}>
              <span className="service-icon"><Icon size={19} /></span>
              <div className="service-state"><span className={`status-badge ${tone}`}>{label}</span></div>
              <h3>{service.name}</h3>
              <p>{service.detail}</p>
              <small><span className={`status-dot ${tone}`} /> {label}</small>
              <span className="live-latency">{service.latency_ms} ms</span>
            </article>
          );
        })}
      </div>
    </section>
  );
}
