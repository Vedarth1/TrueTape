-- POSTGRES_USER (truetape_owner) is the superuser and owns every table.
-- The Flask app connects as truetape_app, which owns nothing.
CREATE ROLE truetape_app WITH LOGIN PASSWORD 'truetape_app_pw';

GRANT CONNECT ON DATABASE truetape TO truetape_app;
GRANT USAGE   ON SCHEMA   public   TO truetape_app;

-- Every table the owner creates from now on is automatically readable and
-- writable by the app role. Without this, each `flask db migrate` would
-- produce tables the app cannot see.
ALTER DEFAULT PRIVILEGES FOR ROLE truetape_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO truetape_app;
ALTER DEFAULT PRIVILEGES FOR ROLE truetape_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO truetape_app;