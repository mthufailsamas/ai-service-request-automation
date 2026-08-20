\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
    IF (SELECT count(*) FROM users) <> 4 THEN
        RAISE EXCEPTION 'Expected 4 fictional users';
    END IF;

    IF (SELECT count(*) FROM managed_systems) <> 3 THEN
        RAISE EXCEPTION 'Expected 3 managed systems';
    END IF;

    IF (SELECT count(*) FROM system_permissions) <> 5 THEN
        RAISE EXCEPTION 'Expected 5 system permissions';
    END IF;

    IF (SELECT count(*) FROM users WHERE employee_reference = 'MGR-104') <> 1 THEN
        RAISE EXCEPTION 'Exact approver reference MGR-104 was not found once';
    END IF;

    IF EXISTS (SELECT 1 FROM users WHERE employee_reference = 'MGR-10') THEN
        RAISE EXCEPTION 'Truncated reference MGR-10 must not resolve';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM users AS u
        JOIN user_roles AS r USING (user_id)
        JOIN system_permissions AS p USING (user_id)
        JOIN managed_systems AS s USING (system_id)
        WHERE u.employee_reference = 'MGR-104'
          AND u.is_active
          AND r.role_code = 'APPROVER'
          AND p.permission_code = 'APPROVE_ACCESS'
          AND p.is_active
          AND s.system_code = 'WMS'
          AND s.is_active
    ) THEN
        RAISE EXCEPTION 'MGR-104 must be an active WMS access approver';
    END IF;

    IF EXISTS (
        SELECT lower(btrim(alias_name))
        FROM managed_systems
        CROSS JOIN LATERAL unnest(aliases) AS alias_name
        WHERE is_active
        GROUP BY lower(btrim(alias_name))
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Active system aliases are ambiguous';
    END IF;
END;
$$;

DO $$
BEGIN
    BEGIN
        INSERT INTO user_roles (user_id, role_code)
        VALUES ('10000000-0000-4000-8000-000000000001', 'UNKNOWN_ROLE');
        RAISE EXCEPTION 'Invalid role was accepted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO system_permissions (user_id, system_id, permission_code)
        VALUES (
            '10000000-0000-4000-8000-000000000001',
            '20000000-0000-4000-8000-000000000001',
            'REQUEST_ACCESS'
        );
        RAISE EXCEPTION 'Duplicate permission was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO system_permissions (user_id, system_id, permission_code)
        VALUES (
            '99999999-9999-4999-8999-999999999999',
            '20000000-0000-4000-8000-000000000001',
            'VIEW_STATUS'
        );
        RAISE EXCEPTION 'Permission for an unknown user was accepted';
    EXCEPTION
        WHEN foreign_key_violation THEN NULL;
    END;
END;
$$;

SELECT 'PASS: stage 1 identity and reference database checks' AS result;

ROLLBACK;
