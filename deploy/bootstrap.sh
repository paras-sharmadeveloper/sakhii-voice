#!/usr/bin/env bash
# One-time server setup (Ubuntu 22.04/24.04), run as root:
#   sudo bash deploy/bootstrap.sh voice.YOURDOMAIN.com "ssh-ed25519 AAAA... github-deploy"
# Afterwards: fill /opt/sakhii-voice/shared/.env, then push to main.
set -euo pipefail

DOMAIN="${1:?usage: bootstrap.sh <domain> <deploy public key>}"
DEPLOY_PUBKEY="${2:?usage: bootstrap.sh <domain> <deploy public key>}"
ROOT=/opt/sakhii-voice
HERE="$(cd "$(dirname "$0")" && pwd)"

apt-get update -y
apt-get install -y nginx certbot python3-certbot-nginx rsync curl

id sakhii >/dev/null 2>&1 || useradd -m -s /bin/bash sakhii
install -d -o sakhii -g sakhii -m 700 /home/sakhii/.ssh
grep -qxF "$DEPLOY_PUBKEY" /home/sakhii/.ssh/authorized_keys 2>/dev/null \
  || echo "$DEPLOY_PUBKEY" >> /home/sakhii/.ssh/authorized_keys
chown sakhii:sakhii /home/sakhii/.ssh/authorized_keys
chmod 600 /home/sakhii/.ssh/authorized_keys

# uv (and through it Python 3.12) for the sakhii user.
sudo -u sakhii bash -lc 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u sakhii bash -lc '~/.local/bin/uv python install 3.12'

install -d -o sakhii -g sakhii "$ROOT" "$ROOT/releases" "$ROOT/shared"
if [ ! -f "$ROOT/shared/.env" ]; then
  install -o sakhii -g sakhii -m 600 "$HERE/../.env.example" "$ROOT/shared/.env"
  echo ">> Edit $ROOT/shared/.env (keys, Redis, EXOTEL_WS_TOKEN) before the first deploy."
fi

# The deploy user may restart only the engine units.
cat > /etc/sudoers.d/sakhii-voice <<SUDO
sakhii ALL=(root) NOPASSWD: /usr/bin/systemctl restart sakhii-voice@8800, /usr/bin/systemctl restart sakhii-voice@8801, /usr/bin/systemctl start sakhii-voice@8800, /usr/bin/systemctl start sakhii-voice@8801
SUDO
chmod 440 /etc/sudoers.d/sakhii-voice
visudo -cf /etc/sudoers.d/sakhii-voice

install -m 644 "$HERE/sakhii-voice@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sakhii-voice@8800 sakhii-voice@8801

sed "s/voice.YOURDOMAIN.com/$DOMAIN/g" "$HERE/nginx-voice.conf" > /etc/nginx/sites-available/sakhii-voice
ln -sf /etc/nginx/sites-available/sakhii-voice /etc/nginx/sites-enabled/sakhii-voice
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
fi
nginx -t && systemctl reload nginx
echo ">> Bootstrap done. Add the GitHub secrets and push to main to deploy."
