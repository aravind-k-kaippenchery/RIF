import React from 'react';

export function SpiralBackground({
  intensity = 'normal',
}: {
  intensity?: 'low' | 'normal';
}) {
  const rings = intensity === 'low' ? 7 : 10;

  return (
    <div className={`spiral-bg spiral-bg--${intensity}`} aria-hidden="true">
      <div className="spiral-bg__wash" />
      <div className="spiral-bg__core" />

      {Array.from({ length: rings }).map((_, i) => (
        <span key={i} className={`spiral-bg__ring r-${i}`} />
      ))}

      <div className="spiral-bg__glow" />
    </div>
  );
}

