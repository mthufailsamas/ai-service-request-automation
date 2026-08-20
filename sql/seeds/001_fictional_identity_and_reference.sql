BEGIN;

-- These accounts are fictional. Random one-way password hashes make the seed
-- accounts intentionally unusable for interactive login at this stage.
INSERT INTO users (
    user_id,
    employee_reference,
    email,
    display_name,
    password_hash
) VALUES
    (
        '10000000-0000-4000-8000-000000000001',
        'EMP-201',
        'maya.requester@example.test',
        'Maya Pratama',
        crypt(gen_random_uuid()::text, gen_salt('bf', 12))
    ),
    (
        '10000000-0000-4000-8000-000000000002',
        'AGT-301',
        'raka.agent@example.test',
        'Raka Wijaya',
        crypt(gen_random_uuid()::text, gen_salt('bf', 12))
    ),
    (
        '10000000-0000-4000-8000-000000000003',
        'MGR-104',
        'sinta.approver@example.test',
        'Sinta Mahendra',
        crypt(gen_random_uuid()::text, gen_salt('bf', 12))
    ),
    (
        '10000000-0000-4000-8000-000000000004',
        'ADM-001',
        'dimas.admin@example.test',
        'Dimas Santoso',
        crypt(gen_random_uuid()::text, gen_salt('bf', 12))
    );

INSERT INTO user_roles (user_id, role_code) VALUES
    ('10000000-0000-4000-8000-000000000001', 'REQUESTER'),
    ('10000000-0000-4000-8000-000000000002', 'REQUESTER'),
    ('10000000-0000-4000-8000-000000000002', 'SERVICE_AGENT'),
    ('10000000-0000-4000-8000-000000000003', 'REQUESTER'),
    ('10000000-0000-4000-8000-000000000003', 'APPROVER'),
    ('10000000-0000-4000-8000-000000000004', 'ADMIN');

INSERT INTO managed_systems (
    system_id,
    system_code,
    system_name,
    aliases
) VALUES
    (
        '20000000-0000-4000-8000-000000000001',
        'WMS',
        'Warehouse Management System',
        ARRAY['warehouse system', 'inventory portal']
    ),
    (
        '20000000-0000-4000-8000-000000000002',
        'CRM',
        'Customer Relationship Management',
        ARRAY['customer portal', 'sales workspace']
    ),
    (
        '20000000-0000-4000-8000-000000000003',
        'HRIS',
        'Human Resources Information System',
        ARRAY['employee portal', 'people system']
    );

INSERT INTO system_permissions (
    permission_id,
    user_id,
    system_id,
    permission_code
) VALUES
    (
        '30000000-0000-4000-8000-000000000001',
        '10000000-0000-4000-8000-000000000001',
        '20000000-0000-4000-8000-000000000001',
        'VIEW_STATUS'
    ),
    (
        '30000000-0000-4000-8000-000000000002',
        '10000000-0000-4000-8000-000000000001',
        '20000000-0000-4000-8000-000000000001',
        'REQUEST_ACCESS'
    ),
    (
        '30000000-0000-4000-8000-000000000003',
        '10000000-0000-4000-8000-000000000002',
        '20000000-0000-4000-8000-000000000001',
        'REQUEST_DATA_CHANGE'
    ),
    (
        '30000000-0000-4000-8000-000000000004',
        '10000000-0000-4000-8000-000000000003',
        '20000000-0000-4000-8000-000000000001',
        'APPROVE_ACCESS'
    ),
    (
        '30000000-0000-4000-8000-000000000005',
        '10000000-0000-4000-8000-000000000003',
        '20000000-0000-4000-8000-000000000001',
        'APPROVE_DATA_CHANGE'
    );

-- Aliases are application reference data. Two active systems must not share
-- the same normalized alias because routing would become ambiguous.
DO $$
BEGIN
    IF EXISTS (
        SELECT lower(btrim(alias_name))
        FROM managed_systems
        CROSS JOIN LATERAL unnest(aliases) AS alias_name
        WHERE is_active
        GROUP BY lower(btrim(alias_name))
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Active system aliases must be unique after normalization';
    END IF;
END;
$$;

COMMIT;
