#!/usr/bin/env bash
# Rebuild the private workspace for the shallow-memory T9 rule check inside an exact knowledge checkout.
# Usage: t9_shallow_memory_prepare.sh <knowledge checkout>
# Needs git credentials for the knowledge repo already configured (GIT_ASKPASS) when objects must be fetched.
# Prints counts only; no chat content reaches the log.
set -euo pipefail
repo="$1"
EXPORT_COMMIT=e611e649440a76d627ac2268de72836680af8f24
PACKET_COMMIT=01285f526db49ae4d51bdfb351632bc048884ad5
PACKET_DIR=evaluation_results/research_matter_phase0_candidates/2026-08-19_cec83f7c_run32268255221/candidate_packets
cd "$repo"
git cat-file -e "$EXPORT_COMMIT^{commit}" 2>/dev/null || git fetch -q --depth=1 origin "$EXPORT_COMMIT"
git cat-file -e "$PACKET_COMMIT^{commit}" 2>/dev/null || git fetch -q --depth=1 origin "$PACKET_COMMIT"
mkdir -p workspace/corpus workspace/t9_packets
python l0/tools/build_research_daily_group_corpus.py --db l0_state.db --output-dir workspace/corpus >/dev/null
python -m shallow_memory.prep.build --l0-db l0_state.db --export-commit "$EXPORT_COMMIT" >/dev/null
for f in 2026-07-08_8e2cfc446694.txt 2026-07-16_84f34dbd5f23.txt; do
  git show "$PACKET_COMMIT:$PACKET_DIR/$f" > "workspace/t9_packets/$f"
done
python - <<'PY'
import json
s = json.load(open("workspace/corpus/build_summary.json", encoding="utf-8"))
p = json.load(open("workspace/prep/people.json", encoding="utf-8"))
print(f"corpus_messages={s['final_message_count']} group_days={s['group_day_count']}")
print(f"people_resolved={p['coverage']['resolved']}/{p['coverage']['senders']}")
PY
echo 't9_workspace=READY'
