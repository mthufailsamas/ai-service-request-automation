BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_reference varchar(50) NOT NULL UNIQUE,
    email varchar(254) NOT NULL UNIQUE,
    display_name varchar(120) NOT NULL,
    password_hash text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_employee_reference_format CHECK (
        employee_reference = upper(btrim(employee_reference))
        AND employee_reference ~ '^[A-Z][A-Z0-9-]{1,49}$'
    ),
    CONSTRAINT users_email_normalized CHECK (
        email = lower(btrim(email))
        AND email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
    ),
    CONSTRAINT users_display_name_not_blank CHECK (btrim(display_name) <> ''),
    CONSTRAINT users_password_hash_not_blank CHECK (btrim(password_hash) <> ''),
    CONSTRAINT users_updated_after_created CHECK (updated_at >= created_at)
);

CREATE TABLE user_roles (
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role_code varchar(30) NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role_code),
    CONSTRAINT user_roles_role_code_allowed CHECK (
        role_code IN ('REQUESTER', 'SERVICE_AGENT', 'APPROVER', 'ADMIN')
    )
);

CREATE TABLE managed_systems (
    system_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    system_code varchar(50) NOT NULL UNIQUE,
    system_name varchar(120) NOT NULL UNIQUE,
    aliases text[] NOT NULL DEFAULT '{}'::text[],
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT managed_systems_code_format CHECK (
        system_code = upper(btrim(system_code))
        AND system_code ~ '^[A-Z][A-Z0-9_-]{1,49}$'
    ),
    CONSTRAINT managed_systems_name_not_blank CHECK (btrim(system_name) <> ''),
    CONSTRAINT managed_systems_aliases_no_null CHECK (
        array_position(aliases, NULL) IS NULL
    ),
    CONSTRAINT managed_systems_updated_after_created CHECK (
        updated_at >= created_at
    )
);

CREATE TABLE system_permissions (
    permission_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    system_id uuid NOT NULL REFERENCES managed_systems(system_id) ON DELETE CASCADE,
    permission_code varchar(40) NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT system_permissions_code_allowed CHECK (
        permission_code IN (
            'VIEW_STATUS',
            'REQUEST_ACCESS',
            'REQUEST_DATA_CHANGE',
            'APPROVE_ACCESS',
            'APPROVE_DATA_CHANGE'
        )
    ),
    CONSTRAINT system_permissions_unique_grant UNIQUE (
        user_id,
        system_id,
        permission_code
    )
);

CREATE INDEX system_permissions_active_user_idx
    ON system_permissions (user_id, system_id)
    WHERE is_active;

CREATE INDEX managed_systems_active_code_idx
    ON managed_systems (system_code)
    WHERE is_active;

COMMENT ON TABLE users IS
    'Fictional people who submit, review, approve, or administer requests.';
COMMENT ON TABLE user_roles IS
    'Business roles assigned to each fictional user.';
COMMENT ON TABLE managed_systems IS
    'Business systems that may be referenced by service requests.';
COMMENT ON TABLE system_permissions IS
    'Exact user permissions for requesting, viewing, or approving system work.';

COMMIT;
