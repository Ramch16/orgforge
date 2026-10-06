"""Single-company cloud entrypoint. Credentials are runtime bindings, never image data."""
import argparse
import os
import sqlite3
from pathlib import Path


def application():
    from .company import Company
    from .server import create_app
    root=Path(os.environ.get('ORGFORGE_HOME','/var/lib/orgforge')).resolve()
    tokens={role:os.environ.get('ORGFORGE_'+role.upper()+'_TOKEN','') for role in ('ceo','cto')}
    if any(len(value)<32 for value in tokens.values()) or tokens['ceo']==tokens['cto']:
        raise ValueError('Hosted OrgForge requires distinct CEO/CTO runtime tokens of at least 32 characters.')
    company=Company(root,create=not (root/'.orgforge'/'company.db').exists())
    company.machine.installs_allowed=False   # a hosted server never installs software on its host
    return create_app(company,tokens)


def main():
    parser=argparse.ArgumentParser(description='Hosted single-company OrgForge')
    parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=4700)
    parser.add_argument('--backup',help='Write a consistent SQLite backup; project workspaces need separate volume backup')
    args=parser.parse_args()
    if args.backup:
        from .company import Company
        company=Company(os.environ.get('ORGFORGE_HOME','/var/lib/orgforge'))
        target=Path(args.backup).resolve()
        if target.exists():raise ValueError('Backup output already exists.')
        target.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(target) as connection:company.db.conn.backup(connection)
        return
    import uvicorn
    uvicorn.run(application(),host=args.host,port=args.port,log_level='info')


if __name__=='__main__':main()
