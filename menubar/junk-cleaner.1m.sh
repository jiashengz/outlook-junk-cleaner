#!/bin/bash
# <swiftbar.title>Junk cleaner</swiftbar.title>
# <swiftbar.desc>Status and controls for the Outlook.com Junk cleaner.</swiftbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>false</swiftbar.hideSwiftBar>
#
# SwiftBar menu bar plugin. It only READS the log and the schedule state every
# minute; the cleaner itself runs from the schedule, exactly as before.
# The ".1m." in the file name is SwiftBar's refresh interval.

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$DIR/logs/cleaner.log"
CLEAN="$DIR/move-junk-to-deleted.sh"
LABEL="local.junk-cleaner"

# SwiftBar treats " | " as the start of its options, so log text must not contain "|".
clean() { printf '%s' "$1" | sed -e 's/|/·/g'; }

last_done=$(grep -h "Run done" "$LOG" 2>/dev/null | tail -1)
last_line=$(tail -1 "$LOG" 2>/dev/null)
today=$(date +%Y-%m-%d)
moved_today=$(grep -c -E "^$today .*(Moved to Deleted Items|Permanently deleted)" "$LOG" 2>/dev/null || true); moved_today=${moved_today:-0}
if job=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null); then scheduled=1; else scheduled=0; fi
interval_min=$(( $(echo "$job" | awk '/run interval/ {print $4; exit}') / 60 ))

# Menu bar icon: trash = fine, triangle = the last log entry is a problem, pause = schedule off.
case "$last_line" in
  *" ERROR "*|*" WARNING "*) icon="exclamationmark.triangle" ;;
  *) icon="xmark.bin" ;;
esac
[ "$scheduled" = 0 ] && icon="pause.circle"
echo " | sfimage=$icon"
echo "---"

if [ -n "$last_done" ]; then
  echo "Last run ${last_done:11:5}: $(clean "${last_done#*Run done: }") | size=12"
else
  echo "No runs logged yet | size=12"
fi
echo "Moved today: $moved_today | size=12"
case "$last_line" in
  *" ERROR "*|*" WARNING "*) echo "Problem: $(clean "${last_line:28:140}") | color=red size=12" ;;
esac
if [ "$scheduled" = 1 ]; then
  echo "Schedule: on, every $interval_min minutes | size=12"
else
  echo "Schedule: paused | size=12 color=orange"
fi
echo "---"

echo "Run now | bash='$CLEAN' terminal=false refresh=true sfimage=play"
echo "Preview Junk (changes nothing) | bash='$CLEAN' param1=--survey terminal=true sfimage=eye"
echo "Find new spam patterns | bash='$CLEAN' param1=--discover terminal=true sfimage=sparkle.magnifyingglass"
if [ "$scheduled" = 1 ]; then
  echo "Pause schedule | bash='$DIR/schedule.sh' param1=uninstall terminal=false refresh=true sfimage=pause"
else
  echo "Resume schedule | bash='$DIR/schedule.sh' param1=install terminal=false refresh=true sfimage=play.circle"
fi
echo "---"

echo "Recent moves | sfimage=clock"
grep -h -E "Moved to Deleted Items|Permanently deleted" "$LOG" 2>/dev/null | tail -15 | tail -r | while IFS= read -r l; do
  # "2026-10-19 17:28:15 INFO    Moved to Deleted Items  (reason) <received> | sender | subject"
  when="${l:5:11}"
  rest="${l#*) }"                 # drop everything up to the rule reason
  sender_subject="${rest#* | }"   # drop the received date
  echo "--$when  $(clean "$sender_subject") | size=12 length=80"
done
echo "Open log | bash=/usr/bin/open param1=-a param2=Console param3='$LOG' terminal=false sfimage=doc.text"
echo "Edit rules | bash=/usr/bin/open param1=-t param2='$DIR/rules.toml' terminal=false sfimage=pencil"
echo "Sign in again | bash='$CLEAN' param1=--login terminal=true sfimage=person.badge.key"
