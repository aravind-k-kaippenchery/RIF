import React, { useEffect, useMemo, useState } from 'react';

type BusinessBackgroundProps = {
  intensity?: 'low' | 'normal';
};

export function BusinessBackground({ intensity = 'normal' }: BusinessBackgroundProps) {
  const [enabled, setEnabled] = useState(true);

  useEffect(() => {
    try {
      const prefersReduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
      setEnabled(!prefersReduced);
    } catch {
      setEnabled(true);
    }
  }, []);

  const points = useMemo(() => {
    // Deterministic pseudo-layout (no animation loops, just subtle CSS drift)
    const count = intensity === 'low' ? 10 : 14;
    return Array.from({ length: count }).map((_, i) => {
      const x = (i * 73) % 100;
      const y = (i * 41) % 100;
      const s = 0.6 + ((i * 29) % 100) / 180;
      const delay = ((i % 7) * 0.35).toFixed(2);
      const hueShift = (i % 3) * 36;
      return { x, y, s, delay: Number(delay), hueShift };
    });
  }, [intensity]);

  return (
    <div
      className={`business-bg ${enabled ? 'is-enabled' : 'is-disabled'}`}
      aria-hidden="true"
    >
      <div className="business-bg__wash" />
      <div className="business-bg__grid" />

      {points.map((p, i) => (
        <span
          key={i}
          className="business-bg__dot"
          style={
            {
              left: `${p.x}%`,
              top: `${p.y}%`,
              transform: `translate(-50%, -50%) scale(${p.s})`,
              animationDelay: `${p.delay}s`,
              ['--hue-shift' as any]: `${p.hueShift}deg`,
            } as React.CSSProperties
          }
        />
      ))}

      <div className="business-bg__sweep" />
    </div>
  );
}

