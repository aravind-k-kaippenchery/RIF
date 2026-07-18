import React from 'react';

export function BackgroundNeuralOrbits() {
  // lightweight canvas-free background layer to preserve performance.
  return (
    <div className="bg-orbits" aria-hidden="true">
      {Array.from({ length: 10 }).map((_, i) => (
        <span key={i} className={`orbit-node orbit-node-${i}`} />
      ))}
      <div className="bg-connections" />
      <div className="bg-particles" />
    </div>
  );
}

