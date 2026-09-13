"""Dedicated runtime and backup roles; run after the one-shot migration job."""
import re
from pathlib import Path

APPEND_ONLY = {'core_auditevent', 'finance_payment', 'finance_settlementline',
               'finance_exceptionnote', 'appointments_appointmenthistory'}


def grant_runtime(cursor, database, user, password, backup=False):
    for value in [database, user]:
        if not re.fullmatch(r'[a-zA-Z0-9_]+', value):
            raise ValueError('Invalid database/account identifier.')
    cursor.execute(f"CREATE USER IF NOT EXISTS '{user}'@'%%' IDENTIFIED BY %s", [password])
    cursor.execute(f"ALTER USER '{user}'@'%%' IDENTIFIED BY %s", [password])
    cursor.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{user}'@'%'")
    cursor.execute(f"SHOW TABLES FROM {database}")
    tables = [row[0] for row in cursor.fetchall()]
    if not tables:
        raise RuntimeError('Apply migrations before provisioning runtime grants.')
    for table in tables:
        if not re.fullmatch(r'[a-zA-Z0-9_]+', table):
            raise ValueError('Unexpected table identifier.')
        grants = 'SELECT' if backup else 'SELECT, INSERT' if table in APPEND_ONLY else 'SELECT, INSERT, UPDATE, DELETE'
        cursor.execute(f"GRANT {grants} ON {database}.{table} TO '{user}'@'%'")


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import django
    django.setup()
    from django.conf import settings
    from django.db import connection
    database = settings.DATABASES['default']['NAME']
    if database not in ['smartcare', 'smartcare_sandbox']:
        raise SystemExit('Refusing unexpected deployment database.')
    with connection.cursor() as cursor:
        grant_runtime(cursor, database, 'smartcare_runtime', Path('/run/secrets/runtime_db_password').read_text().strip())
        grant_runtime(cursor, database, 'smartcare_backup', Path('/run/secrets/backup_db_password').read_text().strip(), backup=True)
    print('Runtime and read-only backup roles provisioned; ledger/audit updates are not granted.')
