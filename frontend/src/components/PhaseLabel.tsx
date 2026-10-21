import { AnimatePresence, motion, useIsPresent, useReducedMotion } from './Motion';

function Label({ text, reduced }: { text: string; reduced: boolean }) {
  const present = useIsPresent();
  return <motion.span aria-hidden={!present || undefined} style={{ gridArea: '1 / 1' }}
    initial={{ opacity: reduced ? 1 : 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
    transition={{ duration: reduced ? 0 : .18 }}>{text}</motion.span>;
}

export function PhaseLabel({ text }: { text: string }) {
  const reduced = useReducedMotion();
  return <span className="space-phase-label"><AnimatePresence initial={false}>
    <Label key={text} text={text} reduced={reduced} />
  </AnimatePresence></span>;
}
