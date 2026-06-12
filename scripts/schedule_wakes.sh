#!/bin/bash
# Schedule RTC wake-ups so the sleeping Mac wakes for each trading cycle.
# Run with: sudo bash ~/ai-portfolio/scripts/schedule_wakes.sh
# One-shot wakes through Fri Jun 19, 2026 + a repeating daily fallback.
set -e

# Clear any prior one-shot wake events we created (ignore errors)
pmset schedule cancelall 2>/dev/null || true

for day in 12 13 14 15 16 17 18 19; do
  pmset schedule wake "06/${day}/26 09:00:30"
  pmset schedule wake "06/${day}/26 12:00:30"
  pmset schedule wake "06/${day}/26 16:30:30"
done

# Repeating fallback: wake every day at 09:00:30 even after Jun 19
pmset repeat wakeorpoweron MTWRFSU 09:00:30

echo "Scheduled wakes:"
pmset -g sched
