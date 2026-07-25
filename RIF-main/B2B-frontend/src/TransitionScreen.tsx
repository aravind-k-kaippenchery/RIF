import React, { useEffect, useMemo, useState } from 'react';
import { Sparkles, Cpu, Database, Network, ShieldCheck, Activity } from 'lucide-react';
import { PlexusNeural3DBackground } from './animation/PlexusNeural3DBackground';
import { NeuralNetworkBackground } from './animation/NeuralNetworkBackground';
import { wait } from './lib/wait';
import './transition-screen.css';


export type TransitionScreenProps = {
  username: string;
  onComplete: () => void;
  /** allow deterministic duration ranges for testing */
  durationMs?: number;
};

const BASE_STATUS = [
  { label: 'Identity Verified', micro: 'Verified identity chain' },
  { label: 'Session Established', micro: 'Secure session handshake' },
  { label: 'Loading Enterprise Knowledge Base', micro: 'Vector context ready' },
  { label: 'Connecting AI Agents', micro: 'LangGraph route activation' },
  { label: 'Initializing LangGraph Routes', micro: 'Directed workflow paths' },
  { label: 'Connecting ChromaDB', micro: 'Vector nodes in place' },
  { label: 'Syncing PostgreSQL Data', micro: 'Enterprise data channels' },
  { label: 'Activating MCP Tools', micro: 'Tool boundary confirmed' },
  { label: 'Workspace Ready', micro: 'Intelligence core online' },
] as const;

const SYSTEM_METRICS = [
  { label: 'Agents Online', value: '12' },
  { label: 'Knowledge Sources', value: 'Connected' },
  { label: 'Database Status', value: 'Active' },
  { label: 'Session Security', value: 'Verified' },
  { label: 'Vector Search', value: 'Ready' },
] as const;


function MicroActivation() {
  return (
    <div className="ts-micro-activations" aria-hidden="true">
      <div className="ts-micro ts-micro--chromadb">
        <i className="ts-dot" />
        <span>ChromaDB vector nodes</span>
      </div>
      <div className="ts-micro ts-micro--langgraph">
        <i className="ts-dot" />
        <span>LangGraph route paths</span>
      </div>
      <div className="ts-micro ts-micro--mcp">
        <i className="ts-dot" />
        <span>MCP tool endpoints</span>
      </div>
      <div className="ts-micro ts-micro--indexing">
        <i className="ts-dot" />
        <span>Document indexing particles</span>
      </div>
      <div className="ts-micro ts-micro--dbsync">
        <i className="ts-dot" />
        <span>Database synchronization</span>
      </div>
    </div>
  );
}

export function TransitionScreen({ username, onComplete, durationMs }: TransitionScreenProps) {
  const [stepIndex, setStepIndex] = useState(-1);
  const [phase, setPhase] = useState<'enter' | 'running' | 'exit'>('enter');
  const [poweredUp, setPoweredUp] = useState(false);


  const duration = durationMs ?? 2000; // approx 1.8–2.2s target

  // Mark stepIndex as completed at the exact time the last status appears.
  // This is intentionally separate from phase exit timing for cinematic pulse.

  const reducedMotion = useMemo(() => {

    try {
      return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
    } catch {
      return false;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    // Trigger AI Core power-up animation right away on mount.
    if (!reducedMotion) {
      setPoweredUp(true);
      // keep boolean true throughout the transition lifecycle
    }


    // Sequential reveal timings.
    // We target roughly: enter fade-in + status sequence + then exit transition.
    const statusRevealStart = reducedMotion ? 150 : 220;
    const perItem = reducedMotion ? 180 : Math.max(170, Math.floor((duration - 520) / BASE_STATUS.length));

    const run = async () => {
      setPhase('enter');
      await wait(reducedMotion ? 100 : 160);
      if (cancelled) return;
      setPhase('running');

      for (let i = 0; i < BASE_STATUS.length; i++) {
        await wait(statusRevealStart + i * perItem - (i === 0 ? statusRevealStart : 0));
        if (cancelled) return;
        setStepIndex(i);
      }

      // short hold so the user feels "authenticated"
      await wait(reducedMotion ? 120 : 260);
      if (cancelled) return;
      setPhase('exit');
      await wait(reducedMotion ? 220 : 420);
      if (cancelled) return;
      onComplete();

    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [duration, onComplete, reducedMotion]);

  const progressPct = ((Math.max(0, stepIndex) + 1) / BASE_STATUS.length) * 100;

  const isDone = stepIndex >= BASE_STATUS.length - 1;


  return (
    <div className={`ts-root ${phase}`} role="status" aria-live="polite">
      {/* Futuristic enterprise background */}
      <PlexusNeural3DBackground />
      <NeuralNetworkBackground />

      <div className="ts-vignette" aria-hidden="true" />
      <div className="ts-center-wrap">
        <div className="ts-card">
          <div className="ts-camera" aria-hidden="true" />

          <div className="ts-header" aria-live="polite">
            <div className="ts-welcome">
              Welcome back, <span className="ts-username">{username}</span>
            </div>
            <div className="ts-init">Preparing your Intelligence Workspace</div>

          </div>

          <div className={`ts-core ${poweredUp ? 'ts-core--powered' : ''} ${isDone ? 'ts-core--complete' : ''}`} aria-hidden="true">
            <div className="ts-core__rings" />
            <div className="ts-core__particles" />
            <div className="ts-core__trails" />
            <div className="ts-core__packets" />
            <div className="ts-core__halo" />

            <div className="ts-core-metrics" aria-hidden="true">
              {SYSTEM_METRICS.map((m, idx) => (
                <div key={m.label} className={`ts-metric ts-metric--${idx}`}>
                  <span className="ts-metric__k">{m.label}</span>
                  <b className="ts-metric__v">{m.value}</b>
                </div>
              ))}
            </div>
          </div>



          <div className="ts-status">

            {BASE_STATUS.map((s, i) => {
              const status = i < stepIndex ? 'done' : i === stepIndex ? 'active' : 'pending';
              return (
                <div key={s.label} className={`ts-status-row ${status}`} style={{ ['--i' as any]: i }}>
                  <span className="ts-check" aria-hidden="true">
                    <ShieldCheck size={15} />
                  </span>
                  <span className="ts-status-text">{s.label}</span>
                  <span className="ts-status-micro">{s.micro}</span>
                </div>
              );
            })}

            <div className="ts-progress" aria-hidden="true">
              <div className="ts-progress__bar" style={{ width: `${progressPct}%` }} />
            </div>
          </div>

          <MicroActivation />

          <div className="ts-footer" aria-hidden="true">
            <span className="ts-footer-item">
              <Cpu size={14} /> Secure orchestration
            </span>
            <span className="ts-footer-item">
              <Network size={14} /> Evidence channels
            </span>
            <span className="ts-footer-item">
              <Database size={14} /> Enterprise sync
            </span>
            <span className="ts-footer-item">
              <Activity size={14} /> Real-time preparation
            </span>
          </div>

          <div className="ts-perf-chroma" aria-hidden="true" />
        </div>

        <div className="ts-hint" aria-hidden="true">
          <Sparkles size={14} /> Authenticating into an intelligent enterprise system
        </div>
      </div>
    </div>
  );
}

