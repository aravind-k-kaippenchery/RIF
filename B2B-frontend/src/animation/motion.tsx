import type React from 'react';

// Avoid hard dependency on framer-motion to fix build/runtime white screens.
// If framer-motion is available, we'll use it. Otherwise, fall back to plain rendering.
let AnimatePresence: any = ({ children }: any) => children;
let motion: any = { div: ({ children }: any) => children };
// Dynamic import fallback. We also declare the module type locally to avoid TS build failures.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
// (intentionally no import types from 'framer-motion' to keep TS build unblocked)


try {
  // Vite supports dynamic import; catch keeps the UI alive.
  // eslint-disable-next-line @typescript-eslint/no-floating-promises
  import('framer-motion' as any).then((fm) => {
    AnimatePresence = (fm as any).AnimatePresence;
    motion = (fm as any).motion;
  });
} catch {
  // no-op fallback
}


export { AnimatePresence, motion };


export type PageTransitionProps = { children: React.ReactNode };

export function PageTransition({ children }: PageTransitionProps) {
  // framer-motion respects reduced motion; fallback renders children directly.
  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.div
        initial={{ opacity: 0, y: 10, filter: 'blur(8px) scale(0.985)' }}
        animate={{ opacity: 1, y: 0, filter: 'blur(0px) scale(1)' }}
        exit={{ opacity: 0, y: -6, filter: 'blur(10px) scale(0.99)' }}
        transition={{ duration: 0.42, ease: [0.22, 1, 0.36, 1] }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}



