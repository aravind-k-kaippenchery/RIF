import React, { useEffect, useMemo, useRef, useState } from 'react';

export function NeuralNetworkBackground() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    const prefersReduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
    setEnabled(!prefersReduced);
  }, []);

  useEffect(() => {
    if (!enabled) return;

    const el = ref.current;
    if (!el) return;

    let raf = 0;
    const onMove = (event: PointerEvent) => {
      const { innerWidth, innerHeight } = window;
      const x = (event.clientX / innerWidth) * 2 - 1;
      const y = (event.clientY / innerHeight) * 2 - 1;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        el.style.setProperty('--mx', String(x));
        el.style.setProperty('--my', String(y));
      });
    };

    window.addEventListener('pointermove', onMove, { passive: true });
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('pointermove', onMove);
    };
  }, [enabled]);

  const nodes = useMemo(() => Array.from({ length: 18 }), []);

  return (
    <div ref={ref} className={`neural-bg ${enabled ? 'enabled' : 'disabled'}`} aria-hidden="true">
      <div className="neural-grid" />
      <div className="neural-streams" />
      <div className="neural-particles">
        {nodes.map((_, i) => (
          <span key={i} className={`particle p-${i}`} />
        ))}
      </div>
      <div className="neural-orbs">
        <span className="orb orb-blue" />
        <span className="orb orb-purple" />
        <span className="orb orb-cyan" />
      </div>
      <div className="neural-network">
        {nodes.slice(0, 12).map((_, i) => (
          <span key={i} className={`net-node n-${i}`} />
        ))}
        <svg className="net-lines" viewBox="0 0 100 100" preserveAspectRatio="none">
          {Array.from({ length: 18 }).map((_, i) => (
            <path
              key={i}
              d={`M ${10 + i * 4} ${10 + (i % 6) * 13} C ${35 + (i % 4) * 9} ${15 + (i % 7) * 10}, ${60 + (i % 3) * 13} ${45 + (i % 5) * 6}, ${90 - i * 2} ${70 - (i % 6) * 9}`}
              className="net-line"
            />
          ))}
        </svg>
      </div>
    </div>
  );
}

