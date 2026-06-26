#!/bin/bash
# Schedule RTC wake-ups so the sleeping Mac wakes for each trading cycle.
# Run with: sudo bash ~/ai-portfolio/scripts/schedule_wakes.sh
#
# WHY THIS IS DRIVEN BY A LAUNCHD RENEWAL JOB
# -------------------------------------------
# macOS `pmset repeat` supports only ONE repeating wake time, so a single repeat
# entry can cover at most ONE of our three daily cycles (premarket 08:56 /
# midday 11:56 / postclose 16:26). To wake for all THREE we must use one-shot
# `pmset schedule wake` events -- but those are finite: they only cover the
# rolling window scheduled below and silently lapse once consumed.
#
# To keep all three times covered forever, this script is run on a schedule by
# launchd/com.aibia.claudeportfolio.wakerenew.plist (weekly + RunAtLoad). Each
# run cancels the old one-shots and lays down a fresh DAYS-long window, so the
# window is continuously refreshed long before it can expire. The `pmset repeat`
# line below is only a last-resort safety net for the 08:56 premarket time in
# case the renewal job ever stops running.
#
# This script is idempotent: `pmset schedule cancelall` clears every prior
# one-shot we created and `pmset repeat ...` overwrites the single repeat slot,
# so frequent re-runs (e.g. weekly from launchd) never pile up duplicates.
#
# Wakes fire ~4 min BEFORE each launchd cycle (9:00 / 12:00 / 16:30 ET) so the
# machine is already awake when the job triggers. Waking on non-trading days is
# harmless: runner.py skips weekends/holidays via broker.is_trading_day().
set -e

# Clear any prior one-shot wake events we created (ignore errors).
# This is what makes re-running idempotent -- no duplicate pmset entries.
pmset schedule cancelall 2>/dev/null || true

DAYS=30
for i in $(seq 0 $((DAYS - 1))); do
  d=$(date -v+"${i}"d +%m/%d/%y)   # BSD/macOS date arithmetic
  pmset schedule wake "${d} 08:56:00"   # premarket  (job @ 9:00)
  pmset schedule wake "${d} 11:56:00"   # midday     (job @ 12:00)
  pmset schedule wake "${d} 16:26:00"   # postclose  (job @ 16:30)
done

# Last-resort fallback: a single repeating daily wake. pmset repeat allows only
# ONE time, so this covers only the 08:56 premarket cycle if the renewal job
# (launchd/com.aibia.claudeportfolio.wakerenew.plist) ever stops refreshing the
# one-shot window above. Midday/postclose rely on that renewal, not this line.
pmset repeat wakeorpoweron MTWRFSU 08:56:00

echo "Scheduled wakes (rolling ${DAYS}-day window + daily repeat):"
pmset -g sched
