-- The suite's own database, created when the volume is initialised. The tests
-- truncate every table before each one, so they never touch the dev database.
CREATE DATABASE IF NOT EXISTS pyrt_test
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL ON pyrt_test.* TO 'pyrt'@'%';
