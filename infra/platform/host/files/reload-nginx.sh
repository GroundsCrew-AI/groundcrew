#!/bin/sh
# certbot deploy hook: load renewed certificates.
nginx -t -q && systemctl reload nginx
