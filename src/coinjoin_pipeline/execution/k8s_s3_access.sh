# Shared header of the in-pod S3 scripts (prefix preflight and uploader).
set -euo pipefail
command -v s5cmd >/dev/null || { echo "s5cmd is required" >&2; exit 1; }
# The credentials live on a memory-backed volume and only while the script runs.
trap 'rm -f /credentials/credentials' EXIT
write_s3_credentials() {
  mkdir -p /credentials
  umask 077
  printf '[coinjoin]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
    "$S3_ACCESS_KEY_ID" "$S3_SECRET_ACCESS_KEY" > /credentials/credentials
}
# s5cmd with the Job's credentials only: no AWS_* variable of the image may leak in.
s5() {
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_PROFILE -u AWS_DEFAULT_PROFILE -u AWS_REGION -u AWS_DEFAULT_REGION \
    s5cmd --credentials-file /credentials/credentials \
    --profile coinjoin --endpoint-url "$S3_ENDPOINT_URL" "$@"
}
