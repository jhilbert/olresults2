#!/bin/zsh

script_dir="${0:A:h}"
cd -- "$script_dir" || exit 1

python3 build/sync_local.py
status=$?

if (( status != 0 )) && [[ -t 0 ]]; then
  echo
  echo "Taste drücken, um dieses Fenster zu schließen …"
  read -k 1
fi

exit $status
