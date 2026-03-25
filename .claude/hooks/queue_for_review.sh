#!/bin/bash
# Smart review gate — køres kun når det faktisk giver mening

set -euo pipefail

# ── 1. Ingen git repo / ingen ændringer → afslut stille ──────────────────────
if ! git rev-parse --git-dir &>/dev/null; then exit 0; fi

DIFF=$(git diff HEAD 2>/dev/null)
if [ -z "$DIFF" ]; then exit 0; fi

# ── 2. Find kun ændrede kodfiler (ikke data/config/docs) ─────────────────────
CHANGED_CODE=$(git diff HEAD --name-only 2>/dev/null \
  | grep -E '\.(py|js|ts|tsx|jsx|sh|sql)$' || true)

if [ -z "$CHANGED_CODE" ]; then
  echo "[review-gate] Kun data/config ændret — springer review over" >&2
  exit 0
fi

# ── 3. Tæl ændrede kodelinjer (ekskl. tomme linjer og kommentarer) ───────────
CODE_DIFF=$(git diff HEAD -- $CHANGED_CODE 2>/dev/null)

CHANGED_LINES=$(echo "$CODE_DIFF" \
  | grep -E '^[+-]' \
  | grep -vE '^(\+\+\+|---)' \
  | grep -vE '^[+-]\s*(#|//|$)' \
  | wc -l | tr -d ' ')

# ── 4. Tjek for strukturelle ændringer (nye funktioner / klasser) ─────────────
STRUCTURAL=$(echo "$CODE_DIFF" \
  | grep -E '^\+.*(def |class |async def |function |const .* = \(|=>)' \
  | grep -v '^+++' || true)

# ── 5. Tjek for nye filer ─────────────────────────────────────────────────────
NEW_FILES=$(git diff HEAD --name-only --diff-filter=A 2>/dev/null \
  | grep -E '\.(py|js|ts|tsx|jsx|sh|sql)$' || true)

# ── 6. Beslutningslogik ───────────────────────────────────────────────────────
REASON=""

if [ -n "$NEW_FILES" ]; then
  REASON="ny fil oprettet: $NEW_FILES"
elif [ -n "$STRUCTURAL" ]; then
  REASON="strukturelle ændringer (def/class/function)"
elif [ "$CHANGED_LINES" -ge 15 ]; then
  REASON="${CHANGED_LINES} ændrede kodelinjer"
fi

if [ -z "$REASON" ]; then
  echo "[review-gate] ${CHANGED_LINES} linjer, ingen struktur, ingen ny fil — springer review over" >&2
  exit 0
fi

echo "[review-gate] Kører Claude review (årsag: ${REASON})..." >&2

# ── 7. Kør Claude non-interaktivt ─────────────────────────────────────────────
DIFF_SNIPPET=$(echo "$CODE_DIFF" | head -300)

REVIEW=$(claude -p "Review this git diff. Only flag real issues: bugs, logic errors, security problems, or important improvements. Skip style/formatting. Be brief.

$DIFF_SNIPPET" 2>/dev/null) || true

if [ -n "$REVIEW" ]; then
  echo ""
  echo "── Claude review ──────────────────────────────"
  echo "$REVIEW"
  echo "───────────────────────────────────────────────"
fi

echo "[review-gate] Færdig." >&2
