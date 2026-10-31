# Deployment for bot.example.com

Example configuration only. Replace bot.example.com with your own hostname
and confirm access to the host before deployment.

1. Inspect the host's existing reverse proxy and workloads. Preserve existing sites.
2. Copy the application source and deployment files. Provision `.env` separately
   with the Telegram token and a stable TOKEN_ENCRYPTION_KEY. Restrict it to mode 600.
   Do not include `.env`, legacy OAuth credentials, or SQLite in image builds.
3. Stop the local bot before starting the server: Telegram polling must have a
   single consumer. To preserve local preferences/accounts, migrate a consistent
   SQLite backup into the volume with UID 10001 ownership; carry over the same
   encryption key. Do not copy a live WAL database as a plain file.
4. Run `docker compose -f deploy/compose.yaml up -d --build` from repository root.
   The callback port is exposed only on server loopback. The bot runs as a non-root user.
5. Add an A record `bot` pointing at the confirmed host's public IP, TTL 600.
6. If the server uses Caddy, merge the provided site block into its configuration,
   validate it, and reload. If it uses another proxy, configure the equivalent HTTPS
   virtual host. Do not install a competing proxy on ports already in use.
7. Verify `https://bot.example.com/callback` returns a controlled HTTP 400 for
   missing OAuth state. Other paths should return 404. Do not retain callback query
   strings in reverse-proxy access logs.
8. Use `/connect` in Telegram. Verify Swiggy accepts the exact redirect URI,
   complete login, and confirm from the originating Telegram account.

Keep the database volume and encryption key backed up separately. The compose file
creates an empty database on a fresh volume; it does not silently migrate local data.
