write_s3_credentials
set +e
listing="$(s5 ls "$ARTIFACT_URI/$RUN_ID/*" 2>&1)"
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo "run prefix $ARTIFACT_URI/$RUN_ID/ already contains artifacts; choose a fresh --run-id" >&2
  printf '%s\n' "$listing" >&2
  exit 1
elif ! printf '%s\n' "$listing" | grep -qi 'no object found'; then
  printf '%s\n' "$listing" >&2
  exit "$status"
fi
