"""Registry of reserved transaction-id / user-id ranges used by pipeline
tests and the synthetic load generator, so a new task can pick a
non-overlapping range without re-grepping every existing test file first.

Plain documented constants only -- no collision-detection logic, no
allocation function. That would be solving a problem that doesn't exist yet
(same restraint applied to not sharing a base class between ReplayHarness
and LoadGenerator).

Tasks 1-3's ranges are documented below by reference/comment only -- those
test files are closed and are not edited to import from this module.
"""

# Task 1 (tests/pipeline/test_loader.py):
#   transaction ids: -3001..-3004, -3009..-3011
#   user_id: -1001

# Task 2 (tests/pipeline/test_replay.py):
#   transaction ids: 4,000,000..4,000,999
#   user_id: 1 (hardcoded, not reserved -- coincidentally safe only because
#   those tests are id-range-isolated and never query by user_id)

# Task 3 (tests/pipeline/test_baseline.py):
#   transaction ids: 6,000,000+
#   user_id: -2001, -2099

# Task 3 (tests/pipeline/test_pipeline.py):
#   transaction ids: 7,000,000..7,000,999
#   user_id: -3001, -3002, -3003

# --- Task 4 onward: actually imported, not just documented. ---

# Full range reserved for LoadGenerator's own runs (not just its tests) -- a
# stress run can consume far more ids than a fixed test. Reserves a full
# order of magnitude of headroom above the highest prior reservation
# (7,000,008). 8,000,000-9,999,999 is left as an explicit gap for Task 5/6.
LOAD_GENERATOR_ID_RANGE_START = 10_000_000
LOAD_GENERATOR_ID_RANGE_END = 99_999_999

# Narrow sub-range within the above, for Task 4's own deterministic tests.
LOAD_GENERATOR_TEST_ID_BASE = 10_000_000

# Synthetic user id pool for the load generator -- distinct negative range,
# clearly distant from Tasks 1/3's -1001..-3099 cluster.
LOAD_GENERATOR_USER_ID_POOL_START = -10050
LOAD_GENERATOR_USER_POOL_SIZE = 50

# Task 6 (tests/pipeline/test_run_pipeline.py): uses the 8,000,000-9,999,999
# gap left open above, for the one subprocess-based CLI test. It owns only
# 8,000,000..8,000,999.
RUN_PIPELINE_CLI_TEST_ID_BASE = 8_000_000
RUN_PIPELINE_CLI_TEST_USER_ID = -4001
