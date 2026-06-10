# TLS for DataOpsStudio 2.0

The API listens plain HTTP on `127.0.0.1:8020` (see `app/config.py`,
`DATAOPS_API_PORT`). It does **not** terminate TLS itself. TLS is terminated by a
reverse proxy (nginx) in front of it. This matches the 1.x deployment shape: a
reverse proxy fronts the app, and the certificate strategy switches from
self-signed to Let's Encrypt once the domain's ICP filing (备案) clears.

## Two stages

| Stage | Certificate | When |
|---|---|---|
| **Dogfood / pre-filing** (default) | **Self-signed** | No public domain yet, or ICP filing (备案) not done. Browsers warn; that is expected for internal dogfood. |
| **Production** | **Let's Encrypt** | Public domain resolves to the host and the ICP filing has cleared, so port 80/443 can serve real traffic for the ACME HTTP-01 challenge. |

> Secrets discipline (contract §5): never put the real server IP, domain, or any
> credential into git. The examples below use the placeholder `dataops.example.com`;
> substitute your real domain only on the host.

## Stage 1 — self-signed (dogfood default)

Generate a self-signed cert and key on the host (kept out of git — store under
`$DATAOPS_HOME` or `/etc/ssl/`, mode `0600` on the key):

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /etc/ssl/private/dataops-selfsigned.key \
  -out    /etc/ssl/certs/dataops-selfsigned.crt \
  -days 365 -subj "/CN=dataops.example.com"
```

Minimal nginx reverse proxy in front of the API:

```nginx
server {
    listen 443 ssl;
    server_name dataops.example.com;

    ssl_certificate     /etc/ssl/certs/dataops-selfsigned.crt;
    ssl_certificate_key /etc/ssl/private/dataops-selfsigned.key;

    location / {
        proxy_pass http://127.0.0.1:8020;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Browsers show a certificate warning for self-signed certs. That is acceptable for
internal dogfood; do not ship self-signed to real users.

## Stage 2 — switch to Let's Encrypt (after ICP filing / 备案)

Prerequisites: a public domain pointing at the host and a cleared ICP filing so
ports 80/443 can serve the ACME challenge.

```bash
sudo apt-get install -y certbot python3-certbot-nginx
# Issues the cert, edits the nginx server block to use it, and sets up renewal:
sudo certbot --nginx -d dataops.example.com
```

certbot rewrites the `ssl_certificate` / `ssl_certificate_key` lines to point at
`/etc/letsencrypt/live/dataops.example.com/` and installs a renewal timer
(`systemctl status certbot.timer`). Renewal reloads nginx automatically; the
DataOpsStudio API process is unaffected and needs no restart.

To go back to a known-good config if issuance fails, keep the Stage-1 server block
commented in place until `certbot renew --dry-run` succeeds.

## Notes

- Keep TLS termination at the proxy. The app stays on loopback HTTP; do not expose
  `8020` publicly.
- The reverse proxy's own access logs may capture request metadata. Do not enable
  request-body logging — datasource creation still carries the target DB password
  in the request body (tracked in `docs/backlog.md`), and bodies must not land in
  proxy logs.
