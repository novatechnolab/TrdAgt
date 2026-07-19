#!/data/data/com.termux/files/usr/bin/bash

# Prevent CPU from sleeping when screen is off
termux-wake-lock

SESSION="tradesignal"
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Kill existing session
tmux kill-session -t $SESSION 2>/dev/null

# Start the session in the script's directory and loop launch_tradesignal.sh
tmux new-session -d -s $SESSION -n "main" "cd \"$DIR\" && while true; do
  bash launch_tradesignal.sh
  echo 'Server stopped. Restarting in 5 seconds...'
  sleep 5
done"

echo "TradeSignal started in tmux session '$SESSION'."
echo "Attach with: tmux attach -t $SESSION"
