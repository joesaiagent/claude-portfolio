#!/bin/bash
# Schedule RTC wake-ups so the sleeping Mac wakes for each trading cycle.
# Run with: sudo bash ~/ai-portfolio/scripts/schedule_wakes.sh
#
# Rolling 30-day window of one-shot wakes + a repeating daily fallback, so the
# schedule never fully expires. Re-run any time to roll the window forward.
#
# Wakes fire ~4 min BEFORE each launchd cycle (9:00 / 12:00 / 16:30 ET) so the
# machine is already awake when the job triggers. Waking on non-trading days is
# harmless: runner.py skips weekends/holidays via broker.is_trading_day().
set -e

# Clear any prior one-shot wake events we created (ignore errors)
pmset schedule cancelall 2>/dev/null || true

DAYS=30
for i in $(seq 0 $((DAYS - 1))); do
  d=$(date -v+"${i}"d +%m/%d/%y)   # BSD/macOS date arithmetic
  pmset schedule wake "${d} 08:56:00"   # premarket  (job @ 9:00)
  pmset schedule wake "${d} 11:56:00"   # midday     (job @ 12:00)
  pmset schedule wake "${d} 16:26:00"   # postclose  (job @ 16:30)
done

# Never-expiring fallback: wake every day at 08:56 even if this isn't re-run.
pmset repeat wakeorpoweron MTWRFSU 08:56:00

echo "Scheduled wakes (rolling ${DAYS}-day window + daily repeat):"
pmset -g sched
