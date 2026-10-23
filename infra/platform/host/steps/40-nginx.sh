# nginx, the only public entry, and certbot's renewal hook. Before the first certificate, the
# bootstrap site answers only Let's Encrypt challenges (gc-certs issue switches over).
set -euo pipefail
DOMAIN=${GC_DOMAIN:-groundkrew.com}
API_DOMAIN=${GC_API_DOMAIN:-api.groundkrew.com}
install -d /opt/groundcrew/nginx /var/www/acme
install -m 0644 "$HERE"/nginx/*.conf /opt/groundcrew/nginx/
site() { sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__API_DOMAIN__/$API_DOMAIN/g" "$HERE/nginx/$1"; }

install -m 0644 "$HERE/nginx/groundcrew-common.conf" /etc/nginx/conf.d/groundcrew-common.conf
# Ubuntu's nginx.conf sets its own TLS protocols and cipher preference at http level; the
# Groundcrew site sets them (TLS 1.2+), and nginx refuses both.
sed -i -E 's/^(\s*)(ssl_protocols|ssl_prefer_server_ciphers)\b/\1# groundcrew: set in sites-enabled\/groundcrew.conf # \2/' /etc/nginx/nginx.conf
rm -f /etc/nginx/sites-enabled/default
install -m 0644 "$HERE/nginx/default.conf" /etc/nginx/sites-available/00-default.conf
ln -sf /etc/nginx/sites-available/00-default.conf /etc/nginx/sites-enabled/00-default.conf
if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  site groundcrew.conf >/etc/nginx/sites-available/groundcrew.conf
  ln -sf /etc/nginx/sites-available/groundcrew.conf /etc/nginx/sites-enabled/groundcrew.conf
  rm -f /etc/nginx/sites-enabled/groundcrew-bootstrap.conf
else
  site groundcrew-bootstrap.conf >/etc/nginx/sites-available/groundcrew-bootstrap.conf
  ln -sf /etc/nginx/sites-available/groundcrew-bootstrap.conf /etc/nginx/sites-enabled/groundcrew-bootstrap.conf
fi
install -m 0644 "$HERE/files/logrotate-nginx" /etc/logrotate.d/nginx
install -m 0644 "$HERE/files/fail2ban-nginx.local" /etc/fail2ban/jail.d/groundcrew-nginx.local
install -d /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$HERE/files/reload-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
install -m 0755 "$HERE/bin/gc-certs" /opt/groundcrew/bin/gc-certs
install -m 0644 "$HERE/files/gc-certcheck.service" "$HERE/files/gc-certcheck.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now certbot.timer >/dev/null 2>&1 || true
[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] && systemctl enable --now gc-certcheck.timer >/dev/null 2>&1
nginx -t -q
systemctl enable nginx >/dev/null 2>&1
systemctl reload-or-restart nginx
systemctl restart fail2ban
