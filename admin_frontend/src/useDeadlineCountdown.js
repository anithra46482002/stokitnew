import React from 'react';
import { createDeadlineTicker, remainingSeconds } from './otpTimer.js';

// Seconds left until `deadline` (ms since epoch, 0 = none). One interval per hook instance, cleaned up on unmount or when
// the deadline changes. Remaining time is recomputed from the clock on every tick, so it never drifts or double-counts.
export function useDeadlineCountdown(deadline) {
  const [remaining, setRemaining] = React.useState(() => remainingSeconds(deadline));
  React.useEffect(() => {
    const ticker = createDeadlineTicker({ onTick: setRemaining });
    ticker.start(deadline);
    return () => ticker.stop();
  }, [deadline]);
  return remaining;
}
