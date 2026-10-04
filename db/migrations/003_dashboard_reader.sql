SELECT (:'dashboard_user' = current_user) AS dashboard_user_is_owner
\gset

\if :dashboard_user_is_owner
    DO $block$
    BEGIN
        RAISE EXCEPTION 'DASHBOARD_DB_USER muss sich von POSTGRES_USER unterscheiden.';
    END
    $block$;
\endif

SELECT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = :'dashboard_user'
      AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
) AS dashboard_user_is_privileged
\gset

\if :dashboard_user_is_privileged
    DO $block$
    BEGIN
        RAISE EXCEPTION 'DASHBOARD_DB_USER verweist auf eine privilegierte Rolle.';
    END
    $block$;
\endif

SELECT EXISTS (
    SELECT 1
    FROM pg_auth_members memberships
    JOIN pg_roles members ON members.oid = memberships.member
    WHERE members.rolname = :'dashboard_user'
) AS dashboard_user_has_memberships
\gset

\if :dashboard_user_has_memberships
    DO $block$
    BEGIN
        RAISE EXCEPTION 'DASHBOARD_DB_USER darf keiner anderen Rolle angehören.';
    END
    $block$;
\endif

SELECT EXISTS (
    SELECT 1
    FROM pg_roles roles
    JOIN pg_shdepend dependencies
      ON dependencies.refclassid = 'pg_authid'::regclass
     AND dependencies.refobjid = roles.oid
     AND dependencies.deptype = 'o'
    WHERE roles.rolname = :'dashboard_user'
) AS dashboard_user_owns_objects
\gset

\if :dashboard_user_owns_objects
    DO $block$
    BEGIN
        RAISE EXCEPTION 'DASHBOARD_DB_USER darf keine Datenbankobjekte besitzen.';
    END
    $block$;
\endif

SELECT EXISTS (
    SELECT 1
    FROM pg_roles roles
    JOIN pg_shdepend dependencies
      ON dependencies.refclassid = 'pg_authid'::regclass
     AND dependencies.refobjid = roles.oid
     AND dependencies.deptype = 'a'
    WHERE roles.rolname = :'dashboard_user'
      AND NOT (
          dependencies.dbid = (
              SELECT oid
              FROM pg_database
              WHERE datname = current_database()
          )
          OR (
              dependencies.dbid = 0
              AND dependencies.classid = 'pg_database'::regclass
              AND dependencies.objid = (
                  SELECT oid
                  FROM pg_database
                  WHERE datname = current_database()
              )
          )
      )
) AS dashboard_user_has_external_privileges
\gset

\if :dashboard_user_has_external_privileges
    DO $block$
    BEGIN
        RAISE EXCEPTION 'DASHBOARD_DB_USER besitzt Rechte außerhalb der Projektdatenbank.';
    END
    $block$;
\endif

SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L',
    :'dashboard_user',
    :'dashboard_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = :'dashboard_user'
)
\gexec

SELECT format('DROP OWNED BY %I', :'dashboard_user')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'dashboard_user',
    :'dashboard_password'
)
\gexec

SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I',
    :'dashboard_user'
)
\gexec

SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I',
    :'dashboard_user'
)
\gexec

SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'dashboard_user')
\gexec

SELECT format(
    'GRANT CONNECT ON DATABASE %I TO %I',
    current_database(),
    :'dashboard_user'
)
\gexec

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'dashboard_user')
\gexec

SELECT format(
    'GRANT SELECT ON TABLE solarbank_measurements, collector_state, schema_migrations TO %I',
    :'dashboard_user'
)
\gexec

SELECT format(
    'ALTER ROLE %I SET default_transaction_read_only = on',
    :'dashboard_user'
)
\gexec

INSERT INTO schema_migrations (version) VALUES (3)
ON CONFLICT (version) DO NOTHING;
