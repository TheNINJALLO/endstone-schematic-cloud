-- Replace host and password values before running.
CREATE DATABASE IF NOT EXISTS `ninjos_schematics`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'schematics'@'YOUR_SERVER_IP'
  IDENTIFIED BY 'CHANGE_THIS_PASSWORD';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
  ON `ninjos_schematics`.*
  TO 'schematics'@'YOUR_SERVER_IP';

FLUSH PRIVILEGES;
