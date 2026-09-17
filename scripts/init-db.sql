-- Official postgres entrypoint runs this only on first initialization.
-- Read the password from the environment and quote it as a SQL literal.
\getenv app_password POSTGRES_APP_PASSWORD
CREATE ROLE tutor_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
ALTER DATABASE tutor_lead_monitor OWNER TO tutor_app;
ALTER SCHEMA public OWNER TO tutor_app;
