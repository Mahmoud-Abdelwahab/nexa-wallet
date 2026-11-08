"""Create local Docker settings once, without overwriting existing secrets."""
from pathlib import Path
import secrets


def main():
    target = Path('.env.docker')
    if target.exists():
        print('.env.docker already exists; left unchanged.')
        return
    template = Path('.env.docker.example').read_text()
    template = template.replace('SECRET_KEY=GENERATE_ME', 'SECRET_KEY=' + secrets.token_urlsafe(48))
    template = template.replace('MOYASAR_WEBHOOK_SECRET=GENERATE_ME',
                                'MOYASAR_WEBHOOK_SECRET=' + secrets.token_urlsafe(32))
    # Exclusive creation prevents an accidental overwrite if setup is run twice.
    with target.open('x') as output:
        output.write(template)
    target.chmod(0o600)
    print('Created .env.docker with random local secrets. No existing .env was changed.')


if __name__ == '__main__':
    main()
