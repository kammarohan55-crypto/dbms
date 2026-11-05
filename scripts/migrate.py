"""Apply the versioned local schema upgrade with schema-administrator access."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sqlalchemy.engine import make_url, URL
from gridmind.config import settings, ROOT
from gridmind.database import make_engine
from gridmind.migrations import upgrade, schema_problems


def main():
    parser = argparse.ArgumentParser(description='Preserve existing data while upgrading GRIDMIND schema')
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args()
    configured = make_url(settings.database_url)
    url = os.environ.get('GRIDMIND_MIGRATION_URL',settings.database_url)
    credentials = ROOT/'.runtime/mysql-credentials.json'
    if not args.check and configured.drivername.startswith('mysql') and configured.host=='127.0.0.1' and configured.port==3307 and configured.database=='gridmind' and credentials.exists():
        protected = json.loads(credentials.read_text(encoding='utf-8'))
        url = URL.create('mysql+pymysql',username='root',password=protected['root_password'],host='127.0.0.1',port=3307,database='gridmind')
    engine = make_engine(url)
    try:
        if args.check:
            problems = schema_problems(engine)
            print(json.dumps({'upgrade_required':bool(problems),'problems':problems}))
        else:
            print(json.dumps(upgrade(engine)))
    finally:
        engine.dispose()


if __name__=='__main__':
    main()
