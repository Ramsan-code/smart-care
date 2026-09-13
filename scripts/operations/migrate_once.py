import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import django
django.setup()
from django.db import connection
from django.core.management import call_command
with connection.cursor() as cursor:
    cursor.execute("SELECT GET_LOCK('smartcare:migrations', 0)")
    if cursor.fetchone()[0] != 1:
        raise SystemExit('Another release holds the migration lock.')
    try:
        call_command('migrate', interactive=False)
    finally:
        cursor.execute("SELECT RELEASE_LOCK('smartcare:migrations')")
