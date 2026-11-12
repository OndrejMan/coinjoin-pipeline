write_s3_credentials
# The frontend stages .pipeline/exporters/ into this prefix before creating the
# Job, so that path is expected here; anything else still means a reused run id.
# The filter must survive both `s5cmd ls` output shapes: a recursive listing of
# full keys (.pipeline/exporters/...) if the wildcard crosses "/", and a plain
# "DIR .pipeline/" row if it does not. Matching on `.pipeline` alone covers both;
# nothing else the pipeline writes carries that string, and a genuinely reused
# prefix still shows its own rows.
set +e
listing="$(s5 ls "$ARTIFACT_URI/$RUN_ID/*" 2>&1)"
status=$?
set -e
if [ "$status" -eq 0 ]; then
  unexpected="$(printf '%s\n' "$listing" | grep -v '\.pipeline' || true)"
  if [ -n "$unexpected" ]; then
    echo "run prefix $ARTIFACT_URI/$RUN_ID/ already contains artifacts; choose a fresh --run-id" >&2
    printf '%s\n' "$unexpected" >&2
    exit 1
  fi
elif ! printf '%s\n' "$listing" | grep -qi 'no object found'; then
  printf '%s\n' "$listing" >&2
  exit "$status"
fi
# Ignoring the exporters is only half the check: an empty or partial staging
# step would pass it just as well, so verify every required exporter is present.
for required in @REQUIRED_EXPORTERS@; do
  if ! s5 ls "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/$required" >/dev/null 2>&1; then
    echo "staged exporters are incomplete: missing $required" >&2
    exit 1
  fi
done
