{preamble}
stage_finalize() {{
  index_unlock 2>/dev/null || :
}}
{start}
{index_reader}
INDEX_ZSTD_THREADS={ncpus}
index_backup
index_unlock
# The lab learns which height the backup holds from the run.
s5 cp "$INDEX_DIR/manifest.json" "$RUN_PREFIX/blocksci-index-backup.json"
echo "[blocksci-backup] index at height $(index_height "$INDEX_DIR") backed up to $INDEX_BACKUP"
