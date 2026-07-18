import React from 'react';
import { Sparkles, Timer } from 'lucide-react';

export function LoadingGate({
  name,
  subtitle = 'Please wait a sec…',
}: {
  name: string;
  subtitle?: string;
}) {
  return (
    <div className="login-page gate-page" role="status" aria-live="polite">
      <div className="gate-orbit" aria-hidden="true" />
      <div className="gate-orbit gate-orbit-2" aria-hidden="true" />
      <div className="gate-content">
        <div className="brand-row fade-rise delay-1" style={{ marginBottom: 18 }}>
          <span className="gate-logo">
            <Sparkles size={18} />
            <i />
            <b />
          </span>
          <div className="brand-word">
            B2B <span>Intelligence</span>
          </div>
        </div>

        <div className="eyebrow compact fade-rise delay-2">
          <i /> WORKSPACE READYING
        </div>

        <h1 className="gate-title">
          {name} <em>is directed to</em>
          <br />
          <span className="gate-em">WORKSPACE</span>
        </h1>

        <p className="gate-subtitle fade-rise delay-5">
          <Timer size={14} /> {subtitle}
        </p>

        <div className="gate-progress" aria-hidden="true">
          <span />
        </div>

        <div className="gate-footer">Local-first · Evidence-grounded · Safe actions</div>
      </div>
    </div>
  );
}

