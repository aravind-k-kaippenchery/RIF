import React, { useEffect, useMemo, useRef, useState } from 'react';

type BlockShimmerBackgroundProps = {
  enabled?: boolean;
  blockCount?: number;
};

export function BlockShimmerBackground({
  enabled = true,
  blockCount = 28,
}: BlockShimmerBackgroundProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const prefersReduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
    setActive(enabled && !prefersReduced);
  }, [enabled]);

  useEffect(() => {
    if (!active) return;
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
  }, [active]);

  const blocks = useMemo(() => Array.from({ length: blockCount }), [blockCount]);

  return (
    <div
      ref={ref}
      className={`block-shimmer ${active ? 'enabled' : 'disabled'}`}
      aria-hidden="true"
    >
      {blocks.map((_, i) => (
        <span
          key={i}
          className={`block-shimmer__b b-${i}`}
          style={{
            // Deterministic but varied
            ['--sx' as any]: `${(i * 37) % 100}%`,
            ['--sy' as any]: `${(i * 19) % 100}%`,
            ['--dur' as any]: `${6 + ((i * 11) % 7)}s`,
            ['--delay' as any]: `${(i % 9) * 0.12}s`,
          }}
        />
      ))}
    </div>
  );
}

